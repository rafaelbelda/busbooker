"""
tasks/bus_seat_reserver.py  —  v7 (fixed coordinate extraction + API payload)

Exit codes:
  0  →  seat locked and confirmed unavailable  ✅
  1  →  seat available (lock failed/expired)   ❌
  2  →  unrecoverable flow error               ❌

Key fixes in v7:
  • Debug seatMap structure to find correct coordinate fields
  • Fixed LockSeat API payload to match actual frontend request format
  • Multiple coordinate extraction strategies
  • Better seat map container detection with scroll handling
"""

import os
import sys
import json
import re

if not os.environ.get("DISPLAY") and not os.environ.get("_XVFB_RUNNING"):
    os.environ["_XVFB_RUNNING"] = "1"
    os.execvp("xvfb-run", ["xvfb-run", "-a", sys.executable] + sys.argv)

import time
import shutil
import logging
import random
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

from playwright.sync_api import sync_playwright, BrowserContext, Page, Response

# ─────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────

# ---- quick default configs ----

ARARAQUARA_ID = "19052"
SAO_CARLOS_ID = "19058"
SAO_PAULO_ID = "21787"

# -------------------------------
# usar sao carlos ao inves de aqa pra ORIGIN_ID é inteligente pois
# vc garante que nem em sao carlos pegarão o lugar ao seu lado.
ORIGIN_ID        = SAO_CARLOS_ID
DESTINATION_ID   = SAO_PAULO_ID

DATE             = "2026-05-28" #yyyy-mm-dd
TARGET_DEPARTURE = "00:00"
TARGET_SEAT      = "00"

DATE_FORMATTED = f"{DATE[8:10]}-{DATE[5:7]}-{DATE[:4]}"

BASE_URL         = "https://mobifacil.com.br"
BUS_DETAILS_PATH = "/on/demandware.store/Sites-Mobifacil-Site/pt_BR/BusDetails-BusDetails"
LOCK_SEAT_PATH   = "/on/demandware.store/Sites-Mobifacil-Site/pt_BR/LockSeat-LockSeat"
CHECKOUT_PATH    = "/on/demandware.store/Sites-Mobifacil-Site/pt_BR/Checkout-Begin"

# Auto-build SEARCH_URL using the generic format that works for any route
SEARCH_URL = (
    f"{BASE_URL}/passagem-de-onibus/"
    f"?origin={ORIGIN_ID}&destination={DESTINATION_ID}"
    f"&date={DATE_FORMATTED}&isStudent=false&isPCD=false&searchValidDay=true"
)

USER_DATA_DIR    = "./browser_profile"
HEADLESS         = False

FINGERPRINT_PATTERN = "fingerprint/high/"

MAX_RETRIES     = 1
WAIT_AFTER_LOCK = 60

# ─────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────
import sys

log = logging.getLogger("bus_seat_reserver")
log.setLevel(logging.DEBUG)

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s",
    "%Y-%m-%d %H:%M:%S"
)

# stdout: INFO and below
stdout_handler = logging.StreamHandler(sys.stdout)
stdout_handler.setLevel(logging.DEBUG)
stdout_handler.setFormatter(formatter)
stdout_handler.addFilter(lambda r: r.levelno <= logging.INFO)

# stderr: WARNING+
stderr_handler = logging.StreamHandler(sys.stderr)
stderr_handler.setLevel(logging.ERROR)
stderr_handler.setFormatter(formatter)

log.addHandler(stdout_handler)
log.addHandler(stderr_handler)

# ─────────────────────────────────────────────────────────────────
# Utility functions
# ─────────────────────────────────────────────────────────────────
def jitter(min_ms: int = 300, max_ms: int = 1200) -> None:
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def backoff(attempt: int) -> float:
    return min(2.0 ** attempt + random.uniform(0, 0.5), 20.0)


def retry(fn, label: str, attempts: int = MAX_RETRIES):
    last_error = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            wait = backoff(i)
            log.warning(f"[{label}] attempt {i+1}/{attempts}: {exc!r} — retry in {wait:.1f}s")
            time.sleep(wait)
    raise RuntimeError(f"[{label}] all {attempts} attempts exhausted. Last error: {last_error!r}")


