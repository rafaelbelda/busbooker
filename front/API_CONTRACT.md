# Frontend Integration Contract — busbooker

Technical contract for the frontend that consumes the `core/` FastAPI backend.
**Design and visuals are out of scope here** — this document only specifies how
to talk to the API correctly. If anything below blocks a design decision, ask;
don't guess.

The authoritative, always-current schema is the backend's OpenAPI document:

- Swagger UI: `GET /docs`
- ReDoc: `GET /redoc`
- Machine-readable: `GET /openapi.json` ← **generate your typed client from this.**

If this document and `/openapi.json` ever disagree, `/openapi.json` wins.

---

## 0. Read this first — non-obvious constraints

These will break the UI if ignored. They are not optional.

1. **Browser-driven endpoints are slow and serialized.** `POST /search`,
   `POST /reservations`, and `GET /seats` each drive a real headless browser on
   the server. They are **synchronous** (the HTTP response is sent only when the
   flow finishes) and can take **up to ~90 s** (`/reservations`) or **longer**
   for `/search` (one page fetch *per trip*). The server runs **one browser flow
   at a time** behind a global lock — concurrent calls **queue** server-side, they
   do not run in parallel. See §3.

2. **CORS is not configured.** The backend has **no CORS middleware**. A browser
   app served from a *different origin* than the API will be blocked. You must
   either (a) serve the frontend **same-origin** behind the same nginx (preferred),
   or (b) ask the backend team to add `CORSMiddleware` for your origin. See §2.

3. **State is in-memory and non-persistent.** Reservations live in RAM. A server
   restart, deploy, crash, or `POST /admin/shutdown` **wipes all reservations and
   their re-lock jobs**. Reservation IDs are **not durable** — never treat them as
   permanent. The UI must tolerate a reservation simply disappearing.

4. **Times are UTC, ISO-8601.** Every datetime field (`created_at`, `updated_at`,
   `departure_datetime`, `next_run`, …) is UTC with offset (e.g.
   `2026-05-28T03:00:00+00:00`). Convert to the user's local zone for display.
   Bus times are local to `America/Sao_Paulo`; the backend already converted them.

5. **IDs and seats are strings, not numbers.** `origin_id`, `destination_id`,
   `seat`, `numero` are **strings** and may carry **leading zeros** (`seat: "00"`).
   Never coerce to integer. `date` is `"yyyy-mm-dd"`, `departure`/`arrival` are
   `"HH:MM"`.

6. **No realtime.** No WebSocket/SSE. Track progress by **polling** (see §6).

7. **`POST /reservations` is not idempotent.** Each call creates a new record and
   runs a full flow. Double-submit → duplicate reservations + duplicate re-lock
   jobs for the same seat. **Disable the submit control while a call is in flight.**

---

## 1. Base URL, transport, headers

- All endpoints are served at the **root** of the API host (no `/api` prefix).
- Request bodies: `Content-Type: application/json`.
- Responses: JSON (`application/json`), gzipped by nginx.
- Reserved top-level paths owned by the API (do **not** let the SPA router claim
  these): `/health`, `/seats`, `/search`, `/reservations`, `/reservations/{id}`,
  `/scheduler/status`, `/admin/*`, `/docs`, `/redoc`, `/openapi.json`.

### Client timeouts
Set your HTTP client timeout to **≥ 120 s** for the three browser-driven calls
(nginx `proxy_read_timeout` is 120 s). With `fetch`, use an `AbortController` but
**do not abort early** — a 30 s default will kill a valid in-progress lock. Health
and read-only calls can use a short timeout.

---

## 2. Deployment / origin (CORS)

The API does not send CORS headers. Pick one:

- **Same-origin (recommended).** Serve the built frontend from the same nginx that
  proxies the API. nginx routes the reserved API paths (§1) to `127.0.0.1:8771`
  and serves your static bundle for everything else. Then the frontend calls the
  API with **relative URLs** (`fetch('/reservations', …)`) and CORS never applies.
- **Separate origin.** If the frontend is hosted elsewhere (separate domain/port),
  the backend must add `CORSMiddleware` allowing your exact origin (and, for
  `/admin`, `allow_credentials=True`). **Tell the backend team your origin** so it
  can be added — it is not there yet.

