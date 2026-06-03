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
    search: null, trip: null, seat: null,
    seatMap: null,   // fetched from GET /seats when a trip is selected
    activeFloor: 0,
    reserving: false, browserBusy: false,
    monitorId: localStorage.getItem("bb_resv") || "",
    adminAuth: null,
    searchMode: "city",
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
  // Reservation-specific: browser-driven, can take up to ~90s.
  const RESERVE_SUB = "browser flow is serialized server-side — please hold";
  const workingBanner = (t) => banner("proc", t || "WORKING… THIS CAN TAKE UP TO ~90 SECONDS", RESERVE_SUB, true);
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
  // Normalises both TripSeat {numero, disponivel, posX, posY}
  // and SeatInfo {number, available} shapes into a common form.
  // posX = JSON "x" = bus length (1..N, front→back)   → internal y (rows in vertical, columns in transpose)
  // posY = JSON "y" = cross-section (0..4, 2=corridor) → internal x (columns / aisle detection)
  const normSeat = (s) => ({
    num: s.numero != null ? s.numero : s.number,
    avail: s.disponivel != null ? s.disponivel : s.available,
    x: +(s.posY || 0),   // cross-section → aisle axis
    y: +(s.posX || 0),   // bus length   → depth axis
  });

  /* Split seats into sections/decks. Two strategies:
     1. z field > 0 exists → true multi-deck bus, group by z.
     2. Fallback: large gap in bus-length axis (normSeat.y = posX) → two sections
        separated by empty rows (e.g. executive buses).  Higher posX = FLOOR 1
        to match mobifacil's Primeiro/Segundo Piso ordering. */
  function splitFloors(seats) {
    const zVals = [...new Set(seats.map((s) => +(s.posZ || 0)))].sort((a, b) => a - b);
    if (zVals.length > 1) {
      const groups = {};
      seats.forEach((s) => { const z = +(s.posZ || 0); (groups[z] = groups[z] || []).push(s); });
      return zVals.map((z, idx) => ({ label: "FLOOR " + (idx + 1), seats: groups[z] }));
    }
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
    norm.forEach((ns, i) => { const f = floorOf(ns.y); (groups[f] = groups[f] || []).push(seats[i]); });
    // Reverse: higher posX section → FLOOR 1 (matches mobifacil Primeiro Piso)
    return Object.keys(groups).sort((a, b) => +b - +a).map((f, idx) => ({ label: "FLOOR " + (idx + 1), seats: groups[f] }));
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
      // Bus length (posX) → columns left→right; cross-section (posY) → rows top→bottom.
      // Split cross-section positions into groups at aisle gaps (xMin * 1.8 threshold).
      // Sort groups so the SMALLER group (single seats) is always on top.
      const xGaps = xs.slice(1).map((x, i) => x - xs[i]);
      const xMin = xGaps.length ? Math.min(...xGaps) : 0;
      const xGroups = []; let curGrp = [xs[0]];
      xs.slice(1).forEach((x, i) => {
        if (xMin > 0 && xGaps[i] > xMin * 1.8) { xGroups.push(curGrp); curGrp = [x]; }
        else curGrp.push(x);
      });
      xGroups.push(curGrp);
      // Smaller group first (single-seat side on top); equal-size groups keep natural order
      xGroups.sort((a, b) => a.length - b.length);
      const rowTpl = []; const xRowMap = new Map();
      xGroups.forEach((grp, gi) => {
        if (gi > 0) rowTpl.push((mini ? 4 : 14) + "px");
        grp.forEach((x) => { rowTpl.push(cell + "px"); xRowMap.set(x, rowTpl.length); });
      });
      map.style.gridTemplateColumns = ys.map(() => cell + "px").join(" ");
      map.style.gridTemplateRows = rowTpl.join(" ");
      norm.forEach((s) => {
        const c = seatCell(s, o);
        c.style.gridColumn = nearestIndex(ys, s.y) + 1;
        c.style.gridRow = xRowMap.get(xs[nearestIndex(xs, s.x)]);
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
      text: (fl.label || "FLOOR " + (i + 1)) + " · " + fl.seats.filter((s) => normSeat(s).avail).length,
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

    const CITIES = [
      { name: "São Paulo (todos)", id: "-3" },
      { name: "Campinas SP",       id: "19301" },
      { name: "Ribeirão Preto SP", id: "19068" },
      { name: "São Carlos SP",     id: "19058" },
      { name: "Araraquara SP",     id: "19052" },
      { name: "Florianópolis SC",  id: "-18" },
    ];

    function dateOffset(days) {
      const d = new Date();
      d.setDate(d.getDate() + days);
      return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
    }
    const today = dateOffset(0);
    const maxDay = dateOffset(5);

    const makeOpts = (selectedId) =>
      CITIES.map((c) => {
        const o = el("option", { value: c.id, text: c.name });
        if (c.id === selectedId) o.selected = true;
        return o;
      });

    const originSel = el("select", { id: "originSel" }, makeOpts("19052")); // default: Araraquara
    const destSel   = el("select", { id: "destSel"   }, makeOpts("-3"));    // default: São Paulo

    const dateInput = el("input", {
      type: "date", id: "searchDate",
      value: today, min: today, max: maxDay,
    });

    const swapBtn = el("button", {
      class: "swapbtn", type: "button", "aria-label": "swap origin and destination", text: "⇄",
    });
    swapBtn.addEventListener("click", () => {
      const tmp = originSel.value;
      originSel.value = destSel.value;
      destSel.value = tmp;
    });

    const urlField = el("textarea", {
      id: "searchUrl", rows: "3", autocapitalize: "off", autocomplete: "off", spellcheck: "false",
      placeholder: "https://mobifacil.com.br/passagem-de-onibus/…?origin=…&destination=…&date=dd-mm-yyyy…",
    });

    const tabCity = el("button", { class: "modetab", type: "button", "aria-pressed": "true",  text: "City Select" });
    const tabUrl  = el("button", { class: "modetab", type: "button", "aria-pressed": "false", text: "Direct URL" });

    const cityForm = el("div", { id: "cityForm" }, [
      el("div", { class: "field" }, [
        el("div", { class: "cityrow" }, [
          el("div", { class: "citycol" }, [el("span", { class: "citylbl", text: "Origin" }), originSel]),
          swapBtn,
          el("div", { class: "citycol" }, [el("span", { class: "citylbl", text: "Destination" }), destSel]),
        ]),
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "searchDate", text: "Date" }),
        dateInput,
        el("span", { class: "hint", text: "Up to 5 days ahead. For other cities use Direct URL." }),
      ]),
    ]);

    const urlForm = el("div", { id: "urlForm", class: "hidden" }, [
      el("div", { class: "field" }, [
        el("label", { for: "searchUrl", text: "Mobifacil passage URL" }),
        urlField,
        el("span", { class: "hint", text: "Paste the full mobifacil passagem-de-onibus link exactly as copied." }),
      ]),
    ]);

    const switchMode = (mode) => {
      state.searchMode = mode;
      tabCity.setAttribute("aria-pressed", mode === "city" ? "true" : "false");
      tabUrl.setAttribute("aria-pressed",  mode === "url"  ? "true" : "false");
      cityForm.classList.toggle("hidden", mode !== "city");
      urlForm.classList.toggle("hidden",  mode !== "url");
    };
    tabCity.addEventListener("click", () => switchMode("city"));
    tabUrl.addEventListener("click",  () => switchMode("url"));

    const searchBtn = el("button", { class: "btn", id: "searchBtn", type: "button", text: "▶ Search" });

    root.append(
      el("div", { class: "section" }, [
        el("div", { class: "legend" }, [el("span", { class: "idx", text: "01" }), "Traject input"]),
        el("div", { class: "modetabs" }, [tabCity, tabUrl]),
        cityForm,
        urlForm,
        searchBtn,
        el("div", { id: "searchStatus", class: "spaced", style: "margin-top:12px" }),
      ]),
      el("div", { class: "section hidden", id: "resultsSec" }, [
        el("div", { class: "legend" }, [el("span", { class: "idx", text: "02" }), "Trip register"]),
        el("div", { id: "tripList", class: "spaced" }),
      ]),
      el("div", { class: "section hidden", id: "seatSec" })
    );

    if (state.searchMode === "url") switchMode("url");
    searchBtn.addEventListener("click", doSearch);
    if (state.search) { renderTrips(); if (state.trip) renderSeatSection(); }
  }

  async function doSearch() {
    const status = $("#searchStatus");
    status.innerHTML = "";

    let url;
    if (state.searchMode !== "url") {
      const origin = $("#originSel").value;
      const dest   = $("#destSel").value;
      const date   = $("#searchDate").value; // yyyy-mm-dd
      if (!date) { status.appendChild(banner("warn", "NO DATE", "select a travel date")); return; }
      if (origin === dest) { status.appendChild(banner("warn", "INVALID ROUTE", "origin and destination must differ")); return; }
      const [yr, mo, dy] = date.split("-");
      url = `https://mobifacil.com.br/passagem-de-onibus/?origin=${origin}&destination=${dest}&date=${dy}-${mo}-${yr}&isStudent=false&isPCD=false&searchValidDay=true`;
    } else {
      url = $("#searchUrl").value.trim();
      if (!url) { status.appendChild(banner("warn", "NO URL", "paste a mobifacil passage link first")); return; }
    }

    const btn = $("#searchBtn");
    btn.disabled = true;
    status.appendChild(banner("proc", "SEARCHING TRIPS…", "fetching mobifacil route HTML", true));
    try {
      const res = await BB.search(url);
      state.search = res; state.trip = null; state.seat = null; state.seatMap = null; state.activeFloor = 0;
      status.innerHTML = "";
      const n = (res.trips || []).length;
      status.appendChild(banner("ok", `${n} TRIP${n === 1 ? "" : "S"} FOUND`, `route ${res.origin_id} → ${res.destination_id} · ${res.date}`));
      renderTrips();
    } catch (e) {
      status.innerHTML = ""; status.appendChild(errBanner(e));
    } finally { btn.disabled = false; }
  }

  function renderTrips() {
    const sec = $("#resultsSec"); const list = $("#tripList");
    sec.classList.remove("hidden"); list.innerHTML = "";
    const trips = (state.search && state.search.trips) || [];
    if (!trips.length) { list.appendChild(el("div", { class: "empty", text: "NO TRIPS RETURNED FOR THIS ROUTE" })); return; }
    trips.forEach((t) => {
      // Build meta line: service class, duration, floor count
      const metaParts = [t.service_class];
      if (t.duration) metaParts.push(t.duration);
      if (t.has_second_floor) metaParts.push("2 pisos");

      const card = el("button", { class: "trip", type: "button", "aria-pressed": state.trip === t ? "true" : "false" }, [
        el("div", { class: "toprow" }, [
          el("span", { class: "co", text: t.company }),
          el("span", { class: "price", text: t.price ? "R$" + t.price : "—" }),
        ]),
        el("div", { class: "timerow" }, [
          el("span", { class: "clock", text: t.departure }),
          el("span", { class: "arrow", text: "→" }),
          el("span", { class: "clock", text: t.arrival }),
        ]),
        el("div", { class: "meta" }, [
          el("span", { text: metaParts.join(" · ") }),
          // available_seats from lsServicos (accurate count without calling BusDetails)
          el("span", { class: "avail", text: `${t.available_seats} free` }),
        ]),
      ]);
      card.addEventListener("click", () => {
        // Reset seatMap so the next section fetch is fresh for this trip.
        state.trip = t; state.seat = null; state.seatMap = null; state.activeFloor = 0;
        renderTrips();
        renderSeatSection();
      });
      list.appendChild(card);
    });
  }

  async function renderSeatSection() {
    const sec = $("#seatSec");
    const t = state.trip;
    if (!t) { sec.classList.add("hidden"); sec.innerHTML = ""; return; }

    sec.classList.remove("hidden"); sec.innerHTML = "";
    sec.appendChild(el("div", { class: "legend" }, [el("span", { class: "idx", text: "03" }), "Seat select"]));
    // departure_date is "DD/MM/YYYY" as returned by mobifacil — may differ from
    // the searched date if mobifacil rolls over to the next day's results.
    const rawDate = t.departure_date ?? "";
    const fmtDate = rawDate
      ? new Date(rawDate.split("/").reverse().join("-") + "T12:00:00")
          .toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" }).toUpperCase()
      : "";
    sec.appendChild(el("div", { class: "readout", style: "margin-bottom:12px" }, [
      kv("Service",  el("span", { class: "v wrap", text: t.company + " · " + t.service_class })),
      kv("Date",     el("span", { class: "v", text: fmtDate })),
      kv("Depart",   el("span", { class: "v seg7", text: t.departure })),
      kv("Arrive",   el("span", { class: "v seg7", text: t.arrival })),
      t.duration ? kv("Duration", el("span", { class: "v", text: t.duration })) : null,
    ]));

    const seatArea = el("div", { id: "seatArea" });
    const reserveSec = el("div", { id: "reserveSec" });
    sec.append(seatArea, reserveSec);

    // Use cached seat map if this trip's data is already loaded.
    if (state.seatMap) {
      renderSeatPicker(seatArea, reserveSec);
      return;
    }

    // Fetch live seat map from /seats.
    const s = state.search;
    seatArea.appendChild(banner("proc", "READING SEAT MAP…", "fetching live seat data", true));
    setBrowserBusy(true);
    try {
      const seatsRes = await BB.getSeats({
        origin_id: s.origin_id, destination_id: s.destination_id,
        date: s.date, departure: t.departure,
      });
      // SeatInfo {number, available} → normSeat-compatible shape.
      // No position data from /seats; gridMap falls back to sequential layout.
      state.seatMap = seatsRes.seats || [];
      seatArea.innerHTML = "";
      renderSeatPicker(seatArea, reserveSec);
    } catch (e) {
      seatArea.innerHTML = "";
      seatArea.appendChild(errBanner(e));
    } finally {
      setBrowserBusy(false);
    }
  }

  function renderSeatPicker(seatArea, reserveSec) {
    seatArea.innerHTML = "";
    seatArea.appendChild(buildSeatMap(state.seatMap || [], {
      selected: state.seat, activeFloor: state.activeFloor,
      transpose: true,
      onFloor: (i) => { state.activeFloor = i; },
      onPick: (num) => { state.seat = num; renderReserveControl(reserveSec); },
    }));
    seatArea.appendChild(el("div", { class: "seatlegend" }, [
      el("span", {}, [el("i", { class: "a" }), "free"]),
      el("span", {}, [el("i", { class: "t" }), "taken"]),
      el("span", {}, [el("i", { class: "s" }), "selected"]),
    ]));
    renderReserveControl(reserveSec);
  }

  function renderReserveControl(reserveSec) {
    reserveSec.innerHTML = "";
    reserveSec.append(
      el("div", { class: "divider" }),
      el("div", { class: "readout", style: "margin:10px 0" }, [
        el("div", { class: "kv" }, [
          el("span", { class: "k", text: "Seat armed" }),
          el("span", { class: "v seg7", style: "font-size:22px", text: state.seat || "--" }),
        ]),
      ])
    );
    const reserveBtn = el("button", { class: "btn browser-action", id: "reserveBtn", type: "button" }, "Reserve seat");
    reserveBtn.disabled = !state.seat || state.browserBusy;
    reserveBtn.dataset.forceDisabled = state.seat ? "0" : "1";
    reserveSec.append(
      reserveBtn,
      el("p", { class: "note", text: "Reserve drives a live browser at the provider. Submit is disabled until the call returns — the endpoint is not idempotent." }),
      el("div", { id: "reserveStatus", class: "spaced", style: "margin-top:12px" })
    );
    reserveBtn.addEventListener("click", doReserve);
  }

  function doReserve() {
    if (!state.trip || !state.seat || state.reserving) return;
    const s = state.search;
    // Generate 8-char hex ID client-side (same format the server uses).
    // This lets us navigate to the monitor instantly without waiting for the
    // 90-second browser flow — the monitor polls for live status via GET /reservations/{id}.
    const rid = Array.from(crypto.getRandomValues(new Uint8Array(4)))
      .map((b) => b.toString(16).padStart(2, "0")).join("");
    saveMonitorId(rid);
    switchView("monitor");
    // Fire the reservation in the background; PROC lamp stays amber until done.
    state.reserving = true; setBrowserBusy(true);
    BB.createReservation({
      id: rid,
      origin_id: s.origin_id, destination_id: s.destination_id,
      date: s.date, departure: state.trip.departure, seat: state.seat,
    }).catch(() => {}).finally(() => { state.reserving = false; setBrowserBusy(false); });
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
        if (!confirm("Force-cancel " + r.id + "? Record is kept and marked cancelled.")) return;
        cancelBtn.disabled = true; cancelBtn.textContent = "…";
        try { await BB.adminDelete(r.id, auth); reload(); }
        catch (e) { cancelBtn.disabled = false; cancelBtn.textContent = "Force-cancel"; alert("Failed: " + e.message); }
      });
      tb.appendChild(el("tr", {}, [
        el("td", { class: "mono7", text: r.id }),
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
