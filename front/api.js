/* ============================================================
   BUSBOOKER API CLIENT  (window.BB)
   Same-origin relative-URL fetch to the FastAPI backend.
   Defensive error parsing for both FastAPI shapes + the special
   cases documented in the API contract:
     POST /reservations → ReservationRecord on 409/500
     POST /admin/shutdown → {error,count,message} on 409
   ============================================================ */
(function () {
  "use strict";

  // Reservation: browser-driven, can take up to ~90s. Never abort early.
  const BROWSER_TIMEOUT = 125000;
  // Search + seats: plain httpx calls server-side, ~2-5s expected.
  const HTTP_TIMEOUT = 30000;
  const READ_TIMEOUT = 15000;

  class ApiError extends Error {
    constructor(status, message, opts = {}) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.retryAfter = opts.retryAfter ?? null;
      this.validation = opts.validation ?? null;
      this.raw = opts.raw ?? null;
    }
  }

  function parseDetail(body, fallback) {
    if (!body) return fallback || "unknown error";
    const d = body.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      return d
        .map((e) => {
          const loc = Array.isArray(e.loc) ? e.loc.filter((x) => x !== "body").join(".") : "";
          return (loc ? loc + ": " : "") + (e.msg || "invalid");
        })
        .join("  ·  ");
    }
    if (body.message) return body.message; // shutdown 409 shape
    return fallback || "unknown error";
  }

  async function http(method, path, opts = {}) {
    const headers = {};
    if (opts.body) headers["Content-Type"] = "application/json";
    if (opts.auth != null) headers["Authorization"] = "Basic " + btoa("admin:" + opts.auth);

    const ctrl = new AbortController();
    const to = setTimeout(() => ctrl.abort(), opts.timeout || READ_TIMEOUT);
    let resp;
    try {
      resp = await fetch(path, {
        method,
        headers,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
        signal: ctrl.signal,
      });
    } catch (e) {
      clearTimeout(to);
      const msg = e && e.name === "AbortError" ? "request timed out" : "network error,  backend unreachable";
      throw new ApiError(0, msg);
    }
    clearTimeout(to);

    const raHeader = resp.headers.get("Retry-After");
    const retryAfter = raHeader ? parseInt(raHeader, 10) : null;
    let data = null;
    try { data = await resp.json(); } catch (_) { /* no/invalid body */ }
    return { resp, data, status: resp.status, retryAfter: Number.isNaN(retryAfter) ? null : retryAfter };
  }

  function backpressure(status, retryAfter) {
    if (status === 503) return new ApiError(503, "server busy", { retryAfter: retryAfter ?? 30 });
    if (status === 429) return new ApiError(429, "rate limited", { retryAfter: retryAfter ?? 60 });
    return null;
  }

  async function adminGet(path, auth) {
    const r = await http("GET", path, { auth, timeout: READ_TIMEOUT });
    if (r.status === 401) throw new ApiError(401, parseDetail(r.data, "invalid credentials"));
    if (r.status !== 200) throw new ApiError(r.status, parseDetail(r.data, "request failed"));
    return r.data;
  }

  const BB = {
    ApiError,
    parseDetail,

    async health() {
      const { data, status } = await http("GET", "/health", { timeout: 8000 });
      if (status !== 200) throw new ApiError(status, parseDetail(data, "health check failed"));
      return data;
    },

    async search(url) {
      const r = await http("POST", "/search", { body: { url }, timeout: HTTP_TIMEOUT });
      const bp = backpressure(r.status, r.retryAfter);
      if (bp) throw bp;
      if (r.status !== 200) throw new ApiError(r.status, parseDetail(r.data, "search failed"), {
        validation: Array.isArray(r.data && r.data.detail) ? r.data.detail : null,
      });
      return r.data;
    },

    async getSeats(p) {
      const q = new URLSearchParams({
        origin_id: p.origin_id, destination_id: p.destination_id, date: p.date, departure: p.departure,
      });
      const r = await http("GET", "/seats?" + q.toString(), { timeout: HTTP_TIMEOUT });
      const bp = backpressure(r.status, r.retryAfter);
      if (bp) throw bp;
      if (r.status !== 200) throw new ApiError(r.status, parseDetail(r.data, "seat map failed"));
      return r.data;
    },

    /* returns {status, record}; throws only on 422 / 503 / 429 / network */
    async createReservation(body) {
      const r = await http("POST", "/reservations", { body, timeout: BROWSER_TIMEOUT });
      const bp = backpressure(r.status, r.retryAfter);
      if (bp) throw bp;
      if (r.status === 201 || r.status === 409 || r.status === 500) {
        return { status: r.status, record: r.data };
      }
      throw new ApiError(r.status, parseDetail(r.data, "reservation failed"), {
        validation: Array.isArray(r.data && r.data.detail) ? r.data.detail : null,
      });
    },

    async getReservation(id) {
      const r = await http("GET", "/reservations/" + encodeURIComponent(id), { timeout: READ_TIMEOUT });
      if (r.status === 404) throw new ApiError(404, "reservation not found");
      if (r.status !== 200) throw new ApiError(r.status, parseDetail(r.data, "lookup failed"));
      return r.data;
    },

    async deleteReservation(id) {
      const r = await http("DELETE", "/reservations/" + encodeURIComponent(id), { timeout: READ_TIMEOUT });
      if (r.status === 404) throw new ApiError(404, "already gone");
      if (r.status !== 200) throw new ApiError(r.status, parseDetail(r.data, "cancel failed"));
      return r.data;
    },

    async schedulerStatus() {
      const r = await http("GET", "/scheduler/status", { timeout: READ_TIMEOUT });
      if (r.status !== 200) throw new ApiError(r.status, parseDetail(r.data, "scheduler unavailable"));
      return r.data;
    },

    async adminStats(auth)        { return adminGet("/admin/stats", auth); },
    async adminReservations(auth) { return adminGet("/admin/reservations", auth); },
    async adminScheduler(auth)    { return adminGet("/admin/scheduler", auth); },

    async adminDelete(id, auth) {
      const r = await http("DELETE", "/admin/reservations/" + encodeURIComponent(id), { auth, timeout: READ_TIMEOUT });
      if (r.status === 401) throw new ApiError(401, parseDetail(r.data, "invalid credentials"));
      if (r.status === 404) throw new ApiError(404, "not found");
      if (r.status !== 200) throw new ApiError(r.status, parseDetail(r.data, "force-cancel failed"));
      return r.data;
    },

    async adminShutdown(auth) {
      const r = await http("POST", "/admin/shutdown", { auth, timeout: READ_TIMEOUT });
      if (r.status === 401) throw new ApiError(401, parseDetail(r.data, "invalid credentials"));
      if (r.status === 409) throw new ApiError(409, parseDetail(r.data, "active reservations"), { raw: r.data });
      if (r.status !== 200) throw new ApiError(r.status, parseDetail(r.data, "shutdown failed"));
      return r.data;
    },
  };

  window.BB = BB;
})();