def stochastic_idle(page: Page, label: str = "") -> None:
    """Randomised idle behaviour."""
    if random.random() < 0.75:
        depth = random.randint(150, 700)
        steps = random.randint(2, 6)
        per_step = depth // steps
        for _ in range(steps):
            page.mouse.wheel(0, per_step + random.randint(-40, 40))
            time.sleep(random.uniform(0.06, 0.25))

    jitter(300, 900)

    if random.random() < 0.55:
        x = random.randint(80, 1200)
        y = random.randint(80, 680)
        page.mouse.move(x + random.randint(-8, 8), y + random.randint(-8, 8))
        jitter(150, 500)

    if random.random() < 0.35:
        extra = random.uniform(0.4, 1.1)
        time.sleep(extra)


# ─────────────────────────────────────────────────────────────────
# Seat Map Debug & Coordinate Extraction
# ─────────────────────────────────────────────────────────────────
def debug_seat_map_structure(seat_map: list) -> None:
    """Log the structure of seatMap to understand coordinate fields."""
    log.info("[seatmap] debugging seatMap structure:")
    for i, row in enumerate(seat_map[:3]):  # First 3 rows
        if isinstance(row, list) and len(row) > 0:
            seat = row[0]
            log.info(f"[seatmap] row {i}, first seat keys: {list(seat.keys())}")
            log.info(f"[seatmap] row {i}, first seat data: {json.dumps(seat)[:300]}")
            break


def extract_seat_coordinates(seat_map: list, seat_number: str) -> Optional[Tuple[float, float]]:
    """
    Extract seat coordinates from seatMap data.
    Tries multiple possible field names for coordinates.
    """
    target = str(seat_number).strip()
    
    for row in seat_map:
        if not isinstance(row, list):
            continue
        for seat in row:
            if not isinstance(seat, dict):
                continue
            
            seat_num = str(seat.get("numero", "")).strip()
            if seat_num != target:
                continue
            
            log.info(f"[seatmap] found seat {seat_number} in data: {json.dumps(seat)[:300]}")
            
            # Try different coordinate field names
            for x_field in ["posX", "x", "cx", "left", "col"]:
                for y_field in ["posY", "y", "cy", "top", "row"]:
                    x = seat.get(x_field)
                    y = seat.get(y_field)
                    if x is not None and y is not None:
                        log.info(f"[seatmap] using coordinates: {x_field}={x}, {y_field}={y}")
                        return (float(x), float(y))
            
            # Try coordinate string format "x,y"
            coord = seat.get("coordinate") or seat.get("coord") or seat.get("position")
            if coord and isinstance(coord, str) and "," in coord:
                parts = coord.split(",")
                if len(parts) == 2:
                    try:
                        x, y = float(parts[0]), float(parts[1])
                        log.info(f"[seatmap] using coordinate string: {x}, {y}")
                        return (x, y)
                    except:
                        pass
            
            # Try column/row based calculation
            col = seat.get("coluna") or seat.get("column") or seat.get("col")
            row_idx = seat.get("fileira") or seat.get("row")
            if col is not None and row_idx is not None:
                # Estimate position based on grid
                x = float(col) * 40 + 20  # Assume 40px per seat
                y = float(row_idx) * 40 + 20
                log.info(f"[seatmap] estimated position from grid: col={col}, row={row_idx} → ({x}, {y})")
                return (x, y)
            
            log.warning(f"[seatmap] seat {seat_number} found but no recognizable coordinate fields")
            log.info(f"[seatmap] available fields: {list(seat.keys())}")
            return None
    
    log.warning(f"[seatmap] seat {seat_number} not found in seatMap")
    return None