Production note: `/admin` uses HTTP Basic Auth, which sends credentials in
base64. **Serve over HTTPS** so they aren't exposed.

---

## 3. Concurrency model (what the UI must enforce)

- One browser flow runs at a time (server-side global lock). If the user triggers
  a second browser-bound call (`/search`, `/reservations`, `/seats`) while one is
  running, it **waits its turn** — the request just takes longer.
- The UI should **serialize browser-bound actions**: disable the relevant buttons
  and show a "working…" state until the in-flight call returns. Avoid firing
  `/search` and `/reservations` simultaneously.
- Read-only endpoints (`/health`, `/scheduler/status`, `GET /reservations/{id}`,
  all non-mutating `/admin/*` reads) are fast and safe to call any time, including
  while a browser flow runs.

---

## 4. Endpoint reference

Status codes and exact body shapes. `→` denotes the success body.

### Public

| Method | Path | Purpose | Success | Notes |
|---|---|---|---|---|
| GET | `/health` | liveness | 200 → `HealthResponse` | fast |
| GET | `/seats` | live seat map for a route | 200 → `SeatsResponse` | browser-driven; **required** query params (`origin_id`, `destination_id`, `date`, `departure`) |
| POST | `/search` | resolve trips+seats from a pasted mobifacil URL | 200 → `SearchResponse` | browser-driven; 422 on bad URL |
| POST | `/reservations` | lock a seat now + start re-lock cycle | **201** → `ReservationRecord` | browser-driven; see status semantics below |
| GET | `/reservations/{id}` | reservation detail | 200 → `ReservationRecord` | 404 if unknown |
| DELETE | `/reservations/{id}` | forget reservation + stop its re-lock | 200 → `MessageResponse` | 404 if unknown |
| GET | `/scheduler/status` | scheduler + per-reservation jobs | 200 → `SchedulerStatusResponse` | fast |

### Admin (HTTP Basic Auth — see §5)

| Method | Path | Purpose | Success |
|---|---|---|---|
| GET | `/admin/reservations` | list **all** reservations | 200 → `ReservationRecord[]` |
| GET | `/admin/reservations/{id}` | one reservation | 200 → `ReservationRecord` (404 if unknown) |
| DELETE | `/admin/reservations/{id}` | force-cancel (keeps record) | 200 → `ReservationRecord` (status `cancelled`) |
| GET | `/admin/scheduler` | basic scheduler status | 200 → `{ running, interval_minutes, active_relock_count }` (**no** `active_relock_jobs`) |
| GET | `/admin/stats` | counts by status | 200 → `AdminStats` |
| POST | `/admin/shutdown` | graceful SIGTERM | 200 → `{ "status": "shutting_down" }` / 409 (see below) |

> `/admin/scheduler` returns the basic status **without** `active_relock_jobs`.
> For the per-reservation job list, use the public `GET /scheduler/status`.

#### `POST /reservations` status semantics (important)
The response **body is always a `ReservationRecord`** (read `status`/`exit_code`/
`error_msg` from it) — even on 409/500. Only request-validation failures return
the FastAPI error shape (§7).

| HTTP | `status` | Meaning | UI treatment |
|---|---|---|---|
| 201 | `locked` | seat locked, re-lock cycle started | success |
| 409 | `failed` | seat unavailable / lock failed | **normal business outcome**, not a crash — "seat taken, try another" |
| 500 | `failed` | unrecoverable flow error | error; offer retry |
| 422 | — | bad request body (§7) | fix the request |

#### `POST /admin/shutdown` 409 body (distinct shape)
```json
{ "error": "active_reservations", "count": 2,
  "message": "Cannot shut down: 2 reservation(s) still active." }
```
Returned when any reservation is `pending` or `locked`. Note this is **not** the
`{ "detail": … }` shape. On 200, the process SIGTERMs itself ~1 s later, so the
API will stop responding shortly after — expect connection loss and surface it.

---

## 5. Admin authentication

- Scheme: **HTTP Basic**. Username is fixed **`admin`**; password is the backend's
  `ADMIN_PASSWORD`.
