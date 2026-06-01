/* ============================================================
   BUSBOOKER — MONITOR module
   Live reservation telemetry: ticking countdowns, availability
   strip-chart, teletype event console, and a live bus-occupancy
   preview (seats sell off in SIM; manual rescan in LIVE).
   Uses window.BBUI (helpers) + window.BB (api).
   ============================================================ */
(function () {
  "use strict";
  const U = window.BBUI;
  const { el, $, pad2, banner, errBanner, kv, kvSeg, buildSeatMap, normSeat } = U;

  let M = null; // monitor session

  function stop() {
    if (M) { (M.timers || []).forEach(clearInterval); }
    M = null;
  }
  window.stopMonitor = stop;

  /* ---------- time formatting ---------- */
  function cd(targetMs) {
    if (targetMs == null) return { txt: "--:--:--", neg: false };
    let s = Math.round((targetMs - Date.now()) / 1000);
    const neg = s < 0; s = Math.abs(s);
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60); s -= m * 60;
    const core = (d > 0 ? d + "d " : "") + `${pad2(h)}:${pad2(m)}:${pad2(s)}`;
    return { txt: (neg ? "-" : "") + core, neg };
  }
  function cdShort(targetMs) {
    if (targetMs == null) return "--:--";
    let s = Math.max(0, Math.round((targetMs - Date.now()) / 1000));
    const m = Math.floor(s / 60); s -= m * 60;
    return `${pad2(m)}:${pad2(s)}`;
  }
  const nowClock = () => { const d = new Date(); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`; };

  /* ---------- human-readable progress line ---------- */
  function progressLine(rec) {
    switch (rec.status) {
      case "pending": return ["WORKING", "Lock in progress — driving the provider browser…"];
      case "locked": return ["NOMINAL", `Seat held. Auto re-locking to keep it until departure.`];
      case "failed":
        return rec.exit_code === 2
          ? ["HARD FAULT", "Unrecoverable error — re-lock cycle stopped. Re-reserve to retry."]
          : ["SOFT FAULT", "Last re-lock failed — scheduler will retry next interval."];
      case "expired": return ["EXPIRED", "Bus has departed — monitoring stopped."];
      case "cancelled": return ["CANCELLED", "Reservation cancelled — re-lock job removed."];
      default: return ["—", ""];
    }
  }

  /* ====================================================================
     SHELL
     ==================================================================== */
  window.renderMonitor = function () {
    const root = $("#view-monitor");
    root.innerHTML = "";
    const input = el("input", { type: "text", id: "monitorId", value: U.state.monitorId, placeholder: "reservation id (uuid)", autocapitalize: "off", autocomplete: "off", spellcheck: "false" });
    const track = el("button", { class: "btn", type: "button", text: "◎ Track" });
    root.append(
      el("div", { class: "section" }, [
        el("div", { class: "legend" }, [el("span", { class: "idx", text: "01" }), "Reservation lookup"]),
        el("div", { class: "field" }, [el("label", { for: "monitorId", text: "Reservation ID" }), input]),
        track,
      ]),
      el("div", { id: "monitorBody", class: "spaced" })
    );
    track.addEventListener("click", () => { const id = input.value.trim(); if (id) { U.saveMonitorId(id); start(id); } });
    if (U.state.monitorId) start(U.state.monitorId);
  };

  /* ====================================================================
     SESSION
     ==================================================================== */
  async function start(id) {
    stop();
    const body = $("#monitorBody");
    if (!body) return;
    body.innerHTML = "";
    body.appendChild(banner("proc", "ACQUIRING TELEMETRY…", "reading reservation record", true));
    M = { id, rec: null, sched: null, job: null, depMs: null, relockMs: null, timers: [], lastRelock: -1, built: false,
          seats: null, total: 0, avail: 0, history: [], logEl: null };
    await poll(true);
    if (!M) return;
    if (M.dead) return;
    buildBody();
    update();
    M.timers.push(setInterval(() => poll(false), 20000));
    M.timers.push(setInterval(tick, 1000));
  }

  async function poll(first) {
    if (!M) return;
    try {
      const rec = await BB.getReservation(M.id);
      M.rec = rec;
      M.depMs = rec.departure_datetime ? new Date(rec.departure_datetime).getTime() : null;
    } catch (e) {
      stop();
      const body = $("#monitorBody"); if (!body) return;
      body.innerHTML = "";
      body.appendChild(e instanceof BB.ApiError && e.status === 404
        ? banner("warn", "RECORD GONE · 404", "reservation no longer on the server (deleted or lost on restart)")
        : errBanner(e));
      M = { dead: true };
      return;
    }
    try {
      const s = await BB.schedulerStatus();
      M.sched = s;
      M.job = (s.active_relock_jobs || []).find((j) => j.reservation_id === M.id) || null;
      M.relockMs = M.job && M.job.next_run ? new Date(M.job.next_run).getTime() : null;
    } catch (_) { /* read-only, ignore */ }

    if (!first && M.built) {
      // log re-lock progression
      if (M.rec.relock_count > M.lastRelock && M.lastRelock >= 0) {
        appendLog(`RE-LOCK #${M.rec.relock_count} OK · seat ${M.rec.seat} held`, "ok");
      }
      update();
    }
    M.lastRelock = M.rec.relock_count;

    if (M.rec.status === "cancelled" || M.rec.status === "expired" || M.rec.is_expired) {
      // stop the record poll but keep the 1 s ticker for a final readout
      if (M.timers[0]) { clearInterval(M.timers[0]); M.timers[0] = null; }
    }
  }

  /* ====================================================================
     BODY (stable panels)
     ==================================================================== */
  function buildBody() {
    const body = $("#monitorBody");
    body.innerHTML = "";

    // ---- PRIMARY: status + countdowns ----
    const primary = el("div", { class: "section" }, [
      el("div", { class: "legend" }, [el("span", { class: "idx", text: "02" }), "Telemetry"]),
      el("div", { class: "statusarray", id: "mon-lamps" }),
      el("div", { class: "progline", id: "mon-prog" }),
      el("div", { class: "twocount" }, [
        el("div", { class: "bigcount" }, [el("div", { class: "lab", text: "Time to departure" }), el("div", { class: "val", id: "mon-dep", text: "--:--:--" })]),
        el("div", { class: "bigcount sm" }, [el("div", { class: "lab", text: "Next re-lock" }), el("div", { class: "val", id: "mon-relock", text: "--:--" })]),
      ]),
      el("div", { class: "readout", style: "margin-top:4px" }, [
        rowSeg("Seat", "mon-seat"), rowSeg("Re-lock count", "mon-rc"),
        row("Status", "mon-status"), row("Interval", "mon-int"),
      ]),
    ]);

    // ---- BUS OCCUPANCY ----
    const scanBtn = el("button", { class: "btn verb browser-action", id: "mon-scan", type: "button", text: "◎ Scan bus occupancy" });
    const bus = el("div", { class: "section" }, [
      el("div", { class: "legend" }, [el("span", { class: "idx", text: "03" }), "Bus occupancy"]),
      el("p", { class: "note", id: "mon-busnote", text: "Live read of the current seat map for this route. Rescan to refresh." }),
      scanBtn,
      el("div", { id: "mon-busbody", class: "spaced", style: "margin-top:12px" }),
    ]);
    scanBtn.addEventListener("click", scan);

    // ---- TELETYPE CONSOLE ----
    const con = el("div", { class: "section" }, [
      el("div", { class: "legend" }, [el("span", { class: "idx", text: "04" }), "Event log"]),
      el("div", { class: "teletype", id: "mon-tty" }),
    ]);

    // ---- MANIFEST ----
    const manifest = el("div", { class: "section" }, [
      el("div", { class: "legend" }, [el("span", { class: "idx", text: "05" }), "Manifest"]),
      el("div", { class: "readout", id: "mon-manifest" }),
    ]);

    // ---- CONTROLS ----
    const refresh = el("button", { class: "btn verb sm", type: "button", text: "↻ Refresh" });
    const cancel = el("button", { class: "btn danger sm", id: "mon-cancel", type: "button", text: "✕ Cancel reservation" });
    const ctrlStatus = el("div", { class: "spaced", style: "margin-top:10px" });
    const ctrl = el("div", { class: "section" }, [el("div", { class: "btnrow" }, [refresh, cancel]), ctrlStatus]);
    refresh.addEventListener("click", () => start(M.id));
    cancel.addEventListener("click", () => doCancel(ctrlStatus, cancel));

    body.append(primary, bus, con, manifest, ctrl);
    M.built = true;
    M.logEl = $("#mon-tty");

    // seed the event log from the record
    const c = M.rec;
    appendLog(`RESERVATION ${c.id.slice(0, 8)} ACQUIRED`, "");
    appendLog(`CREATED · seat ${c.seat} · route ${c.origin_id}→${c.destination_id}`, "");
    if (c.status === "locked") appendLog(`SEAT LOCKED · exit ${c.exit_code} · cycle armed`, "ok");
    if (c.relock_count > 0) appendLog(`${c.relock_count} re-lock cycle(s) on record`, "");
    if (c.status === "failed") appendLog(`LAST ATTEMPT FAILED · ${c.error_msg || "exit " + c.exit_code}`, c.exit_code === 2 ? "bad" : "warn");
  }
  const row = (label, id) => el("div", { class: "kv" }, [el("span", { class: "k", text: label }), el("span", { class: "v", id })]);
  const rowSeg = (label, id) => el("div", { class: "kv" }, [el("span", { class: "k", text: label }), el("span", { class: "v seg7", id })]);

  /* ====================================================================
     UPDATE (from record/scheduler) — every poll
     ==================================================================== */
  const STATUSES = ["pending", "locked", "failed", "cancelled", "expired"];
  function update() {
    if (!M || !M.rec) return;
    const r = M.rec;
    // lamps
    const lamps = $("#mon-lamps");
    if (lamps) {
      lamps.innerHTML = "";
      STATUSES.forEach((st) => lamps.appendChild(el("div", { class: "slamp s-" + st + (r.status === st ? " lit" : "") }, [el("span", { class: "b" }), el("span", { class: "t", text: st })])));
    }
    // progress line
    const [tag, msg] = progressLine(r);
    const prog = $("#mon-prog");
    if (prog) { prog.className = "progline " + tag.toLowerCase().replace(/\s+/g, "-"); prog.innerHTML = ""; prog.append(el("span", { class: "ptag", text: tag }), el("span", { class: "pmsg", text: msg })); }
    // readouts
    setText("mon-seat", r.seat);
    setText("mon-rc", String(r.relock_count));
    setText("mon-status", r.status.toUpperCase() + (r.is_expired ? " · DEPARTED" : ""));
    setText("mon-int", M.sched ? M.sched.interval_minutes + " min" : "—");
    // manifest
    const man = $("#mon-manifest");
    if (man) {
      man.innerHTML = "";
      man.append(
        kv("ID", el("span", { class: "v wrap", text: r.id })),
        kv("Route", el("span", { class: "v", text: r.origin_id + " → " + r.destination_id })),
        kv("Date · dep (SP)", el("span", { class: "v", text: r.date + " · " + r.departure })),
        kv("Departure (local)", el("span", { class: "v", text: r.departure_datetime ? U.fmtLocal(r.departure_datetime) : "— set on lock" })),
        kv("Exit code", el("span", { class: "v", text: r.exit_code == null ? "—" : String(r.exit_code) })),
        kv("Created", el("span", { class: "v", text: U.fmtLocal(r.created_at) })),
        kv("Updated", el("span", { class: "v", text: U.fmtLocal(r.updated_at) })),
        r.error_msg ? kv("Error", el("span", { class: "v wrap", style: "color:var(--lamp-red)", text: r.error_msg })) : null
      );
    }
    // cancel availability
    const cancel = $("#mon-cancel");
    if (cancel) cancel.disabled = r.status === "cancelled" || r.status === "expired";
    tick();
  }
  function setText(id, v) { const n = $("#" + id); if (n) n.textContent = v; }

  /* ====================================================================
     TICK (1 s) — live countdowns
     ==================================================================== */
  function tick() {
    if (!M || !M.rec) return;
    const dep = $("#mon-dep");
    if (dep) {
      if (!M.depMs) { dep.textContent = "--:--:--"; dep.className = "val"; }
      else {
        const remain = Math.round((M.depMs - Date.now()) / 1000);
        const c = cd(M.depMs);
        dep.textContent = c.neg ? "DEPARTED" : c.txt;
        dep.className = "val" + (c.neg ? " bad" : remain < 1800 ? " warn" : "");
      }
    }
    const rl = $("#mon-relock");
    if (rl) {
      const terminal = M.rec.status === "cancelled" || M.rec.status === "expired";
      rl.textContent = terminal ? "—" : cdShort(M.relockMs);
    }
  }

  /* ====================================================================
     BUS OCCUPANCY (scan + live drift)
     ==================================================================== */
  async function scan() {
    if (!M || !M.rec) return;
    const r = M.rec;
    const note = $("#mon-busnote"); const out = $("#mon-busbody");
    out.innerHTML = "";
    U.setBrowserBusy(true);
    out.appendChild(banner("proc", "READING SEAT MAP…", "fetching live seat data", true));
    appendLog("SEAT-MAP SCAN REQUESTED", "");
    try {
      const res = await BB.getSeats({ origin_id: r.origin_id, destination_id: r.destination_id, date: r.date, departure: r.departure });
      // SeatInfo shape: {number, available} — normSeat handles both this and TripSeat.
      M.seats = res.seats || [];
      M.total = res.total; M.avail = res.available;
      M.history = [res.available];
      out.innerHTML = "";
      renderBus(out);
      appendLog(`SCAN COMPLETE · ${res.available}/${res.total} seats free`, "ok");
    } catch (e) {
      out.innerHTML = ""; out.appendChild(errBanner(e));
      appendLog("SCAN FAILED · " + (e.message || "error"), "bad");
    } finally { U.setBrowserBusy(false); }
  }

  function renderBus(out) {
    out.innerHTML = "";
    const avail = M.seats.filter((s) => normSeat(s).avail).length;
    M.avail = avail;
    out.append(
      el("div", { class: "statgrid" }, [
        U.stat(String(M.total), "total", "hot"),
        U.stat(String(avail), "free", "ok"),
        U.stat(String(M.total - avail), "taken", avail < M.total * 0.25 ? "bad" : ""),
      ]),
      chartEl(),
      buildSeatMap(M.seats, { mine: M.rec.seat, readOnly: true }),
      el("div", { class: "seatlegend" }, [
        el("span", {}, [el("i", { class: "a" }), "free"]),
        el("span", {}, [el("i", { class: "t" }), "taken"]),
        el("span", {}, [el("i", { class: "s" }), "your seat"]),
      ]),
      rescanBtn()
    );
  }
  function rescanBtn() {
    const b = el("button", { class: "btn verb sm browser-action", type: "button", text: "↻ Rescan seat map" });
    b.addEventListener("click", scan);
    return b;
  }

  /* ---- availability strip-chart (oscilloscope style) ---- */
  function chartEl() {
    const W = 300, H = 72, pad = 8;
    const hist = M.history.length ? M.history : [M.avail];
    const max = Math.max(M.total || 1, ...hist, 1);
    const n = hist.length;
    const xFor = (i) => pad + (n <= 1 ? 0 : (i / (n - 1)) * (W - 2 * pad));
    const yFor = (v) => pad + (1 - v / max) * (H - 2 * pad);
    const line = hist.map((v, i) => `${xFor(i).toFixed(1)},${yFor(v).toFixed(1)}`).join(" ");
    const area = `${pad},${H - pad} ` + line + ` ${xFor(n - 1).toFixed(1)},${H - pad}`;
    const lastX = xFor(n - 1), lastY = yFor(hist[n - 1]);
    const grid = [0.25, 0.5, 0.75].map((g) => `<line x1="${pad}" y1="${(pad + g * (H - 2 * pad)).toFixed(1)}" x2="${W - pad}" y2="${(pad + g * (H - 2 * pad)).toFixed(1)}" stroke="#3a2c1d" stroke-width="0.5" stroke-dasharray="2 3"/>`).join("");
    const svg = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="seats available over time">
        ${grid}
        <polygon points="${area}" fill="rgba(255,150,50,0.10)"/>
        <polyline points="${line}" fill="none" stroke="#ff9d33" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>
        <circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="2.6" fill="#ffc878"/>
      </svg>`;
    return el("div", { class: "strip" }, [
      el("div", { class: "striphead" }, [
        el("span", { class: "k", text: "Seats available" }),
        el("span", { class: "v seg7", text: pad2(M.avail) }),
      ]),
      el("div", { class: "stripsvg", html: svg }),
    ]);
  }

  /* ====================================================================
     CANCEL
     ==================================================================== */
  async function doCancel(statusEl, btn) {
    if (!M || !M.rec) return;
    const r = M.rec;
    if (!confirm("Cancel reservation " + r.id.slice(0, 8) + "? This stops its re-lock job and forgets the record.")) return;
    btn.disabled = true; statusEl.innerHTML = "";
    appendLog("CANCEL REQUESTED", "warn");
    try {
      const resp = await BB.deleteReservation(r.id);
      if (M.timers[0]) { clearInterval(M.timers[0]); M.timers[0] = null; }
      statusEl.appendChild(banner("ok", "RESERVATION CANCELLED", (resp && resp.detail) || "re-lock job stopped"));
      appendLog("RESERVATION CANCELLED · job stopped", "ok");
      if (localStorage.getItem("bb_resv") === r.id) localStorage.removeItem("bb_resv");
      if (M.rec) M.rec.status = "cancelled";
      update();
    } catch (e) {
      if (e instanceof BB.ApiError && e.status === 404) { statusEl.appendChild(banner("warn", "ALREADY GONE · 404", "record was not on the server")); appendLog("RECORD ALREADY GONE (404)", "warn"); }
      else { btn.disabled = false; statusEl.appendChild(errBanner(e)); }
    }
  }

  /* ---------- teletype ---------- */
  function appendLog(text, kind) {
    if (!M || !M.logEl) return;
    const ln = el("div", { class: "ln " + (kind || "") }, [el("span", { class: "t", text: nowClock() + "  " }), text]);
    M.logEl.appendChild(ln);
    while (M.logEl.childElementCount > 80) M.logEl.removeChild(M.logEl.firstChild);
    M.logEl.scrollTop = M.logEl.scrollHeight;
  }
})();