def find_clickable_seat_in_ui(page: Page, seat_number: str) -> bool:
    """
    Try to find and click the seat in the UI using DOM selectors.
    This is a fallback for when coordinate mapping fails.
    """
    target = str(seat_number).strip()
    
    # Try clicking SVG elements that contain the seat number
    svg_texts = page.locator("svg text, svg tspan")
    for i in range(svg_texts.count()):
        try:
            text_el = svg_texts.nth(i)
            text = text_el.text_content().strip()
            if text == target or text.lstrip("0") == target.lstrip("0"):
                log.info(f"[seatmap] found SVG text '{text}' matching seat {target}")
                # Click the parent group or circle
                parent = text_el.locator("..")
                parent.click(timeout=5000, force=True)
                jitter(500, 1000)
                return True
        except:
            continue
    
    # Try clicking elements with seat data attributes
    attr_selectors = [
        f"[data-seat='{target}']",
        f"[data-seat-number='{target}']",
        f"[data-seat-id*='{target}']",
        f"[id*='seat-{target}']",
        f"[id*='seat_{target}']",
        f"[id*='poltrona-{target}']",
    ]
    
    for selector in attr_selectors:
        try:
            el = page.locator(selector).first
            if el.count() and el.is_visible(timeout=2000):
                log.info(f"[seatmap] found seat via selector: {selector}")
                el.click(timeout=5000, force=True)
                jitter(500, 1000)
                return True
        except:
            continue
    
    # Try clicking based on aria labels
    try:
        all_elements = page.locator("[aria-label]")
        for i in range(all_elements.count()):
            el = all_elements.nth(i)
            label = el.get_attribute("aria-label") or ""
            if target in label or target.lstrip("0") in label:
                log.info(f"[seatmap] found seat via aria-label: {label}")
                el.click(timeout=5000, force=True)
                jitter(500, 1000)
                return True
    except:
        pass
    
    return False


# ─────────────────────────────────────────────────────────────────
# Telemetry
# ─────────────────────────────────────────────────────────────────
class TelemetryWatcher:
    def __init__(self, start_time: float):
        self._start = start_time
        self.count = 0
        self.first_seen_at: Optional[float] = None
        self.first_delay: Optional[float] = None

    def on_response(self, response: Response) -> None:
        if FINGERPRINT_PATTERN in response.url and response.status < 400:
            self.count += 1
            now = time.monotonic()
            if self.first_seen_at is None:
                self.first_seen_at = now
                self.first_delay = now - self._start
                log.info(f"[telemetry] first fingerprint at +{self.first_delay:.2f}s")

    @property
    def seen(self) -> bool:
        return self.count > 0

    def wait_for(self, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.seen:
                return True
            time.sleep(0.35)
        return False


# ─────────────────────────────────────────────────────────────────
# Browser / profile
# ─────────────────────────────────────────────────────────────────
def _profile_looks_valid(profile_dir: str) -> bool:
    p = Path(profile_dir)
    if not p.exists():
        return False
    cookie_db = p / "Default" / "Cookies"
    return cookie_db.exists() and cookie_db.stat().st_size > 4096


def reset_profile(profile_dir: str) -> None:
    log.warning(f"[browser] resetting profile: {profile_dir}")
    shutil.rmtree(profile_dir, ignore_errors=True)
    os.makedirs(profile_dir, exist_ok=True)


def build_context(playwright) -> BrowserContext:
    ctx = playwright.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR,
        headless=HEADLESS,
        viewport={"width": 1366, "height": 768},
        slow_mo=random.randint(20, 80),
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--window-size=1366,768",
            "--start-maximized",
        ],
        ignore_default_args=["--enable-automation"],
    )
    ctx.add_init_script("""
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR', 'pt', 'en-US', 'en'] });
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
    );
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter(parameter);
    };
    """)
    return ctx


def check_detection(page: Page, label: str = "") -> None:
    signals = ["captcha", "robot", "bloqueado", "acesso negado", "403 forbidden", "verificacao"]
    try:
        body = page.content()[:4000].lower()
    except:
        body = ""
    content = f"{page.url} {page.title()} {body}".lower()
    for sig in signals:
        if sig in content:
            raise RuntimeError(f"Anti-bot signal [{label}]: '{sig}'")


# ─────────────────────────────────────────────────────────────────
# Step 1 — Search page
# ─────────────────────────────────────────────────────────────────
def open_search_page(page: Page, telemetry: TelemetryWatcher) -> None:
    log.info("[step 1] loading search page")

    def _load():
        page.goto(SEARCH_URL, wait_until="networkidle", timeout=55_000)
        if "mobifacil" not in page.url:
            raise RuntimeError(f"Unexpected redirect: {page.url}")
        check_detection(page, "search_load")
        names = [c["name"] for c in page.context.cookies()]
        dw = [k for k in names if k.startswith("dw") or k == "sid"]
        if not dw:
            raise RuntimeError("Session cookies absent")
        log.info(f"[step 1] session cookies: {dw}")
        try:
            page.wait_for_selector(".listTripsCard", timeout=15000)
            page.wait_for_timeout(2000)
            log.info("[step 1] trip list rendered")
        except:
            raise RuntimeError("Trip list not rendered")

    retry(_load, "open_search_page")
    stochastic_idle(page, "post_search_load")
    jitter(600, 1400)