- Send `Authorization: Basic base64("admin:" + password)` on **every** `/admin/*`
  request.
- On failure: **401** with header `WWW-Authenticate: Basic` and body
  `{ "detail": "invalid credentials" }` (or `"Not authenticated"` when no header
  was sent).
- The browser may show a native Basic-Auth popup on a bare 401. For a custom admin
  UI, send the header yourself and handle 401 in-app. **Do not** hardcode or commit
  the password; prompt for it and keep it in memory for the session (avoid
  `localStorage`). Requires HTTPS in production.

---

## 6. Data models

Field names and types are exact. Generate types from `/openapi.json` rather than
hand-copying when possible.

### `ReservationRecord`
```jsonc
{
  "id": "f1c2…",                 // string UUID
  "origin_id": "19058",          // string
  "destination_id": "21787",     // string
  "date": "2026-05-28",          // yyyy-mm-dd
  "departure": "00:00",          // HH:MM
  "seat": "00",                  // string (may have leading zeros)
  "status": "locked",            // see enum below
  "exit_code": 0,                // 0 locked | 1 unavailable/failed | 2 error | null while pending
  "created_at": "…Z",            // UTC ISO-8601
  "updated_at": "…Z",            // UTC ISO-8601
  "error_msg": null,             // string | null
  "departure_datetime": "2026-05-28T03:00:00+00:00", // UTC | null (set once locked)
  "relock_count": 0,             // integer, increments per successful re-lock
  "is_expired": false            // computed: now >= departure_datetime
}
```

