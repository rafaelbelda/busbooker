"""
Steps 3-4 of the flow: seat-availability check and seat locking.

Locking is done through mobifacil's authoritative LockSeat API POST — exactly
what mobifacil's own frontend issues via fetch() when a seat is clicked. The POST
returns an explicit ``success`` flag and a ``seatUUID``, which is the only
deterministic proof a seat is held. (The former canvas-coordinate "UI lock" was
removed: mobifacil renders the seat map on a <canvas>, so a click could never be
verified and always reported false success — see git history / the debug logs.)
"""
from __future__ import annotations

import json
import random
from typing import Optional

from playwright.sync_api import Page

from ..config import LOCK_SEAT_PATH, settings
from ..models.schemas import RouteParams, SeatInfo
from ..utils.logger import log
from .browser import jitter, retry
from .seatmap import find_seat, flatten, raw_label


# ─────────────────────────────────────────────────────────────────
# Step 3 — Availability
# ─────────────────────────────────────────────────────────────────
def parse_seat_map(seat_map: list) -> list[SeatInfo]:
    """Flatten the seatMap for the /seats endpoint.

    Structure knowledge lives in ``seatmap``; this only shapes it into SeatInfo.
    ``posX``/``posY`` are the array indices (the 2D structure IS the coordinate
    system) and ``posZ`` is the empty-row divider count.
    """
    return [
        SeatInfo(
            number=s.number,
            available=s.available,
            posX=float(s.depth),
            posY=float(s.cross),
            posZ=float(s.deck),
        )
        for s in flatten(seat_map)
    ]


def seat_is_locked(seat_map: list, params: RouteParams) -> bool:
    """True iff the seat exists, is not an idoso seat, and is currently held."""
    cell = find_seat(seat_map, params.seat)
    if cell is None or cell.get("idoso"):
        return False
    return not bool(cell.get("disponivel", True))


def check_seat_availability(seat_map: list, params: RouteParams) -> bool:
    target = params.seat
    cell = find_seat(seat_map, target)
    if cell is None:
        log.warning(f"[step 3] seat '{target}' not found in seatMap")
        return False
    # Priority (idoso) seats are reservable only via attendance — mobifacil's own
    # UI blocks them client-side and a LockSeat POST would be refused. Treat as
    # unavailable so we fail fast with a clear reason.
    if cell.get("idoso"):
        log.warning(
            f"[step 3] seat '{target}' is a priority/idoso seat — "
            "reservable only via attendance, not bookable here"
        )
        return False
    avail = cell.get("disponivel", False)
    log.info(f"[step 3] seat '{target}' → disponivel={avail}")
    return bool(avail)


# ─────────────────────────────────────────────────────────────────
# Step 4 — Lock seat (authoritative LockSeat API)
# ─────────────────────────────────────────────────────────────────
def _coerce_seats_with_price(trip: dict) -> str:
    """
    FIX (bug 4): the original sent ``str(trip.get("seatsWithPrice", ""))``.
    For a list/dict that yields a Python repr with single quotes — invalid JSON
    that triggers the server's "Unexpected token" error — and ``str([])`` leaks
    a useless ``"[]"`` past the later empty-string filter. Coerce to real JSON
    (or an empty string) up front so the filter behaves correctly.
    """
    raw = trip.get("seatsWithPrice", "")
    if isinstance(raw, (list, dict)):
        return json.dumps(raw, separators=(",", ":")) if raw else ""
    return str(raw) if raw not in (None, "") else ""


def _info_connection(trip: dict) -> str:
    """
    Build LockSeat's ``infoConnection`` value, mirroring mobifacil's frontend
    (``infoConnection = busDetails.objConnection || null``). The server
    ``JSON.parse``s this field, so it must always be present and parseable:
    the real connection object as JSON when the trip has one, otherwise the
    literal string ``"null"`` (which ``JSON.parse`` reads as null). Never ""
    and never absent — either makes the server parse ``undefined`` and fail.
    """
    obj_conn = trip.get("objConnection")
    if obj_conn:
        return json.dumps(obj_conn, separators=(",", ":"))
    return "null"


def _resolve_seat_label(trip: dict, requested: str) -> str:
    """The seatMap's RAW label for the requested seat ("05", not our "5").

    Mobifacil's LockSeat sends ``seat = t.numero``, so the padded form is what the
    server matches on.
    """
    seat_map = trip.get("seatMap") or trip.get("seatsWithPrice") or []
    return raw_label(seat_map, requested)


def _build_lock_payload(trip: dict, params: RouteParams) -> dict:
    seats_with_price = _coerce_seats_with_price(trip)
    payload = {
        "busNumber": "firstBus",
        "origin": trip.get("originId", params.origin_id),
        "destination": trip.get("destinationId", params.destination_id),
        "date": params.date,
        "service": trip["service"],
        "departureHour": params.departure,
        "group": trip.get("group", "TOTAL_BUS"),
        # seat must be the seatMap's raw numero ("05"), matching mobifacil's
        # `seat = t.numero`, not the normalised request value ("5").
        "seat": _resolve_seat_label(trip, params.seat),
        "arrival": trip.get("arrival", trip.get("arrivalHour", "")),
        "company": trip.get("company", ""),
        "departure": trip.get("departure", params.departure),
        "originUf": trip.get("originUf", ""),
        "serviceClass": trip.get("serviceClass", ""),
        "step": trip.get("stepNumber", "1"),
        "rutaId": trip.get("rutaId", ""),
        "empresaId": trip["empresaId"],
        "isUpsell": "false",
        "upsellOriginalClass": trip.get("serviceClass", ""),
        "upsellOriginalPrice": trip.get("preco", ""),
        "upsellOriginalServiceNo": trip["service"],
        "upsellOriginalTime": params.departure.replace(":", ""),
        "seatMap": seats_with_price,
        # The server JSON.parses infoConnection (LockSeatModel.js:115). Mobifacil's
        # own frontend ALWAYS appends it: `infoConnection = objConnection || null`,
        # which URLSearchParams coerces to the literal string "null" for
        # non-connection trips. Sending "" — or dropping the field via the
        # empty-filter below — makes the server parse `undefined`, producing
        # "Unexpected token: u" (JSON.parse(undefined)). Always send a parseable
        # value: real connection JSON when present, else the string "null".
        "infoConnection": _info_connection(trip),
        "raceDate": trip.get("raceDate", params.date),
        "fareId": trip["fareId"],
        "fareCode": trip.get("fareCode", "FARE-1"),
        "offerId": trip.get("offerId", ""),
        "connectionId": trip.get("connectionId", ""),
        "isDistribusion": trip.get("isDistribusion", "true"),
        "isWebView": "false",
        "isMobile": "false",
    }
    # Drop empties (now safe: seatMap is real JSON or a true empty string).
    return {k: v for k, v in payload.items() if v is not None and v != ""}