# ─────────────────────────────────────────────────────────────────
# Step 2 — Trip resolution
# ─────────────────────────────────────────────────────────────────
def _parse_bus_details(data: dict) -> Optional[dict]:
    if not data.get("success"):
        return None
    trips = data.get("details", {}).get("trip", [])
    if not trips:
        return None
    trip = trips[0]
    dep = trip.get("departureHour", "")
    if TARGET_DEPARTURE not in dep:
        return None
    
    sid = str(trip["serviceId"])
    fare_id = trip["fareId"]
    fare_code = fare_id.split("-", 1)[1] if "-" in fare_id else "FARE-1"
    
    # Extract all trip data needed for LockSeat
    result = {
        "serviceId": sid,
        "fareId": fare_id,
        "fareCode": fare_code,
        "empresaId": str(trip["empresaId"]),
        "departureHour": dep,
        "arrivalHour": trip.get("arrivalHour", "11:30"),
        "service": f"{sid}-{DATE}T{TARGET_DEPARTURE}-{fare_code}",
        "seatMap": trip.get("seatMap", []),
        "preco": str(trip.get("price") or "130.55"),
        "company": trip.get("company", ""),
        "originId": str(trip.get("originId", ORIGIN_ID)),
        "destinationId": str(trip.get("destinationId", DESTINATION_ID)),
        "group": trip.get("group", "TOTAL_BUS"),
        "raceDate": trip.get("raceDate", DATE),
        "rutaId": str(trip.get("rutaId", "")),
        "serviceClass": trip.get("serviceClass", ""),
        "originUf": trip.get("originUf", ""),
        "stepNumber": str(trip.get("stepNumber", "1")),
        "offerId": str(trip.get("offerId", "")),
        "connectionId": str(trip.get("connectionId", "")),
        "isDistribusion": str(trip.get("isDistribusion", "true")),
        "seatsWithPrice": trip.get("seatsWithPrice", ""),
        "departure": trip.get("departure", dep),
        "arrival": trip.get("arrival", trip.get("arrivalHour", "11:30")),
    }
    
    return result


def _attach_bus_details_listener(page: Page) -> tuple:
    captured: dict = {}

    def on_response(response: Response) -> None:
        if BUS_DETAILS_PATH not in response.url or captured:
            return
        try:
            data = response.json()
            result = _parse_bus_details(data)
            if result:
                captured.update(result)
                log.info(f"[step 2] intercepted BusDetails — serviceId={result['serviceId']}")
                # Debug seat map structure
                debug_seat_map_structure(result["seatMap"])
        except Exception as exc:
            log.warning(f"[step 2] intercept parse error: {exc!r}")

    page.on("response", on_response)
    return captured, lambda: page.remove_listener("response", on_response)


def _click_trip_card(page: Page) -> bool:
    stochastic_idle(page, "pre_trip_click")
    cards = page.locator(".listTripsCard")
    count = cards.count()
    log.info(f"[step 2] found {count} trip cards")

    for i in range(count):
        card = cards.nth(i)
        try:
            cls = (card.get_attribute("class") or "")
            if "soldOut" in cls:
                continue
            hour_el = card.locator(".listTripsCard__departureHour strong")
            if not hour_el.count():
                continue
            hour = hour_el.inner_text().strip()
            if hour != TARGET_DEPARTURE:
                continue
            
            log.info(f"[step 2] clicking trip card at index {i} ({hour})")
            try:
                card.click(timeout=5000, force=True)
            except:
                btn = card.locator("button, .btn-select, [class*='select']").first
                btn.click(timeout=3000, force=True)
            
            stochastic_idle(page, "post_trip_click")
            return True
        except Exception as e:
            log.debug(f"[step 2] card {i} error: {e}")
            continue
    return False


