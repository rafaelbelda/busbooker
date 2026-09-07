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
- `SCHEDULER_INTERVAL` — minutes between re-lock attempts (default 20)
- `RELOCK_STOP_MINUTES_BEFORE_DEPARTURE` — stop re-locking this long before departure
  (default 15). The provider delists a trip some minutes before it leaves, so a re-lock
  landing after that point cannot succeed.
- `RATE_LIMIT_PER_MIN` — `0` disables per-IP rate limiting (applies to `/seats`,
  `/search` and `/reservations`)
- `FLOW_TIMEOUT_SECONDS` — hard budget for one browser flow (default 240); on expiry
  the flow is abandoned as exit 2 so a hung Chromium cannot wedge the service
- `RETENTION_DAYS` — delete reservations and their logs this long after departure
  (default 7, `0` disables)

`.env.example` documents every setting; copy it to `.env` to start.

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
- **`services/seatmap.py`** — The single owner of mobifacil's `seatMap` format: seat-number
  normalisation, lookup, flattening and the deck grid. Everything else shapes its output.
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
| `3` | Trip no longer offered by the provider (terminal) | 409 |

Exit `3` is a statement about the world, not a fault: the provider delisted the trip as
departure approached, or every departure for the date has gone. The reservation becomes
`expired` (not `failed`), no re-lock is scheduled, and — unlike exit `2` — the browser
profile is **not** reset and the flow is **not** retried, since neither can bring the
trip back.

### Data Model

`ReservationRecord` (Pydantic, stored as JSON in SQLite):
- `service_id`: mobifacil serviceId of the exact coach. **This is what identifies a
  trip** — departure time does not, since two companies can run the same route at the
  same minute. Empty on records created before this field existed; those fall back to
  departure-time matching (`services/htmlsearch.py::select_trip`).
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

`tests/` holds two different things.

**Automated tests** (`pytest`, hermetic — no browser, no network, no real
reservations). Run them with:

```bash
pytest
```

`tests/conftest.py` pins `ADMIN_PASSWORD` and `RESERVATION_DB=:memory:` so a real
`.env` is never picked up. **Never** point a test at mobifacil: a real flow run
books an actual seat.

**Manual scripts** (named `manual_*` so pytest does not collect them — they hit
live endpoints and must be run deliberately):
- `manual_direct_requests.py` — direct API calls against a running server
- `capture_lockseat.py` — Playwright debugging helper
- `serve_front.py` — local static file server for frontend-only development