def lock_seat_api(page: Page, trip: dict, params: RouteParams) -> Optional[str]:
    """Lock the seat via mobifacil's LockSeat API — posts the frontend's
    URLSearchParams form.

    Returns the ``seatUUID`` (proof of hold) on success, or ``None`` when the
    seat is declined / not lockable (business decline — caller maps to a soft
    fail). Raises only on transport-level failures (HTTP error / unparseable
    body), which ``retry`` absorbs and, if persistent, surfaces as a hard error.
    """
    log.info("[step 4] locking seat via LockSeat API")
    payload = _build_lock_payload(trip, params)
    log.info(f"[step 4] LockSeat payload keys: {list(payload.keys())}")
    log.info(f"[step 4] LockSeat payload: {json.dumps(payload, indent=2)[:500]}")

    def _post() -> Optional[str]:
        # Headers mirror mobifacil's own fetch: only Content-Type. (Referer is kept
        # for anti-bot parity — a real browser would send it automatically.)
        resp = page.request.post(
            settings.base_url + LOCK_SEAT_PATH,
            form=payload,
            headers={
                "Referer": settings.base_url + "/passagem-de-onibus/",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=25_000,
        )
        # Transient/infra problems are worth retrying (the seat may free, the edge
        # may hiccup). A 200 with a server-level error is a *decision*, not a glitch.
        if resp.status != 200:
            raise RuntimeError(f"LockSeat API HTTP {resp.status}")
        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to parse LockSeat response: {e}") from e

        log.info(f"[step 4] LockSeat response: {json.dumps(data)[:400]}")
        if data.get("error") or not data.get("success"):
            error_msg = data.get("message", "Unknown error")
            title = data.get("title", "")
            if "Unexpected token" in error_msg:
                # Server couldn't parse our payload — retrying won't help, and a
                # malformed payload is a code bug, not a busy seat. Log loudly and
                # stop (return None → soft fail) instead of resetting the browser.
                log.error("[step 4] Server JSON parse error — malformed payload (bug)")
                # Capped: the payload embeds the ENTIRE seat map, so an uncapped
                # dump wrote ~100 KB into the log on every occurrence.
                log.error(f"[step 4] Payload sent (truncated): {json.dumps(payload)[:2000]}")
                return None
            # Business decline (seat taken / not lockable). Mirrors the frontend,
            # which just surfaces o.error. Don't retry or reset the browser — report
            # unavailable so the scheduler retries on its normal interval.
            log.warning(f"[step 4] LockSeat declined: {title or error_msg}")
            return None

        # success=true. seatUUID is mobifacil's hold token and our authoritative
        # proof; on the rare success-without-uuid, treat the success flag itself as
        # proof (mirrors the frontend) but flag it so it's visible in the logs.
        seat_uuid = data.get("seatUUID")
        if not seat_uuid:
            log.warning("[step 4] LockSeat success but no seatUUID — trusting success flag")
            seat_uuid = "locked"
        log.info(f"[step 4] LockSeat success — uuid={seat_uuid}")

        # Basket state, logged so it stops being guesswork. Production logs showed
        # these climbing (a FIRST lock reporting quantityTotal=2, later 4/4), which
        # suggests the single persistent Chromium profile gives EVERY reservation
        # one shared cart at the provider — so cancelling never removes a seat from
        # it, and a basket cap would surface as a bogus "seat unavailable". Observe
        # before acting: the field semantics are inferred from a handful of samples.
        qty, count = data.get("quantityTotal"), data.get("count")
        if qty is not None or count is not None:
            log.info(
                f"[step 4] provider basket: quantityTotal={qty} count={count} "
                f"total={data.get('total')}"
            )
            if isinstance(qty, int) and qty > 1:
                log.warning(
                    f"[step 4] provider basket holds {qty} seats — expected 1 per "
                    "reservation; seats from other reservations may be accumulating"
                )
        return seat_uuid

    return retry(_post, "api_seat_lock", attempts=3)


def lock_seat(page: Page, trip: dict, params: RouteParams) -> Optional[str]:
    """Lock the seat. Returns the ``seatUUID`` on success, else ``None``.

    The LockSeat API POST is the single authoritative path (mobifacil's own
    frontend locks the same way). We do a few human-like mouse movements first
    purely for anti-bot parity — they are not a success signal.
    """
    log.info("[step 4] warming interactions before lock")
    for _ in range(3):
        try:
            page.mouse.move(random.randint(300, 800), random.randint(300, 600))
            jitter(200, 500)
        except Exception:  # FIX (bug 2)
            pass

    return lock_seat_api(page, trip, params)
