# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

**busbooker** is a seat-reservation automation service that locks specific seats on mobifacil.com.br by driving a headless Chromium browser via Playwright. It holds the seat (and blocks the adjacent one) through automatic re-locking until departure.

## Running the App

```bash
# One-time setup
python -m venv venv
source venv/bin/activate          # Linux/Mac
venv\Scripts\activate             # Windows
pip install -r core/requirements.txt
playwright install chromium
playwright install-deps chromium  # Linux only

# Start the server
bash core/start.sh
# Uvicorn listens on http://127.0.0.1:8771
# Swagger UI: http://127.0.0.1:8771/docs
```

Production runs under systemd: `sudo systemctl restart busbooker`

`start.sh` handles the xvfb/DISPLAY logic automatically — it wraps uvicorn with `xvfb-run` when needed, or runs headless if `HEADLESS=true`.

## Configuration

All config is in `.env` (never committed). Key variables:
- `ADMIN_PASSWORD` — required, no default; app refuses to start without it
- `HEADLESS` — `false` for headed browser (dev), `true` for headless (prod)
- `SCHEDULER_INTERVAL` — minutes between re-lock attempts (default 21)
- `WAIT_AFTER_LOCK` — seconds to hold open checkout tab before confirming (default 60)
- `RATE_LIMIT_PER_MIN` — `0` disables per-IP rate limiting

`core/config.py` is a Pydantic Settings class — all env vars are validated at import time.

## Architecture

### Request Flow

```
Browser → nginx :80 → uvicorn :8771 → FastAPI routes
                  ↘ (static files) → front/
```

nginx (`nginx.conf`) proxies all API paths to `:8771` and serves `front/` for everything else. `proxy_read_timeout=120s` covers the ~90s browser flows.

### Backend (`core/`)

- **`main.py`** — FastAPI app, lifespan (starts scheduler, restores SQLite state), access-log middleware
- **`api/routes.py`** — HTTP endpoints: `GET /health`, `GET /seats`, `POST /search`, `POST /reservations`, `GET|DELETE /reservations/{id}`, `GET /scheduler/status`
- **`api/admin.py`** — `/admin/*` endpoints, HTTP Basic Auth
- **`services/flow.py`** — `run_flow()`: the 7-step orchestrator (search → trip dict → seat check → lock → checkout → hold → confirm). This is where browser automation begins.
- **`services/trip.py`** — Opens the search page; intercepts the BusDetails XHR to extract the trip dict
- **`services/seat.py`** — Checks availability, locks seat via UI click or direct POST
- **`services/checkout.py`** — Navigates checkout, confirms lock persists
- **`services/browser.py`** — Playwright context lifecycle, anti-bot evasion
- **`state.py`** — `ReservationStore` (asyncio.Lock-protected in-memory store) + `FLOW_LOCK` (global single-browser lock)
- **`persistence.py`** — SQLite3 with WAL mode; stores `ReservationRecord` as JSON
- **`scheduler/jobs.py`** — APScheduler wiring; one `relock_<id>` interval job per active reservation

### Concurrency Model

Only one browser flow runs at a time — `FLOW_LOCK` (asyncio.Lock) serializes all `POST /reservations` calls. The synchronous Playwright API is dispatched to a thread-pool executor from async route handlers. `/seats` and `/search` are pure HTTP and bypass the lock entirely.

### Frontend (`front/`)

Vanilla JS SPA with three views: Search, Monitor, Admin. Files load in order:
1. `api.js` — `window.BB` namespace, HTTP client wrapper
2. `app.js` — UI state machine, seat-map rendering, search/reservation flows
3. `monitor.js` — Polls reservation status every 2–3s

The frontend API contract (all endpoints, request/response shapes, error codes) is documented in `front/API_CONTRACT.md`.

### Re-locking Loop

After a successful lock, a recurring APScheduler job (`relock_<id>`) fires every `SCHEDULER_INTERVAL` minutes and re-runs the full 7-step flow. It stops when: reservation is cancelled, departure time passes, or a hard error (exit code 2) occurs.

On restart, `rehydrate_relocks()` in `main.py` re-arms jobs for all active reservations restored from SQLite.

### Exit Codes from `run_flow()`

| Code | Meaning | HTTP response |
|------|---------|---------------|
| `0` | Locked successfully | 201 |
| `1` | Seat unavailable / soft fail (will retry) | 409 |
| `2` | Unrecoverable error | 500 |

### Data Model

`ReservationRecord` (Pydantic, stored as JSON in SQLite):
- `status`: `pending | locked | failed | cancelled | expired`
- `departure_datetime`: computed from date + departure time in São Paulo timezone
- `is_expired`: computed — departure has passed
- `relock_count`: increments on each successful re-lock
- `error_msg`: last error string (seat unavailable, interrupted by restart, etc.)

## Key Files for Common Tasks

| Task | File |
|------|------|
| Add/change API endpoint | `core/api/routes.py` or `core/api/admin.py` |
| Change browser automation steps | `core/services/flow.py`, `trip.py`, `seat.py`, `checkout.py` |
| Modify re-lock scheduling | `core/scheduler/jobs.py` |
| Change frontend UI/behavior | `front/app.js` (>1000 lines), `front/monitor.js` |
| Adjust API client in frontend | `front/api.js` |
| Add config options | `core/config.py` + `.env` |

## Manual Test Scripts

`tests/` contains standalone scripts (not a test framework):
- `test_direct_requests.py` — direct API calls against a running server
- `capture_lockseat.py` — Playwright debugging helper
- `serve_front.py` — local static file server for frontend-only development
