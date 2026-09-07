"""
The single place that understands mobifacil's ``seatMap`` structure.

Before this module the same knowledge lived in three parsers (``parse_seat_map``
in seat.py, ``_seats_from_map`` in htmlsearch.py, and an inline loop in
checkout.py), with ``_posZ_from`` copy-pasted verbatim and seat-number
normalisation repeated in five places. They drifted: the double-decker bug fixed
in Round 2 had to be fixed twice, and the flat list disagreed with the deck grid
about how many floors a coach had.

## The format, as observed in production

``seatMap`` is a 2D array. A real cell looks like::

    {"disponivel": false, "numero": "01", "idoso": false,
     "label": "01", "x": "1", "y": "0", "z": "0"}

Two things matter and are easy to get wrong:

* **The array structure IS the coordinate system.** Outer index = bus depth
  (front→back), inner index = cross-section (window→aisle→window). The per-seat
  ``x``/``y`` fields are *not* usable coordinates — they are ``"1"``/``"0"`` on
  every seat in real data.
* **Decks are split on EMPTY rows**, which is mobifacil's own ``hasSecondFloor``
  rule (``seatMap.some(row => row.length === 0)``). The per-seat ``z`` is ``"0"``
  even on the upper deck, so trusting it collapses a double-decker into one floor.

Non-seat cells: ``-99`` and the central column are aisle, ``WC`` is a bathroom,
``ES``/``GE`` are labelled landmarks. Only numeric labels are bookable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional


# ─────────────────────────────────────────────────────────────────
# Seat identity
# ─────────────────────────────────────────────────────────────────
def normalise_seat_number(raw: object) -> Optional[str]:
    """Canonical form of a seat label, or ``None`` if it is not a bookable seat.

    ``"05"`` → ``"5"``, ``"5.0"`` → ``"5"`` (BusDetails sometimes serialises ints
    as floats). Aisles (``-99``) and non-numeric labels (``WC``, ``ES``, ``GE``)
    return ``None``.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text == "-99":
        return None
    try:
        return str(int(float(text)))
    except (ValueError, OverflowError):
        return None


def seat_matches(raw: object, wanted: str) -> bool:
    """True when a seatMap label refers to the seat the user asked for.

    Callers hold the normalised form (``"5"``) while the map holds the padded raw
    label (``"05"``), so both forms must match. Comparing raw strings alone
    silently missed the seat — which is how a re-check could fall through to
    "assume locked".
    """
    wanted = (wanted or "").strip()
    if not wanted:
        return False
    if str(raw).strip() == wanted:
        return True
    norm = normalise_seat_number(raw)
    return norm is not None and norm == (normalise_seat_number(wanted) or wanted)


def iter_seats(seat_map: list) -> Iterator[dict]:
    """Every dict cell in the map, skipping malformed rows and non-dict cells."""
    for row in seat_map or []:
        if not isinstance(row, list):
            continue
        for cell in row:
            if isinstance(cell, dict):
                yield cell


def find_seat(seat_map: list, wanted: str) -> Optional[dict]:
    """The raw cell for ``wanted``, or ``None`` when the map has no such seat."""
    for cell in iter_seats(seat_map):
        if seat_matches(cell.get("numero"), wanted):
            return cell
    return None


def raw_label(seat_map: list, wanted: str) -> str:
    """The map's own label for a seat (``"05"``), falling back to ``wanted``.

    LockSeat matches on ``numero`` verbatim, not on our normalised value.
    """
    cell = find_seat(seat_map, wanted)
    if cell is None:
        return (wanted or "").strip()
    return str(cell.get("numero", wanted)).strip()


# ─────────────────────────────────────────────────────────────────
# Flattening
# ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FlatSeat:
    """One bookable seat with its grid position. ``deck`` counts empty-row dividers."""

    number: str
    available: bool
    idoso: bool
    depth: int      # outer index — bus length, 0 = front
    cross: int      # inner index — cross-section, 2 = corridor
    deck: int


def has_divider(seat_map: list) -> bool:
    """True when the map contains an empty row — mobifacil's double-decker rule."""
    return any(isinstance(row, list) and len(row) == 0 for row in seat_map or [])


def flatten(seat_map: list) -> list[FlatSeat]:
    """Every bookable seat, in reading order. Aisles and landmarks are excluded."""
    out: list[FlatSeat] = []
    deck = 0
    for depth, row in enumerate(seat_map or []):
        if not isinstance(row, list):
            continue
        if len(row) == 0:
            deck += 1
            continue
        for cross, cell in enumerate(row):
            if not isinstance(cell, dict):
                continue
            number = normalise_seat_number(cell.get("numero", -99))
            if number is None:
                continue
            out.append(FlatSeat(
                number=number,
                available=bool(cell.get("disponivel", False)),
                idoso=bool(cell.get("idoso", False)),
                depth=depth,
                cross=cross,
                deck=deck,
            ))
    return out


# ─────────────────────────────────────────────────────────────────
# Deck grid
# ─────────────────────────────────────────────────────────────────
def classify_cell(numero: object, cross: int) -> tuple[str, str]:
    """``(kind, label)`` for a raw cell, mirroring mobifacil's own render classes."""
    s = str(numero).strip()
    if s == "-99":
        return "aisle", ""
    if s == "WC":
        return "bathroom", "WC"
    if s in ("ES", "GE"):
        return "marker", s
    if cross == 2:                    # the central column is always the corridor
        return "aisle", ""
    return "seat", s


def build_decks(seat_map: list) -> list[dict]:
    """Convert the raw map into ``decks → rows → cells``, matching mobifacil's render.

    Every cell keeps its grid slot (aisles and landmarks included) so columns stay
    aligned — unlike :func:`flatten`, which drops them. Decks are split on empty
    rows; the segment AFTER the divider is Primeiro Piso and is shown first, which
    is what mobifacil's FloorSwitch does.
    """
    segments: list[list] = []
    current: list = []
    for row in seat_map or []:
        if not isinstance(row, list):
            continue
        if len(row) == 0:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(row)
    if current:
        segments.append(current)
    if not segments:
        return []

    def _rows(segment: list) -> list[list[dict]]:
        out = []
        for row in segment:
            cells = []
            for cross, cell in enumerate(row):
                if not isinstance(cell, dict):
                    continue
                kind, label = classify_cell(cell.get("numero", -99), cross)
                cells.append({
                    "kind": kind,
                    "number": label,
                    "available": bool(cell.get("disponivel", False)),
                    "idoso": bool(cell.get("idoso", False)),
                })
            out.append(cells)
        return out

    decks = [_rows(seg) for seg in segments]
    if len(decks) > 1:
        decks = decks[::-1]
    return [{"label": f"FLOOR {i + 1}", "rows": rows} for i, rows in enumerate(decks)]
