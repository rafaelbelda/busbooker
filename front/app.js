/* ============================================================
   BUSBOOKER — console application core
   Search + Admin + nav + shared seat-map engine.
   Monitor lives in monitor.js (uses window.BBUI exposed here).
   ============================================================ */
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  function el(tag, attrs, children) {
    const n = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      if (k === "class") n.className = attrs[k];
      else if (k === "html") n.innerHTML = attrs[k];
      else if (k === "text") n.textContent = attrs[k];
      else if (k.startsWith("on") && typeof attrs[k] === "function") n.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] != null) n.setAttribute(k, attrs[k]);
    }
    if (children != null) (Array.isArray(children) ? children : [children]).forEach((c) => {
      if (c == null) return;
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return n;
  }
  const pad2 = (n) => String(n).padStart(2, "0");

  /* ---------- time helpers ---------- */
  function fmtLocal(iso) {
    if (!iso) return "——";
    const d = new Date(iso);
    if (isNaN(d)) return "——";
    const mon = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"][d.getMonth()];
    return `${pad2(d.getDate())} ${mon} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }
  function localTZ() {
    const m = -new Date().getTimezoneOffset();
    return `UTC${m >= 0 ? "+" : "-"}${pad2(Math.floor(Math.abs(m) / 60))}:${pad2(Math.abs(m) % 60)}`;
  }
  function fmtUptime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const d = Math.floor(sec / 86400); sec -= d * 86400;
    const h = Math.floor(sec / 3600); sec -= h * 3600;
    const m = Math.floor(sec / 60); const s = sec - m * 60;
    return (d > 0 ? d + "d " : "") + `${pad2(h)}:${pad2(m)}:${pad2(s)}`;
  }

  /* ---------- shared state ---------- */
  const state = {
    view: "search",
    search: null, trip: null, seat: null, activeFloor: 0,
    reserving: false, browserBusy: false,
    monitorId: localStorage.getItem("bb_resv") || "",
    adminAuth: null,
  };

  /* ---------- global browser-flow serialization ---------- */
  function setBrowserBusy(on) {
    state.browserBusy = on;
    document.body.classList.toggle("busy", on);
    $$(".browser-action").forEach((b) => { b.disabled = on || b.dataset.forceDisabled === "1"; });
    setLamp("proc", on ? "amber" : "off");
  }

  /* ---------- lamps / health ---------- */
  function setLamp(name, color) {
    const l = $(`.lamp[data-lamp="${name}"]`);
    if (!l) return;
    l.classList.remove("on-green", "on-amber", "on-red");
    if (color && color !== "off") l.classList.add("on-" + color);
  }
  async function pollHealth() {
    try {
      const h = await BB.health();
      setLamp("sys", h.status === "ok" ? "green" : "red");
      setLamp("sched", h.scheduler_running ? "amber" : "off");
      $("#uptimeReadout").textContent = fmtUptime(h.uptime_seconds);
    } catch (e) {
      setLamp("sys", "red"); setLamp("sched", "off");
      $("#uptimeReadout").textContent = "------";
    }
  }

  /* ---------- banners ---------- */
  function banner(kind, title, sub, withDash) {
    const b = el("div", { class: "banner " + kind + " fadein" }, [
      el("span", { class: "ico" }),
      el("div", { class: "grow" }, [el("div", { text: title }), sub ? el("span", { class: "sub", text: sub }) : null]),
    ]);
    if (withDash) b.appendChild(el("div", { class: "procdash", html: "<i></i><i></i><i></i><i></i><i></i><i></i>" }));
    return b;
  }
  const PROC_SUB = "browser flow is serialized server-side — please hold";
  const workingBanner = (t) => banner("proc", t || "WORKING… THIS CAN TAKE UP TO ~90 SECONDS", PROC_SUB, true);
  function errBanner(e) {
    if (e instanceof BB.ApiError && (e.status === 503 || e.status === 429))
      return banner("warn", "SERVER BUSY — TRY AGAIN SHORTLY", `retry after ~${e.retryAfter}s · ${e.status === 429 ? "rate limited" : "back-pressure"}`);
    const code = e instanceof BB.ApiError ? e.status : "";
    return banner("bad", "FAULT" + (code ? " · " + code : ""), (e && e.message) || "unknown error");
  }
  const kv = (k, vEl) => el("div", { class: "kv" }, [el("span", { class: "k", text: k }), vEl]);
  const kvSeg = (k, v) => el("div", { class: "kv" }, [el("span", { class: "k", text: k }), el("span", { class: "v seg7", text: v })]);
  const stat = (num, cap, kind) => el("div", { class: "stat " + (kind || "") }, [el("div", { class: "num", text: num }), el("div", { class: "cap", text: cap })]);

  /* ====================================================================
     SEAT-MAP ENGINE  (floors · single rows · mini preview)
     ==================================================================== */
  function median(a) { if (!a.length) return 0; const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }
  function clusterAxis(values) {
    const uniq = [...new Set(values)].sort((a, b) => a - b);
    if (uniq.length <= 1) return uniq;
    let minGap = Infinity;
    for (let i = 1; i < uniq.length; i++) minGap = Math.min(minGap, uniq[i] - uniq[i - 1]);
    const tol = Math.max(0.01, minGap * 0.5);
    const centers = []; let group = [uniq[0]];
    for (let i = 1; i < uniq.length; i++) {
      if (uniq[i] - group[group.length - 1] <= tol) group.push(uniq[i]);
      else { centers.push(group.reduce((a, b) => a + b, 0) / group.length); group = [uniq[i]]; }
    }
    centers.push(group.reduce((a, b) => a + b, 0) / group.length);
    return centers;
  }
  const nearestIndex = (cs, v) => { let bi = 0, bd = Infinity; cs.forEach((c, i) => { const d = Math.abs(c - v); if (d < bd) { bd = d; bi = i; } }); return bi; };
  const normSeat = (s) => ({
    num: s.numero != null ? s.numero : s.number,
    avail: s.disponivel != null ? s.disponivel : s.available,
    x: +(s.posX || 0), y: +(s.posY || 0),
  });

  /* Split seats into decks/floors by detecting a large vertical gap in posY. */
  function splitFloors(seats) {
    const norm = seats.map(normSeat);
    const ys = [...new Set(norm.map((s) => Math.round(s.y * 100) / 100))].sort((a, b) => a - b);
    if (ys.length < 3) return [{ label: null, seats }];
    const gaps = ys.slice(1).map((y, i) => y - ys[i]);
    const med = median(gaps);
    const cuts = [];
    gaps.forEach((g, i) => { if (med > 0 && g > med * 2.6) cuts.push((ys[i] + ys[i + 1]) / 2); });
    if (!cuts.length) return [{ label: null, seats }];
    const floorOf = (y) => { let f = 0; for (const c of cuts) if (y > c) f++; return f; };
    const groups = {};
    seats.forEach((s) => { const f = floorOf(+(s.posY || 0)); (groups[f] = groups[f] || []).push(s); });
    return Object.keys(groups).sort((a, b) => a - b).map((f, idx) => ({ label: "Piso " + (idx + 1), seats: groups[f] }));
  }

  function seatCell(s, o) {
    const cell = o.cell || 44;
    const labels = o.labels !== false;
    const sel = o.selected === s.num;
    const mine = o.mine === s.num;
    let cls = "seat " + (!s.avail ? "taken" : "avail");
    if (cell <= 14) cls += " mini";
    if (sel) cls += " sel";
    if (mine) cls += " mine";
    const interactive = s.avail && typeof o.onPick === "function" && !o.readOnly;
    const node = el(interactive ? "button" : "div", {
      class: cls,
      "aria-label": `seat ${s.num} ${s.avail ? "free" : "taken"}${mine ? " — your seat" : ""}`,
    });
    if (interactive) { node.type = "button"; if (sel) node.setAttribute("aria-pressed", "true"); node.addEventListener("click", () => o.onPick(s.num)); }
    node.style.width = cell + "px"; node.style.height = cell + "px";
    if (labels) { node.textContent = s.num; node.style.fontSize = Math.max(9, Math.round(cell * 0.32)) + "px"; }
    return node;
  }

  /* Render ONE deck of seats into a CSS grid (handles aisles + single columns). */
  function gridMap(seats, o = {}) {
    o = o || {};
    const cell = o.cell || 44;
    const mini = !!o.mini;
    const norm = seats.map(normSeat);
    const xs = clusterAxis(norm.map((s) => s.x));
    const ys = clusterAxis(norm.map((s) => s.y));

    const wrap = el("div", { class: "seatmap-wrap" + (mini ? " mini" : "") });
    const map = el("div", { class: "seatmap" + (mini ? " mini" : "") });

    if (xs.length <= 1 || ys.length <= 1) {
      // no usable coords (e.g. /seats has none) → sequential grid
      const cols = xs.length <= 1 ? (o.fallbackCols || 4) : xs.length;
      map.style.gridTemplateColumns = `repeat(${cols}, ${cell}px)`;
      norm.forEach((s) => map.appendChild(seatCell(s, o)));
    } else if (o.transpose) {
      // Rotated 90° left: bus length (posY) runs left→right as columns,
      // seat columns (posX) run top→bottom as rows. Saves vertical space in trip cards.
      map.style.gridTemplateColumns = ys.map(() => cell + "px").join(" ");
      norm.forEach((s) => {
        const c = seatCell(s, o);
        c.style.gridColumn = nearestIndex(ys, s.y) + 1;
        c.style.gridRow = nearestIndex(xs, s.x) + 1;
        map.appendChild(c);
      });
    } else {
      const gaps = xs.slice(1).map((x, i) => x - xs[i]);
      const med = median(gaps);
      const template = []; const colMap = [];
      xs.forEach((x, i) => {
        if (i > 0 && med > 0 && gaps[i - 1] > med * 1.7) template.push((mini ? 6 : 14) + "px");
        template.push(cell + "px"); colMap[i] = template.length;
      });
      map.style.gridTemplateColumns = template.join(" ");
      let rowOffset = 1;
      if (!mini) { const front = el("div", { class: "fronttag", text: "▸ front" }); front.style.gridColumn = "1 / -1"; map.appendChild(front); rowOffset = 2; }
      norm.forEach((s) => {
        const c = seatCell(s, o);
        c.style.gridColumn = colMap[nearestIndex(xs, s.x)];
        c.style.gridRow = nearestIndex(ys, s.y) + rowOffset;
        map.appendChild(c);
      });
    }
    wrap.appendChild(map);
    return wrap;
  }

  /* Full interactive map with a floor toggle when the coach is double-decker. */
  function buildSeatMap(seats, o = {}) {
    const floors = splitFloors(seats);
    if (floors.length <= 1) return gridMap(seats, o);
    const wrap = el("div", { class: "floorwrap" });
    const tabs = el("div", { class: "floortabs" });
    const stage = el("div");
    let active = Math.min(o.activeFloor || 0, floors.length - 1);
    const show = (i) => {
      active = i;
      if (o.onFloor) o.onFloor(i);
      stage.innerHTML = "";
      stage.appendChild(gridMap(floors[i].seats, o));
      [...tabs.children].forEach((b, bi) => b.setAttribute("aria-pressed", bi === i ? "true" : "false"));
    };
    floors.forEach((fl, i) => tabs.appendChild(el("button", {
      class: "floortab", type: "button", "aria-pressed": i === active ? "true" : "false",
      text: (fl.label || "Piso " + (i + 1)) + " · " + fl.seats.filter((s) => normSeat(s).avail).length,
      onclick: () => show(i),
    })));
    wrap.append(tabs, stage);
    show(active);
    return wrap;
  }

  /* Tiny non-interactive occupancy preview (decks side by side). */
  function buildMiniMap(seats) {
    const floors = splitFloors(seats);
    const row = el("div", { class: "minibus" });
    floors.forEach((fl, i) => {
      const deck = el("div", { class: "minideck" });
      if (floors.length > 1) deck.appendChild(el("span", { class: "minilbl", text: String(i + 1) }));
      deck.appendChild(gridMap(fl.seats, { cell: 8, labels: false, mini: true, readOnly: true, transpose: true }));
      row.appendChild(deck);
    });
    return row;
  }
  const freeCount = (seats) => seats.filter((s) => normSeat(s).avail).length;

  /* ====================================================================
     VIEW: SEARCH → SEAT → RESERVE
     ==================================================================== */
  function renderSearch() {
    const root = $("#view-search");
    root.innerHTML = "";
    const urlField = el("textarea", {
      id: "searchUrl", rows: "3", autocapitalize: "off", autocomplete: "off", spellcheck: "false",
      placeholder: "https://mobifacil.com.br/passagem-de-onibus/…?origin=…&destination=…&date=dd-mm-yyyy…",
    });
    const searchBtn = el("button", { class: "btn browser-action", id: "searchBtn", type: "button", text: "▶ Search" });
    root.append(
      el("div", { class: "section" }, [
        el("div", { class: "legend" }, [el("span", { class: "idx", text: "01" }), "Traject input"]),
        el("div", { class: "field" }, [
          el("label", { for: "searchUrl", text: "Mobifacil passage URL" }),
          urlField,
          el("span", { class: "hint", text: "Paste the full mobifacil passagem-de-onibus link exactly as copied." }),
        ]),
        searchBtn,
        el("div", { id: "searchStatus", class: "spaced", style: "margin-top:12px" }),
      ]),
      el("div", { class: "section hidden", id: "resultsSec" }, [
        el("div", { class: "legend" }, [el("span", { class: "idx", text: "02" }), "Trip register"]),
        el("div", { id: "tripList", class: "spaced" }),
      ]),
      el("div", { class: "section hidden", id: "seatSec" })
    );
    searchBtn.addEventListener("click", doSearch);
    // restore a previous result if returning to the tab
    if (state.search) { renderTrips(); if (state.trip) renderSeatSection(); }
  }

  async function doSearch() {
    const url = $("#searchUrl").value.trim();
    const status = $("#searchStatus");
    status.innerHTML = "";
    if (!url) { status.appendChild(banner("warn", "NO URL", "paste a mobifacil passage link first")); return; }
    setBrowserBusy(true);
    status.appendChild(workingBanner("SEARCHING TRIPS… ONE PAGE FETCH PER TRIP, CAN TAKE A WHILE"));
    try {
      const res = await BB.search(url);
      state.search = res; state.trip = null; state.seat = null; state.activeFloor = 0;
      status.innerHTML = "";
      const n = (res.trips || []).length;
      status.appendChild(banner("ok", `${n} TRIP${n === 1 ? "" : "S"} RESOLVED`, `route ${res.origin_id} → ${res.destination_id} · ${res.date}`));
      renderTrips();
    } catch (e) {
      status.innerHTML = ""; status.appendChild(errBanner(e));
    } finally { setBrowserBusy(false); }
  }

  function renderTrips() {
    const sec = $("#resultsSec"); const list = $("#tripList");
    sec.classList.remove("hidden"); list.innerHTML = "";
    const trips = (state.search && state.search.trips) || [];
    if (!trips.length) { list.appendChild(el("div", { class: "empty", text: "NO TRIPS RETURNED FOR THIS ROUTE" })); return; }
    trips.forEach((t) => {
      const seats = t.seats || [];
      const floors = splitFloors(seats);
      const free = freeCount(seats);
      const card = el("button", { class: "trip", type: "button", "aria-pressed": state.trip === t ? "true" : "false" }, [
        el("div", { class: "toprow" }, [el("span", { class: "co", text: t.company }), el("span", { class: "price", text: "R$" + t.price })]),
        el("div", { class: "timerow" }, [
          el("span", { class: "clock", text: t.departure }),
          el("span", { class: "arrow", text: "→" }),
          el("span", { class: "clock", text: t.arrival }),
        ]),
        el("div", { class: "meta" }, [
          el("span", { text: t.service_class + (floors.length > 1 ? " · " + floors.length + " pisos" : "") }),
          el("span", { class: "avail", text: `${free}/${seats.length} free` }),
        ]),
        buildMiniMap(seats),
      ]);
      card.addEventListener("click", () => { state.trip = t; state.seat = null; state.activeFloor = 0; renderTrips(); renderSeatSection(); });
      list.appendChild(card);
    });
  }

  function renderSeatSection() {
    const sec = $("#seatSec");
    const t = state.trip;
    if (!t) { sec.classList.add("hidden"); sec.innerHTML = ""; return; }
    sec.classList.remove("hidden"); sec.innerHTML = "";
    sec.appendChild(el("div", { class: "legend" }, [el("span", { class: "idx", text: "03" }), "Seat select"]));
    sec.appendChild(el("div", { class: "readout", style: "margin-bottom:12px" }, [
      kv("Service", el("span", { class: "v wrap", text: t.company + " · " + t.service_class })),
      kv("Depart", el("span", { class: "v seg7", text: t.departure })),
      kv("Service ID", el("span", { class: "v", text: t.service_id })),
    ]));
    sec.appendChild(buildSeatMap(t.seats || [], {
      selected: state.seat, activeFloor: state.activeFloor,
      onFloor: (i) => { state.activeFloor = i; },
      onPick: (num) => { state.seat = num; renderSeatSection(); },
    }));
    sec.appendChild(el("div", { class: "seatlegend" }, [
      el("span", {}, [el("i", { class: "a" }), "free"]),
      el("span", {}, [el("i", { class: "t" }), "taken"]),
      el("span", {}, [el("i", { class: "s" }), "selected"]),
    ]));
    sec.appendChild(el("div", { class: "divider" }));
    sec.appendChild(el("div", { class: "readout", style: "margin:10px 0" }, [
      el("div", { class: "kv" }, [el("span", { class: "k", text: "Seat armed" }), el("span", { class: "v seg7", style: "font-size:22px", text: state.seat || "--" })]),
    ]));
    const reserveBtn = el("button", { class: "btn browser-action", id: "reserveBtn", type: "button" }, "■ Reserve seat");
    reserveBtn.disabled = !state.seat || state.browserBusy;
    reserveBtn.dataset.forceDisabled = state.seat ? "0" : "1";
    sec.appendChild(reserveBtn);
    sec.appendChild(el("p", { class: "note", text: "Reserve drives a live browser at the provider. Submit is disabled until the call returns — the endpoint is not idempotent." }));
    sec.appendChild(el("div", { id: "reserveStatus", class: "spaced", style: "margin-top:12px" }));
    reserveBtn.addEventListener("click", doReserve);
  }

  async function doReserve() {
    if (!state.trip || !state.seat || state.reserving) return;
    const s = state.search;
    const body = { origin_id: s.origin_id, destination_id: s.destination_id, date: s.date, departure: state.trip.departure, seat: state.seat };
    const status = $("#reserveStatus");
    status.innerHTML = ""; state.reserving = true; setBrowserBusy(true);
    status.appendChild(workingBanner("LOCKING SEAT… UP TO ~90 SECONDS"));
    try {
      const { status: code, record } = await BB.createReservation(body);
      status.innerHTML = "";
      if (code === 201 && record.status === "locked") {
        saveMonitorId(record.id);
        status.appendChild(banner("ok", "SEAT LOCKED ✓", `id ${record.id.slice(0, 8)} · re-lock cycle armed`));
        status.appendChild(el("button", { class: "btn verb", type: "button", text: "◎ Track this reservation",
          onclick: () => { state.monitorId = record.id; switchView("monitor"); } }));
      } else if (code === 409) {
        status.appendChild(banner("bad", "SEAT UNAVAILABLE", "that seat was just taken — pick another from the map"));
      } else {
        status.appendChild(banner("bad", "FLOW ERROR · 500", (record && record.error_msg) || "unrecoverable provider error — you may retry"));
      }
    } catch (e) {
      status.innerHTML = ""; status.appendChild(errBanner(e));
    } finally { state.reserving = false; setBrowserBusy(false); }
  }

  function saveMonitorId(id) { state.monitorId = id; localStorage.setItem("bb_resv", id); }

  /* ====================================================================
     VIEW: ADMIN
     ==================================================================== */
  function renderAdmin() {
    const root = $("#view-admin");
    root.innerHTML = "";
    if (!state.adminAuth) return renderAdminLogin(root);
    renderAdminDash(root);
  }
  function renderAdminLogin(root) {
    const pw = el("input", { type: "password", id: "adminPw", placeholder: "ADMIN_PASSWORD", autocapitalize: "off", autocomplete: "current-password" });
    const go = el("button", { class: "btn", type: "button", text: "⤓ Authenticate" });
    const status = el("div", { class: "spaced", style: "margin-top:12px" });
    root.append(el("div", { class: "section" }, [
      el("div", { class: "legend" }, [el("span", { class: "idx", text: "00" }), "Restricted · basic auth"]),
      el("div", { class: "field" }, [el("label", { for: "adminPw", text: "Username fixed: admin" }), pw]),
      go,
      el("p", { class: "note", text: "Credentials held in memory for this session only and sent as an Authorization: Basic header. Never stored." }),
      status,
    ]));
    const submit = async () => {
      const pass = pw.value;
      if (!pass) return;
      go.disabled = true; status.innerHTML = ""; status.appendChild(banner("proc", "AUTHENTICATING…", "", true));
      try { await BB.adminStats(pass); state.adminAuth = pass; renderAdmin(); }
      catch (e) {
        go.disabled = false; status.innerHTML = "";
        status.appendChild(e instanceof BB.ApiError && e.status === 401 ? banner("bad", "ACCESS DENIED · 401", "invalid credentials") : errBanner(e));
      }
    };
    go.addEventListener("click", submit);
    pw.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  }
  function renderAdminDash(root) {
    root.innerHTML = "";
    const auth = state.adminAuth;
    const statBar = el("div", { class: "statgrid" });
    const tableWrap = el("div", { id: "adminTable" });
    const schedReadout = el("div", { class: "readout" });
    const opStatus = el("div", { class: "spaced", style: "margin-top:10px" });
    const refresh = el("button", { class: "btn verb sm", type: "button", text: "↻ Refresh" });
    const logout = el("button", { class: "btn verb sm", type: "button", text: "⏏ Sign out" });
    const shutdown = el("button", { class: "btn danger sm", type: "button", text: "⏻ Shutdown server" });
    root.append(
      el("div", { class: "section" }, [
        el("div", { class: "legend" }, [el("span", { class: "idx", text: "01" }), "Reservation census"]),
        statBar, el("div", { class: "btnrow", style: "margin-top:12px" }, [refresh, logout]),
      ]),
      el("div", { class: "section" }, [el("div", { class: "legend" }, [el("span", { class: "idx", text: "02" }), "Scheduler"]), schedReadout]),
      el("div", { class: "section" }, [el("div", { class: "legend" }, [el("span", { class: "idx", text: "03" }), "All reservations"]), tableWrap]),
      el("div", { class: "section" }, [
        el("div", { class: "legend" }, [el("span", { class: "idx", text: "04" }), "Power"]),
        el("p", { class: "note", text: "Graceful SIGTERM. Refused with 409 while any reservation is pending or locked. On success the API disconnects within ~1 s." }),
        shutdown, opStatus,
      ])
    );
    async function load() {
      statBar.innerHTML = ""; schedReadout.innerHTML = ""; tableWrap.innerHTML = "";
      statBar.appendChild(el("div", { class: "empty", style: "grid-column:1/-1", text: "LOADING…" }));
      try {
        const [stats, list, sched, pubSched] = await Promise.all([
          BB.adminStats(auth), BB.adminReservations(auth), BB.adminScheduler(auth), BB.schedulerStatus().catch(() => null),
        ]);
        statBar.innerHTML = "";
        statBar.append(
          stat(String(stats.total), "total", "hot"), stat(String(stats.locked), "locked", ""), stat(String(stats.pending), "pending", ""),
          stat(String(stats.failed), "failed", stats.failed ? "bad" : ""), stat(String(stats.cancelled), "cancelled", ""), stat(String(stats.expired), "expired", "")
        );
        schedReadout.innerHTML = "";
        schedReadout.append(
          kv("Running", el("span", { class: "v", text: sched.running ? "YES" : "NO" })),
          kvSeg("Interval (min)", String(sched.interval_minutes)),
          kvSeg("Active re-locks", String(sched.active_relock_count)),
          kv("Jobs (public)", el("span", { class: "v", text: pubSched ? String(pubSched.active_relock_count) : "n/a" }))
        );
        renderAdminTable(tableWrap, list, auth, load);
      } catch (e) {
        statBar.innerHTML = "";
        if (e instanceof BB.ApiError && e.status === 401) { state.adminAuth = null; return renderAdmin(); }
        statBar.appendChild(errBanner(e));
      }
    }
    refresh.addEventListener("click", load);
    logout.addEventListener("click", () => { state.adminAuth = null; renderAdmin(); });
    shutdown.addEventListener("click", async () => {
      if (!confirm("Send graceful shutdown (SIGTERM) to the server? It will stop responding within ~1 s.")) return;
      shutdown.disabled = true; opStatus.innerHTML = ""; opStatus.appendChild(banner("proc", "SENDING SIGTERM…", "", true));
      try {
        const r = await BB.adminShutdown(auth);
        opStatus.innerHTML = ""; opStatus.appendChild(banner("warn", "SHUTTING DOWN", (r && r.status) + " · expect connection loss")); setLamp("sys", "red");
      } catch (e) {
        shutdown.disabled = false; opStatus.innerHTML = "";
        if (e instanceof BB.ApiError && e.status === 409) { const raw = e.raw || {}; opStatus.appendChild(banner("bad", "SHUTDOWN REFUSED · 409", raw.message || `${raw.count || ""} reservation(s) still active`)); }
        else if (e instanceof BB.ApiError && e.status === 401) { state.adminAuth = null; renderAdmin(); }
        else opStatus.appendChild(errBanner(e));
      }
    });
    load();
  }
  function renderAdminTable(wrap, list, auth, reload) {
    wrap.innerHTML = "";
    if (!list || !list.length) { wrap.appendChild(el("div", { class: "empty", text: "NO RESERVATIONS ON RECORD" })); return; }
    const tbl = el("table", { class: "recs" });
    tbl.appendChild(el("thead", {}, el("tr", {}, ["ID", "Route", "Seat", "Date · Dep", "Status", "Re-lock", ""].map((h) => el("th", { text: h })))));
    const tb = el("tbody");
    list.forEach((r) => {
      const cancelBtn = el("button", { class: "btn danger sm", type: "button", text: "Force-cancel", style: "min-height:36px;padding:6px 10px;font-size:11px" });
      cancelBtn.disabled = r.status === "cancelled" || r.status === "expired";
      cancelBtn.addEventListener("click", async () => {
        if (!confirm("Force-cancel " + r.id.slice(0, 8) + "? Record is kept and marked cancelled.")) return;
        cancelBtn.disabled = true; cancelBtn.textContent = "…";
        try { await BB.adminDelete(r.id, auth); reload(); }
        catch (e) { cancelBtn.disabled = false; cancelBtn.textContent = "Force-cancel"; alert("Failed: " + e.message); }
      });
      tb.appendChild(el("tr", {}, [
        el("td", { class: "mono7", text: r.id.slice(0, 8) }),
        el("td", { text: r.origin_id + "→" + r.destination_id }),
        el("td", { class: "mono7", text: r.seat }),
        el("td", { text: r.date + " " + r.departure }),
        el("td", {}, el("span", { class: "pill " + r.status, text: r.status })),
        el("td", { class: "mono7", text: String(r.relock_count) }),
        el("td", {}, cancelBtn),
      ]));
    });
    tbl.appendChild(tb);
    wrap.appendChild(el("div", { class: "tablewrap" }, tbl));
  }

  /* ====================================================================
     NAV / BOOT
     ==================================================================== */
  function switchView(v) {
    state.view = v;
    if (v !== "monitor" && window.stopMonitor) window.stopMonitor();
    $$(".view").forEach((n) => n.classList.toggle("active", n.id === "view-" + v));
    $$(".modebtn").forEach((b) => b.setAttribute("aria-pressed", b.dataset.view === v ? "true" : "false"));
    if (v === "search") renderSearch();
    else if (v === "monitor") window.renderMonitor && window.renderMonitor();
    else if (v === "admin") renderAdmin();
  }
  function boot() {
    $$(".modebtn").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
    setLamp("link", "green");
    switchView("search");
    pollHealth();
    setInterval(pollHealth, 20000);
  }

  /* shared namespace for monitor.js */
  window.BBUI = {
    el, $, $$, pad2, banner, errBanner, workingBanner, kv, kvSeg, stat,
    fmtLocal, localTZ, fmtUptime, buildSeatMap, buildMiniMap, gridMap, splitFloors, normSeat, freeCount,
    setBrowserBusy, saveMonitorId, switchView, state,
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
