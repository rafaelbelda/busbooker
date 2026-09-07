"""
Browser-session primitives: timing jitter, retry, anti-bot context builder,
detection checks, fingerprint telemetry and persistent-profile helpers.

Core automation behaviour is preserved verbatim from the original script; the
only changes are config injection and the exception-handling fixes.
"""
from __future__ import annotations

import os
import random
import shutil
import time
from pathlib import Path
from typing import Callable, Optional, TypeVar

from playwright.sync_api import BrowserContext, Page, Response

from ..config import FINGERPRINT_PATTERN, settings
from ..utils.logger import log

T = TypeVar("T")


# ─────────────────────────────────────────────────────────────────
# Timing / retry utilities
# ─────────────────────────────────────────────────────────────────
def jitter(min_ms: int = 300, max_ms: int = 1200) -> None:
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def backoff(attempt: int) -> float:
    return min(2.0 ** attempt + random.uniform(0, 0.5), 20.0)


def retry(fn: Callable[[], T], label: str, attempts: Optional[int] = None) -> T:
    """Run ``fn`` up to ``attempts`` times with exponential backoff."""
    if attempts is None:  # FIX (bug 7): None sentinel, never a mutable default
        attempts = settings.max_retries
    last_error: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            wait = backoff(i)
            log.warning(f"[{label}] attempt {i + 1}/{attempts}: {exc!r} — retry in {wait:.1f}s")
            time.sleep(wait)
    # FIX (bug 5): callers catch generic Exception, so keep the label in the
    # message AND chain the original cause instead of discarding the traceback.
    raise RuntimeError(
        f"[{label}] all {attempts} attempts exhausted. Last error: {last_error!r}"
    ) from last_error


def stochastic_idle(page: Page, label: str = "") -> None:
    """Randomised idle behaviour to look human (label is for logging only)."""
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
        time.sleep(random.uniform(0.4, 1.1))


# ─────────────────────────────────────────────────────────────────
# Profile helpers
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


# ─────────────────────────────────────────────────────────────────
# Context builder + detection
# ─────────────────────────────────────────────────────────────────
_INIT_SCRIPT = """
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
"""


def build_context(playwright) -> BrowserContext:
    ctx = playwright.chromium.launch_persistent_context(
        user_data_dir=settings.user_data_dir,
        headless=settings.headless,
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
    ctx.add_init_script(_INIT_SCRIPT)
    return ctx


def check_detection(page: Page, label: str = "") -> None:
    signals = ["captcha", "robot", "bloqueado", "acesso negado", "403 forbidden", "verificacao"]
    try:
        body = page.content()[:4000].lower()
    except Exception:  # FIX (bug 2): never swallow KeyboardInterrupt/SystemExit
        body = ""
    content = f"{page.url} {page.title()} {body}".lower()
    for sig in signals:
        if sig in content:
            raise RuntimeError(f"Anti-bot signal [{label}]: '{sig}'")


# ─────────────────────────────────────────────────────────────────
# Telemetry
# ─────────────────────────────────────────────────────────────────
class TelemetryWatcher:
    """Passive observer of the site's fingerprint telemetry.

    **This no longer blocks the flow.** It used to expose a ``wait_for`` that the
    flow called with a 15 s timeout (and checkout with 5 s). Production logs showed
    ``FINGERPRINT_PATTERN`` matching *zero* responses across 11 runs, so both waits
    consumed their full timeout every single time — ~20 s of dead wait per flow —
    while all 11 locks succeeded anyway. The waits are gone; this class stays purely
    to observe, cheaply, whether the telemetry ever comes back.

    ``near_misses`` exists to answer the obvious follow-up: the pattern never
    matching does not prove the provider stopped fingerprinting, only that it is no
    longer at *this* path. Any URL merely containing "fingerprint" is recorded, so a
    renamed endpoint shows up in the flow summary instead of needing a special
    debugging run.
    """

    def __init__(self, start_time: float):
        self._start = start_time
        self.count = 0
        self.first_seen_at: Optional[float] = None
        self.first_delay: Optional[float] = None
        self.near_misses: set[str] = set()

    def on_response(self, response: Response) -> None:
        url = response.url
        if FINGERPRINT_PATTERN in url and response.status < 400:
            self.count += 1
            now = time.monotonic()
            if self.first_seen_at is None:
                self.first_seen_at = now
                self.first_delay = now - self._start
                log.info(f"[telemetry] first fingerprint at +{self.first_delay:.2f}s")
        elif "fingerprint" in url.lower() and len(self.near_misses) < 5:
            # Fingerprint-ish, but not where we expect it — likely a moved endpoint.
            self.near_misses.add(url.split("?", 1)[0][:200])

    @property
    def seen(self) -> bool:
        return self.count > 0

    def summary(self) -> str:
        if self.seen:
            return f"fingerprint x{self.count} first=+{self.first_delay:.1f}s"
        if self.near_misses:
            return f"fingerprint NOT seen at '{FINGERPRINT_PATTERN}' — near misses: {sorted(self.near_misses)}"
        return "fingerprint not seen"
