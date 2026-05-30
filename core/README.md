# Bus Reserver

A production-ready FastAPI service that **locks a specific bus seat** on
[mobifacil.com.br](https://mobifacil.com.br) by driving a real Chromium browser
with [Playwright](https://playwright.dev/python/), then confirming the lock via
the site's internal API.

The locking trick: by holding a seat reserved on a recurring schedule you keep
the *adjacent* seat effectively blocked, so nobody is seated next to you.

> Refactored from the original ~1000-line `draft.py` script. Core browser
> automation behaviour is unchanged; the code was modularised, parameterised
> per-request, and a batch of latent bugs were fixed (each marked with a
> `# FIX:` comment in the source).

---

## Overview

The service exposes a small HTTP API around a 7-step browser flow:

1. Launch a persistent Chromium context with anti-bot evasion.
2. Open the search page and wait for session cookies (`dw*`, `sid`).
3. Intercept the `BusDetails-BusDetails` XHR to extract the trip dict
   (`serviceId`, `fareId`, `empresaId`, `seatMap`, …).
4. Check seat availability (`disponivel`) in the seat map.
5. Lock the seat — UI click (SVG/DOM/aria + coordinate mapping) with a direct
   `LockSeat-LockSeat` POST fallback.
6. Navigate to `Checkout-Begin` and hold the lock for `WAIT_AFTER_LOCK` seconds.
7. Confirm the lock (URL → page content → `BusDetails` re-fetch).

Exit codes returned by the flow map onto HTTP status codes:

| Exit | Meaning                          | HTTP |
|------|----------------------------------|------|
| 0    | seat locked & confirmed          | 201  |
| 1    | seat unavailable / lock failed   | 409  |
| 2    | unrecoverable flow error         | 500  |

No database, no users, completely anonymous — reservations live in an in-memory
store keyed by UUID.

---

## Architecture

```
                              HTTP (nginx :80, gzip JSON, 120s read timeout)
                                         │
                                         ▼
   ┌───────────────────────────────────────────────────────────────┐
   │                      FastAPI app (core/main.py)                 │
   │                                                                 │
   │   api/routes.py            scheduler/jobs.py                    │
   │   ├─ GET  /health          └─ AsyncIOScheduler                  │
   │   ├─ GET  /seats              every SCHEDULER_INTERVAL (21 min) │
   │   ├─ POST /reservations  ─┐      │                              │
   │   ├─ GET  /reservations/{id}     │                              │
   │   ├─ DEL  /reservations/{id}     │                              │
   │   └─ GET  /scheduler/status      │                              │
   │                          │       │                              │
   │              run_in_executor + FLOW_LOCK (one browser at a time)│
   │                          ▼       ▼                              │
   │                    services/flow.py  run_flow()                 │
   │                    ├─ trip.py     (steps 1-2)                   │
   │                    ├─ seat.py     (steps 3-4)                   │
   │                    └─ checkout.py (steps 5-7)                   │
   └───────────────────────────────┬───────────────────────────────┘
                                    │  Playwright sync API (own thread)
                                    ▼
                          Chromium  (xvfb virtual display on Linux)
                                    │
                                    ▼
                          https://mobifacil.com.br
```

`run_flow()` uses Playwright's **synchronous** API, so every request that
triggers it (`POST /reservations`, `GET /seats`) and the scheduler job dispatch
it to a thread-pool executor and serialise it behind a global `asyncio.Lock`
(`FLOW_LOCK`) — the persistent browser profile must never be driven by two
flows at once. This is also why uvicorn runs with **a single worker**.

---

## Prerequisites

- **Python 3.11+**
- **Linux** for production (headed Chromium needs an X server → `xvfb`). macOS
  and Windows work for local dev (Chromium runs headed natively; xvfb is skipped).
- `xvfb` on Linux: `sudo apt-get install -y xvfb`
- `nginx` (deployment only)

## Local installation

```bash
# from the project root (parent of core/)
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

pip install -r core/requirements.txt
playwright install chromium          # download the browser binary
# Linux only — system deps for Chromium:
playwright install-deps chromium
```

### Configuration

All settings have sane defaults (see the table below) and can be overridden via
environment variables or a `.env` file in the project root:

```dotenv
# .env
ORIGIN_ID=19058
DESTINATION_ID=21787
DATE=2026-05-28
TARGET_DEPARTURE=00:00
TARGET_SEAT=00
HEADLESS=false
WAIT_AFTER_LOCK=60
SCHEDULER_INTERVAL=21
```

---

## How to run locally

```bash
bash core/start.sh
```

`start.sh` resolves the project root, picks the display strategy (existing
`DISPLAY` → use it; none + `xvfb-run` available → wrap; `HEADLESS=true` → skip),
and launches:

```bash
uvicorn core.main:app --host 127.0.0.1 --port 8771 --workers 1
```

Interactive API docs are then available at <http://127.0.0.1:8771/docs>.

> On Windows there is no `xvfb`; the app detects this and runs headed Chromium
> directly. Set `HEADLESS=true` to run without a visible window anywhere.

---

## How to deploy (nginx + systemd)

**1. nginx reverse proxy**

```bash
sudo cp core/nginx.conf /etc/nginx/sites-available/bus-reserver
sudo ln -s /etc/nginx/sites-available/bus-reserver /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

The config proxies `:80 → 127.0.0.1:8771`, forwards `Host`, `X-Real-IP`,
`X-Forwarded-For`, `X-Forwarded-Proto`, gzips `application/json`, uses a 120 s
read timeout (flows can take ~90 s) and a 10 s connect timeout, and skips access
logging for `/health`. TLS is left to the operator (e.g. certbot).

**2. systemd service** — `core/bus-reserver.service`:

```ini
[Unit]
Description=Bus Reserver (mobifacil.com.br seat locker)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=busreserver
WorkingDirectory=/opt/bus-reserver
Environment=PATH=/opt/bus-reserver/.venv/bin:/usr/bin:/bin
ExecStart=/usr/bin/env bash /opt/bus-reserver/core/start.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo cp core/bus-reserver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bus-reserver
sudo journalctl -u bus-reserver -f      # follow logs
```

Alternatively, run it inside a `screen`/`tmux` session: `bash core/start.sh`.

---

## Endpoint reference

Base URL below assumes local dev (`http://127.0.0.1:8771`).

### `GET /health`
Liveness: status, uptime, scheduler next-run time.
```bash
curl http://127.0.0.1:8771/health
```
```json
{ "status": "ok", "uptime_seconds": 42.1, "scheduler_next_run": "2026-05-30T18:21:00+00:00" }
```

### `GET /seats`
Fetch the live seat map for the configured route (re-uses trip resolution —
drives the browser, so it is serialised with reservations). All query params are
optional and fall back to config.
```bash
curl "http://127.0.0.1:8771/seats?seat=00"
```
```json
{
  "origin_id": "19058", "destination_id": "21787",
  "date": "2026-05-28", "departure": "00:00",
  "total": 44, "available": 12,
  "seats": [ { "number": "01", "available": true }, { "number": "02", "available": false } ]
}
```

### `POST /search`
Given a URL the user copied from mobifacil after browsing a route, resolve every
trip for that route/date **with its seat map**. Validates the URL (must be a
`mobifacil.com.br` `passagem-de-onibus` link) and converts the `dd-mm-yyyy` date
to `yyyy-mm-dd` internally. Drives the browser (serialised with reservations).
```bash
curl -X POST http://127.0.0.1:8771/search \
  -H 'Content-Type: application/json' \
  -d '{ "url": "https://mobifacil.com.br/passagem-de-onibus/sao-paulo-todos-sp/araraquara-sp?origin=-3&destination=19052&date=30-05-2026&isStudent=false&isPCD=false&searchValidDay=true" }'
```
```json
{
  "origin_id": "-3", "destination_id": "19052", "date": "2026-05-30",
  "trips": [
    {
      "service_id": "12345", "departure": "08:00", "arrival": "11:30",
      "company": "Empresa X", "price": "130.55", "service_class": "Executivo",
      "seats": [ { "numero": "01", "disponivel": true, "posX": 40.0, "posY": 20.0 } ]
    }
  ]
}
```
Returns **422** if the URL is not a valid mobifacil search URL. Resolving seats
for every trip means one BusDetails browser fetch per trip, so this can take a
while on routes with many departures.

> Search seats use the raw mobifacil field names (`numero`, `disponivel`,
> `posX`, `posY`) — distinct from `/seats`, which keeps its `number`/`available`
> shape.

### `POST /reservations`
Trigger a full lock flow (blocking, up to ~90 s). Body fields are all optional.
```bash
curl -X POST http://127.0.0.1:8771/reservations \
  -H 'Content-Type: application/json' \
  -d '{ "origin_id": "19058", "destination_id": "21787", "date": "2026-05-28", "departure": "00:00", "seat": "00" }'
```
Returns the reservation record. HTTP status reflects the outcome: **201** locked,
**409** seat unavailable/lock failed, **500** flow error.
On success a per-reservation re-lock job is registered (see **Persistent re-lock**).
```json
{
  "id": "f1c2…", "origin_id": "19058", "destination_id": "21787",
  "date": "2026-05-28", "departure": "00:00", "seat": "00",
  "status": "locked", "exit_code": 0,
  "created_at": "…", "updated_at": "…", "error_msg": null,
  "departure_datetime": "2026-05-28T03:00:00+00:00",
  "relock_count": 0, "is_expired": false
}
```

### `GET /reservations/{id}`
Includes `relock_count`, `departure_datetime` (UTC), and the computed `is_expired`.
```bash
curl http://127.0.0.1:8771/reservations/f1c2...   # 200 or 404
```

### `DELETE /reservations/{id}`
Cancel / forget a reservation. Also cancels its re-lock job so it won't fire again.
```bash
curl -X DELETE http://127.0.0.1:8771/reservations/f1c2...   # 200 or 404
```

### `GET /scheduler/status`
```bash
curl http://127.0.0.1:8771/scheduler/status
```
```json
{
  "running": true, "interval_minutes": 21,
  "next_run": "2026-05-30T18:42:00+00:00",
  "last_run": "2026-05-30T18:21:00+00:00", "last_exit_code": 0,
  "global_next_run": "2026-05-30T18:42:00+00:00",
  "active_relock_jobs": [
    {
      "reservation_id": "f1c2…",
      "next_run": "2026-05-30T18:42:00+00:00",
      "relock_count": 3,
      "departure_datetime": "2026-05-28T03:00:00+00:00",
      "minutes_until_departure": 38.5
    }
  ]
}
```
(`next_run` / `last_run` / `last_exit_code` / `global_next_run` describe the global
heartbeat job; `active_relock_jobs` lists each reservation's own cycle.)

---

## Environment variables

| Name                 | Default                     | Description                                              |
|----------------------|-----------------------------|----------------------------------------------------------|
| `ORIGIN_ID`          | `19058` (São Carlos)        | Origin city id used to build the search URL.             |
| `DESTINATION_ID`     | `21787` (São Paulo)         | Destination city id.                                     |
| `DATE`               | `2026-05-28`                | Travel date, `yyyy-mm-dd`.                               |
| `TARGET_DEPARTURE`   | `00:00`                     | Departure time to match on the trip card.                |
| `TARGET_SEAT`        | `00`                        | Seat number to lock.                                     |
| `BASE_URL`           | `https://mobifacil.com.br`  | Site base URL.                                           |
| `USER_DATA_DIR`      | `./browser_profile`         | Persistent Chromium profile directory.                   |
| `HEADLESS`           | `false`                     | Run Chromium headless (skips xvfb).                      |
| `ADMIN_PASSWORD`     | **(required, no default)**  | HTTP Basic password for `/admin/*` (username `admin`). Startup fails if unset. |
| `MAX_RETRIES`        | `1`                         | Attempts for retry-wrapped browser steps.                |
| `WAIT_AFTER_LOCK`    | `60`                        | Seconds the lock is held before confirmation.            |
| `SCHEDULER_INTERVAL` | `21`                        | Minutes between scheduled flows.                         |
| `LOG_FILE`           | `core/logs/app.log`         | Rotating log file path.                                  |
| `LOG_MAX_BYTES`      | `5242880`                   | Max size per log file before rotation (5 MiB).           |
| `LOG_BACKUP_COUNT`   | `3`                         | Rotated log files to keep.                               |

Named city constants (`ARARAQUARA_ID`, `SAO_CARLOS_ID`, `SAO_PAULO_ID`) are also
exported from `core/config.py` for callers.

---

## Scheduler

An APScheduler `AsyncIOScheduler` is started inside the FastAPI lifespan. It runs
two kinds of jobs, all serialised behind `FLOW_LOCK` (one browser at a time), and
shuts down gracefully (`scheduler.shutdown(wait=False)` in the lifespan `finally`).

- **Global heartbeat (`bus_flow`)** — runs the *default-config* flow every
  `SCHEDULER_INTERVAL` minutes. Its `next_run` / `last_run` / `last_exit_code`
  are reported by `GET /scheduler/status` (also as `global_next_run`).
- **Per-reservation re-lock (`relock_<id>`)** — see below.

### Persistent re-lock

The cadence matters: mobifacil seat locks expire, so each successful reservation
must keep re-locking a bit under the expiry window to hold the seat continuously.

- **One job per reservation.** When `POST /reservations` locks a seat (exit 0),
  a dedicated APScheduler job `relock_<id>` is registered that re-locks that exact
  seat **every `SCHEDULER_INTERVAL` minutes** (21 by default). Its first run is one
  interval after the initial lock.
- **Auto-expiry.** Each reservation stores `departure_datetime` (absolute **UTC**).
  When the job sees `now >= departure_datetime` it sets the status to `expired`,
  removes its own job, and stops — at that point the bus has left and no one can
  reserve the seat anymore. `relock_count` counts the successful cycles so far.
- **Manual cancellation.** `DELETE /reservations/{id}` calls `cancel_relock(id)`
  before forgetting the record, so no orphan job fires on a deleted id.
- **Failure handling.** A *soft* failure (exit 1 — seat momentarily unavailable)
  sets status `failed` but **keeps retrying** every interval in case the seat
  frees up. A *hard* failure (exit 2 — unrecoverable error) sets `failed` and
  **removes** the job so it doesn't retry forever. Unexpected exceptions are
  logged with a traceback and the job is kept.
- **Timezone.** `departure_datetime` is always stored and returned in **UTC**
  (computed from the route's `America/Sao_Paulo` local time via stdlib `zoneinfo`).
  Converting it for display is the frontend's responsibility.

Inspect every active cycle (next run, relock count, minutes until departure) via
`GET /scheduler/status` → `active_relock_jobs`.

> Note on `status` values: `pending` → `locked` (held) ↔ `failed` (soft-fail, still
> retrying), and the terminal `cancelled` (user `DELETE`) / `expired` (departure
> passed). Only `cancelled` and `expired` stop a re-lock cycle.

---

## Admin panel

All `/admin/*` routes require **HTTP Basic Auth** — username `admin`, password
from the `ADMIN_PASSWORD` env var (required; the service refuses to start without
it). Credentials are compared with `secrets.compare_digest`. A bad or missing
credential returns **401** with a `WWW-Authenticate: Basic` header.

```bash
curl -u admin:"$ADMIN_PASSWORD" http://127.0.0.1:8771/admin/stats
```

| Method | Path                       | Description                                                        |
|--------|----------------------------|--------------------------------------------------------------------|
| GET    | /admin/reservations        | List every reservation (full records).                             |
| GET    | /admin/reservations/{id}   | One reservation, or 404.                                           |
| DELETE | /admin/reservations/{id}   | Force-cancel any reservation: sets `cancelled`, stops its re-lock job, keeps the record. |
| GET    | /admin/scheduler           | Scheduler status (next_run, last_run, last_exit_code, interval).   |
| POST   | /admin/scheduler/pause     | Pause the global heartbeat job (per-reservation re-locks keep running). |
| POST   | /admin/scheduler/resume    | Resume the global heartbeat job.                                   |
| GET    | /admin/stats               | Counts: total, pending, locked, failed, cancelled, expired.        |
| POST   | /admin/shutdown            | Graceful shutdown (see below).                                     |

```json
// GET /admin/stats
{ "total": 5, "pending": 0, "locked": 2, "failed": 1, "cancelled": 1, "expired": 1 }
```

### `POST /admin/shutdown`

Refuses with **409** if any reservation is still `pending` or `locked`:
```json
{ "error": "active_reservations", "count": 2, "message": "Cannot shut down: 2 reservation(s) still active." }
```
Otherwise responds **200** `{ "status": "shutting_down" }` and, after a 1-second
delay (a FastAPI `BackgroundTasks` job, so the response is delivered first), sends
`SIGTERM` to its own pid so systemd can stop/restart it cleanly. The trigger is
logged at WARNING.

> **systemd restart tip:** a clean `SIGTERM` is not a failure, so
> `Restart=on-failure` will **not** bring the service back after
> `/admin/shutdown`. Use `Restart=always` in the unit if you want it to
> auto-restart (e.g. to recycle the browser profile) after an admin shutdown.

---

## Notes on anti-bot evasion

mobifacil fingerprints clients aggressively, so the flow deliberately looks human:

- **xvfb (virtual display).** The site behaves differently under headless
  Chromium, so we run a *headed* browser on a virtual X display via `xvfb-run`.
  This is why Linux deployment needs `xvfb` and `start.sh` wraps uvicorn in it.
- **Persistent profile.** `USER_DATA_DIR` keeps cookies (`dw*`, `sid`) and the
  device fingerprint stable across runs, exactly like a returning human user. A
  fresh/empty profile trips detection, so the profile is validated and only
  reset on repeated failures.
- **`slow_mo` + stochastic idle.** Randomised `slow_mo` (20–80 ms), mouse moves,
  wheel scrolls and jittered sleeps spread actions out over human-plausible
  timings instead of firing instantly.
- **Init-script spoofing.** `navigator.webdriver` is hidden, WebGL vendor/renderer
  are spoofed, `plugins`/`languages` are populated, and the locale/timezone are
  pinned to `pt-BR` / `America/Sao_Paulo`.

Run responsibly and only against targets you are authorised to automate.