**`status` enum:** `pending` | `locked` | `failed` | `cancelled` | `expired`.
- `pending` — created, flow in progress (transient; you'll rarely observe it on a
  synchronous create, but it's the initial value).
- `locked` — seat held; a re-lock job runs every `interval_minutes`.
- `failed` — last attempt failed. **Soft fail (exit 1) keeps retrying** every
  interval (status can flip back to `locked` later). **Hard fail (exit 2) stops.**
- `cancelled` — terminal; user `DELETE` (public delete removes the record; admin
  delete marks it `cancelled` and keeps it).
- `expired` — terminal; departure passed, re-lock stopped.

### `SeatsResponse` (from `GET /seats`)
```jsonc
{ "origin_id": "19058", "destination_id": "21787", "date": "2026-05-28",
  "departure": "00:00", "total": 44, "available": 12,
  "seats": [ { "number": "01", "available": true } ] }   // note: number/available
```

### `SearchResponse` / `TripResult` / `TripSeat` (from `POST /search`)
```jsonc
{
  "origin_id": "-3", "destination_id": "19052", "date": "2026-05-30",
  "trips": [
    {
      "service_id": "12345",
      "departure": "08:00", "arrival": "11:30",
      "company": "Empresa X", "price": "130.55", "service_class": "Executivo",
      "seats": [
        { "numero": "01", "disponivel": true, "posX": 40.0, "posY": 20.0 }
      ]
    }
  ]
}
```
**Seat field names differ from `/seats`.** Search seats use the raw site fields
`numero` (string), `disponivel` (bool), `posX`/`posY` (floats, for laying out the
seat grid — relative coordinates, origin top-left). `/seats` uses
`number`/`available`. Don't mix them.

### `SchedulerStatusResponse` (from `GET /scheduler/status`)
```jsonc
{
  "running": true, "interval_minutes": 21,
  "active_relock_count": 1,                                  // == active_relock_jobs.length
  "active_relock_jobs": [
    { "reservation_id": "f1c2…", "next_run": "…", "relock_count": 3,
      "departure_datetime": "…", "minutes_until_departure": 38.5 }  // float, can be negative
  ]
}
```
There is no global/heartbeat job — the scheduler only runs per-reservation re-lock
jobs, listed in `active_relock_jobs`. (`next_run` here is each re-lock job's own
next fire time.)

### `AdminStats` (from `GET /admin/stats`)
```jsonc
{ "total": 5, "pending": 0, "locked": 2, "failed": 1, "cancelled": 1, "expired": 1 }
```

### `HealthResponse` / `MessageResponse`
```jsonc
{ "status": "ok", "uptime_seconds": 42.1, "scheduler_running": true }
{ "detail": "reservation <id> cancelled" }
```

### Request bodies
- `POST /reservations` — `ReservationRequest`, **all fields required**, unknown
  fields **rejected** (`extra: forbid` → 422). Required keys exactly:
  `origin_id`, `destination_id`, `date` (`yyyy-mm-dd`), `departure` (`HH:MM`),
  `seat` (string). There are no server-side route defaults — omitting any field
  is a 422.
- `POST /search` — `{ "url": "<full mobifacil passagem-de-onibus URL>" }`. Paste
  the URL **exactly** as copied (it contains `dd-mm-yyyy` date + `origin`/
  `destination`/`isStudent`/`isPCD` query params; the backend parses it). Only
  `mobifacil.com.br` `passagem-de-onibus` URLs are accepted (else 422).
- `GET /seats` — **required** query params `origin_id`, `destination_id`, `date`
  (`yyyy-mm-dd`), `departure` (`HH:MM`). No `seat` param (a seat-map read is not
  seat-specific); no defaults — omitting any is a 422. `date` and `departure` are
  format-validated (a `{ "detail": "<string>" }` 422 on a bad value). The same
  format validation applies to `POST /reservations` fields (array-shaped 422).

---

## 7. Error handling

Two body shapes exist — handle both:

- **FastAPI errors** (`404`, `500`, `/search` `422` for bad URL, `401`):
  `{ "detail": "<string>" }`.
- **Validation errors** (`422` from a malformed JSON body): `{ "detail": [ {
  "type", "loc", "msg", "input" } ] }` — `detail` is an **array**.
- **Special cases that are NOT `{detail}`:** `POST /reservations` 409/500 return a
  full `ReservationRecord`; `POST /admin/shutdown` 409 returns
  `{ error, count, message }`.

Recommended handling: parse `detail` defensively (string vs array); treat
`POST /reservations` 409 as a business result (read the record), not an exception;
never auto-retry browser-driven calls (they're expensive and serialized — let the
user retry explicitly).

---

## 8. Recommended flows

### A. Search → pick → reserve
1. User pastes the mobifacil URL → `POST /search`. Show a long-running spinner
   (this can take a while; see §0.1).
2. Render `trips[]`; for the chosen trip render its `seats[]` (use `posX`/`posY`
   for the seat-grid layout; gray out `disponivel: false`).
3. On seat selection, build the reservation body by **carrying values from the
   search response**:
   `origin_id` = `SearchResponse.origin_id`,
   `destination_id` = `SearchResponse.destination_id`,
   `date` = `SearchResponse.date`,
   `departure` = chosen `TripResult.departure`,
   `seat` = chosen `TripSeat.numero`.
4. `POST /reservations` (disable submit until it returns). Branch on status per §4.

### B. Monitor a reservation
- After a `201`, poll `GET /reservations/{id}` (e.g. every 20–30 s) to reflect
  `status`, `relock_count`, and `is_expired`. Or poll `GET /scheduler/status` and
  match on `active_relock_jobs[].reservation_id` for `next_run` /
  `minutes_until_departure`.
- Stop polling when `status` is `cancelled`/`expired`, when `is_expired` is true,
  or on `404` (record gone — e.g. server restarted; see §0.3).

### C. Cancel
- `DELETE /reservations/{id}` → 200 `MessageResponse`. This stops the re-lock job
  and forgets the record. Treat `404` as "already gone" (idempotent from the UI's
  view).

### D. Admin dashboard
- `GET /admin/stats` for counts; `GET /admin/reservations` for the table;
  `GET /admin/scheduler` (+ public `/scheduler/status` for `active_relock_jobs`);
  `POST /admin/shutdown` with a confirm dialog (handle the 409 active-reservations
  refusal, and expect the API to go away on 200).

---

## 9. Polling cadence & health

- `GET /health` for a connectivity/uptime indicator (cheap, call freely).
- Keep poll intervals modest (≥ ~15 s). The backend re-locks every
  `interval_minutes` (21 by default), so sub-minute polling adds no signal.
- There is no auth on public endpoints and no rate limiting — be a good citizen
  and don't hammer the browser-driven endpoints.