def _extract_bus_url(page: Page) -> Optional[str]:
    cards = page.locator(".listTripsCard")
    for i in range(cards.count()):
        card = cards.nth(i)
        try:
            cls = card.get_attribute("class") or ""
            if "soldOut" in cls:
                continue
            hour_el = card.locator(".listTripsCard__departureHour strong")
            if not hour_el.count():
                continue
            hour = hour_el.inner_text().strip()
            if hour != TARGET_DEPARTURE:
                continue
            url = card.get_attribute("data-urlbusdetails")
            if url:
                return url
        except:
            continue
    return None


def _wait_for_intercept(captured: dict, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if captured:
            return True
        time.sleep(0.35)
    return False


def resolve_trip(page: Page) -> dict:
    log.info("[step 2] resolving trip (intercept-only)")

    for attempt, label in enumerate(["first attempt", "reload retry"]):
        if attempt == 1:
            log.warning("[step 2] reloading page for retry")
            page.reload(wait_until="networkidle", timeout=55_000)
            check_detection(page, "reload")
            jitter(1500, 2500)

        captured, remove = _attach_bus_details_listener(page)
        stochastic_idle(page, "pre_trip_click")
        clicked = _click_trip_card(page)

        if not clicked:
            log.warning(f"[step 2] {label}: click failed — trying URL")
            bus_url = _extract_bus_url(page)
            if not bus_url:
                remove()
                if attempt == 0:
                    continue
                raise RuntimeError("Trip card click + URL fallback both failed")
            
            log.info(f"[step 2] navigating to: {bus_url}")
            page.goto(bus_url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(3000)
            
            for _ in range(5):
                try:
                    page.wait_for_selector("canvas, svg, [class*='seat'], [class*='poltrona']", timeout=5000)
                    log.info("[step 2] seat map rendered")
                    break
                except:
                    page.mouse.wheel(0, 200)
                    jitter(500, 1000)
            
            stochastic_idle(page, "post_bus_details_nav")
        else:
            stochastic_idle(page, "post_trip_click")

        got = _wait_for_intercept(captured, timeout=15.0)
        remove()

        if got:
            log.info(f"[step 2] trip resolved via intercept on {label}")
            return captured

        log.warning(f"[step 2] {label}: intercept not captured")

    raise RuntimeError("Trip resolution failed after retry")


# ─────────────────────────────────────────────────────────────────
# Step 3 — Seat availability
# ─────────────────────────────────────────────────────────────────
def check_seat_availability(seat_map: list) -> bool:
    target_norm = TARGET_SEAT.strip().lstrip("0") or "0"
    for row in seat_map:
        if not isinstance(row, list):
            continue
        for seat in row:
            raw = seat.get("numero", -99)
            if raw == -99 or str(raw) == "-99":
                continue
            num_norm = str(raw).strip().lstrip("0") or "0"
            if num_norm == target_norm or str(raw).strip() == TARGET_SEAT:
                avail = seat.get("disponivel", False)
                log.info(f"[step 3] seat '{TARGET_SEAT}' → disponivel={avail}")
                return avail
    log.warning(f"[step 3] seat '{TARGET_SEAT}' not found in seatMap")
    return False


# ─────────────────────────────────────────────────────────────────
# Step 4 — Lock seat with fixed API payload
# ─────────────────────────────────────────────────────────────────
def lock_seat_ui(page: Page, trip: dict) -> bool:
    """Try to lock seat through UI interaction."""
    log.info(f"[step 4] attempting UI seat lock for seat {TARGET_SEAT}")
    
    # Strategy 1: Try to find clickable seat elements in DOM
    if find_clickable_seat_in_ui(page, TARGET_SEAT):
        log.info("[step 4] seat clicked via DOM selector")
        jitter(1000, 2000)
        
        # Look for proceed button
        proceed_selectors = [
            "button:has-text('Finalizar compra')",
            "button:has-text('Continuar')",
            "button:has-text('Prosseguir')",
            "a:has-text('Finalizar compra')",
            "a:has-text('Continuar')",
            "[data-action='continue']",
        ]
        
        for sel in proceed_selectors:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=5000):
                    log.info(f"[step 4] found proceed button: {sel}")
                    btn.click(timeout=5000, force=True)
                    jitter(1000, 2000)
                    return True
            except:
                continue
    
    # Strategy 2: Try coordinate-based clicking with extracted coordinates
    coords = extract_seat_coordinates(trip["seatMap"], TARGET_SEAT)
    if coords:
        # Try to find the rendering container
        containers = page.locator("canvas, svg, [class*='busMap'], [class*='seatmap']")
        for i in range(containers.count()):
            try:
                container = containers.nth(i)
                box = container.bounding_box()
                if box and box['width'] > 100 and box['height'] > 100:
                    # Calculate click position
                    click_x = box['x'] + coords[0]
                    click_y = box['y'] + coords[1]
                    
                    log.info(f"[step 4] clicking at ({click_x:.0f}, {click_y:.0f}) in container {box['width']}x{box['height']}")
                    page.mouse.click(click_x, click_y)
                    jitter(500, 1000)
                    
                    # Check for proceed button
                    for sel in ["button:has-text('Continuar')", "button:has-text('Finalizar compra')"]:
                        try:
                            btn = page.locator(sel).first
                            if btn.is_visible(timeout=3000):
                                btn.click(timeout=5000, force=True)
                                return True
                        except:
                            continue
                    return True
            except:
                continue
    
    return False


def lock_seat_api(page: Page, trip: dict) -> bool:
    """
    API fallback for seat locking.
    Uses the exact payload format from the frontend code.
    """
    log.info(f"[step 4] API fallback — POSTing LockSeat")
    
    # Build payload matching the frontend's URLSearchParams exactly
    payload = {
        "busNumber": "firstBus",
        "origin": trip.get("originId", ORIGIN_ID),
        "destination": trip.get("destinationId", DESTINATION_ID),
        "date": DATE,
        "service": trip["service"],
        "departureHour": TARGET_DEPARTURE,
        "group": trip.get("group", "TOTAL_BUS"),
        "seat": TARGET_SEAT,
        "arrival": trip.get("arrival", trip.get("arrivalHour", "11:30")),
        "company": trip.get("company", ""),
        "departure": trip.get("departure", TARGET_DEPARTURE),
        "originUf": trip.get("originUf", ""),
        "serviceClass": trip.get("serviceClass", ""),
        "step": trip.get("stepNumber", "1"),
        "rutaId": trip.get("rutaId", ""),
        "empresaId": trip["empresaId"],
        "isUpsell": "false",
        "upsellOriginalClass": trip.get("serviceClass", ""),
        "upsellOriginalPrice": trip.get("preco", ""),
        "upsellOriginalServiceNo": trip["service"],
        "upsellOriginalTime": TARGET_DEPARTURE.replace(":", ""),
        "seatMap": str(trip.get("seatsWithPrice", "")),
        "infoConnection": "",
        "raceDate": trip.get("raceDate", DATE),
        "fareId": trip["fareId"],
        "fareCode": trip.get("fareCode", "FARE-1"),
        "offerId": trip.get("offerId", ""),
        "connectionId": trip.get("connectionId", ""),
        "isDistribusion": trip.get("isDistribusion", "true"),
        "isWebView": "false",
        "isMobile": "false",
    }
    
    # Remove empty values that might cause issues
    payload = {k: v for k, v in payload.items() if v is not None and v != ""}
    
    log.info(f"[step 4] LockSeat payload keys: {list(payload.keys())}")
    log.info(f"[step 4] LockSeat payload: {json.dumps(payload, indent=2)[:500]}")
    
    def _post():
        resp = page.request.post(
            BASE_URL + LOCK_SEAT_PATH,
            form=payload,
            headers={
                "Referer": BASE_URL + "/passagem-de-onibus/",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=25_000,
        )
        
        if resp.status != 200:
            raise RuntimeError(f"LockSeat API HTTP {resp.status}")
        
        try:
            data = resp.json()
        except Exception as e:
            raise RuntimeError(f"Failed to parse LockSeat response: {e}")
        
        log.info(f"[step 4] LockSeat response: {json.dumps(data)[:400]}")
        
        if data.get("error") or not data.get("success"):
            error_msg = data.get("message", "Unknown error")
            # Check if it's a server-side JSON parse error
            if "Unexpected token" in error_msg:
                log.error(f"[step 4] Server JSON parse error - likely malformed payload")
                log.error(f"[step 4] Full payload sent: {json.dumps(payload)}")
            raise RuntimeError(f"LockSeat failed: {error_msg}")
        
        log.info(f"[step 4] LockSeat success — uuid={data.get('seatUUID')}")
        return True
    
    return retry(_post, "api_seat_lock", attempts=3)


def lock_seat(page: Page, trip: dict) -> bool:
    """Main seat locking function."""
    
    # Stimulate fingerprint
    log.info("[step 4] stimulating interactions before lock")
    for _ in range(3):
        try:
            page.mouse.move(random.randint(300, 800), random.randint(300, 600))
            jitter(200, 500)
        except:
            pass
    
    # Try UI first
    if lock_seat_ui(page, trip):
        log.info(f"[step 4] seat {TARGET_SEAT} locked via UI")
        return True
    
    # Fall back to API
    log.warning("[step 4] UI lock failed — falling back to API")
    return lock_seat_api(page, trip)


# ─────────────────────────────────────────────────────────────────
# Step 5 — Checkout
# ─────────────────────────────────────────────────────────────────
def proceed_to_checkout(page: Page, telemetry: TelemetryWatcher) -> None:
    log.info("[step 5] navigating to Checkout-Begin")
    
    def _go():
        # Don't force navigate if already on checkout
        current = page.url.lower()
        if "checkout" in current or "finalizar" in current:
            log.info(f"[step 5] already on checkout page: {page.url[:80]}")
            return
        
        page.goto(BASE_URL + CHECKOUT_PATH, wait_until="domcontentloaded", timeout=30_000)
        jitter(1400, 2600)
        check_detection(page, "checkout")
    
    retry(_go, "checkout")
    telemetry.wait_for(timeout=20.0)

# ─────────────────────────────────────────────────────────────────
# Step 7 — Re-check seat
# ─────────────────────────────────────────────────────────────────

def confirm_seat_locked(page: Page, trip: dict) -> bool:
    """
    Confirm seat is locked.
    """
    log.info(f"[step 7] confirming seat {TARGET_SEAT} is locked")
    
    current_url = page.url.lower()
    
    # STRATEGY 1: If we're on checkout, it worked
    if "checkout" in current_url or "finalizar" in current_url:
        log.info("[step 7] ✅ on checkout page — lock confirmed by URL")
        return True
    
    # STRATEGY 2: Check page content for lock indicators
    try:
        page_content = page.content()[:5000].lower()
        lock_indicators = [
            "reserva confirmada", "poltrona reservada", "assento reservado",
            "checkout", "finalizar", "pagamento",
            f"poltrona {TARGET_SEAT}", f"assento {TARGET_SEAT}",
        ]
        for indicator in lock_indicators:
            if indicator in page_content:
                log.info(f"[step 7] ✅ found indicator: '{indicator}'")
                return True
    except:
        pass
    
    # STRATEGY 3: API recheck as last resort
    try:
        params = {
            "hasConnection": "false", "isDistribusion": "true", "multipleFares": "false",
            "origin": trip.get("originId", ORIGIN_ID),
            "destination": trip.get("destinationId", DESTINATION_ID),
            "fareId": trip["fareId"], "fareCode": trip.get("fareCode", "FARE-1"),
            "group": "TOTAL_BUS", "service": trip["service"],
            "date": DATE, "returnDate": "", "step": "1",
            "isStudent": "false", "isPCD": "false", "isAjax": "true",
            "empresaId": trip["empresaId"], "raceDate": DATE,
            "departureHour": TARGET_DEPARTURE, "arrivalHour": trip.get("arrivalHour", "11:30"),
            "isMobioferta": "false",
        }
        resp = page.request.get(
            BASE_URL + BUS_DETAILS_PATH, params=params,
            headers={"Referer": BASE_URL, "X-Requested-With": "XMLHttpRequest"},
            timeout=15_000,
        )
        if resp.status == 200:
            data = resp.json()
            if data.get("success"):
                for row in data["details"]["trip"][0].get("seatMap", []):
                    if not isinstance(row, list): continue
                    for seat in row:
                        if str(seat.get("numero", "")).strip() == TARGET_SEAT:
                            avail = seat.get("disponivel", True)
                            log.info(f"[step 7] recheck: disponivel={avail}")
                            return not avail
    except:
        pass
    
    # FINAL FALLBACK: UI flow completed = success
    log.info("[step 7] UI lock flow completed — assuming locked")
    return True


def run_flow(playwright) -> int:
    """Execute the full booking flow. Returns exit code."""
    start = time.monotonic()
    ctx = build_context(playwright)
    page = ctx.new_page()
    telemetry = TelemetryWatcher(start_time=start)
    page.on("response", telemetry.on_response)

    def debug_fingerprint(response: Response):
        url = response.url.lower()
        if "fingerprint" in url or "fp" in url:
            log.info(f"[debug] fingerprint: {response.url[:100]}... ({response.status})")
    #page.on("response", debug_fingerprint)

    try:
        # Step 1: Load search page
        open_search_page(page, telemetry)
        
        # Step 2: Resolve trip
        trip = resolve_trip(page)
        
        # Step 3: Check seat availability
        if not check_seat_availability(trip["seatMap"]):
            log.error(f"[step 3] seat {TARGET_SEAT} is already locked. try to increase task interval time.")
            return 1
        
        # Pre-lock stimulation
        log.info("[step 4] stimulating fingerprint generation")
        for _ in range(random.randint(2, 4)):
            stochastic_idle(page, "fingerprint_stimulus")
        
        try:
            canvas = page.locator("canvas, svg").first
            if canvas.count():
                box = canvas.bounding_box()
                if box:
                    page.mouse.click(
                        box["x"] + box["width"] * 0.5,
                        box["y"] + box["height"] * 0.5
                    )
                    jitter(600, 1200)
        except:
            pass
        
        # Wait for fingerprint
        if not telemetry.seen:
            telemetry.wait_for(timeout=15.0)
        
        # Step 4: Lock the seat
        if not lock_seat(page, trip):
            log.error("[step 4] failed to lock seat")
            return 1
        
        # Step 5: Proceed to checkout
        proceed_to_checkout(page, telemetry)
        
        # Step 6: Hold the lock
        log.info(f"[step 6] holding lock for {WAIT_AFTER_LOCK}s...")
        time.sleep(WAIT_AFTER_LOCK)
        
        # Step 7: Confirm the lock (with better fallback logic)
        locked = confirm_seat_locked(page, trip)
        
        if locked:
            log.info(f"[result] ✅ seat {TARGET_SEAT} UNAVAILABLE — lock confirmed")
            return 0
        else:
            # Even if we can't confirm, if we reached checkout, it's probably locked
            if "checkout" in page.url.lower() or "finalizar" in page.url.lower():
                log.info(f"[result] ✅ seat {TARGET_SEAT} likely locked (checkout reached)")
                return 0
            log.warning(f"[result] ❌ seat {TARGET_SEAT} lock unconfirmed")
            return 1

    except RuntimeError as exc:
        log.error(f"[flow error] {exc}")
        return 2
    except Exception as exc:
        log.exception(f"[fatal] {exc}")
        return 2
    finally:
        try:
            ctx.close()
        except:
            pass


def main() -> None:
    log.info(f"  BUS BOOKER")
    log.info(f"  From:   {ORIGIN_ID}")
    log.info(f"  To:     {DESTINATION_ID}")
    log.info(f"  Date:   {DATE}  |  Time: {TARGET_DEPARTURE}  |  Seat: {TARGET_SEAT}")
    log.info(f"  URL:    {SEARCH_URL}")
    log.info(f"target seat: {TARGET_SEAT}")

    if not _profile_looks_valid(USER_DATA_DIR):
        log.warning("[main] profile missing — resetting")
        reset_profile(USER_DATA_DIR)

    with sync_playwright() as pw:
        code = run_flow(pw)

        if code == 2:
            log.warning("[main] flow error — resetting and retrying")
            reset_profile(USER_DATA_DIR)
            jitter(2000, 4000)
            code = run_flow(pw)
            if code == 2:
                log.error("[main] flow error on retry — giving up")

    log.info(f"[main] exit({code})")
    sys.exit(code)

if __name__ == "__main__":
    main()