// Fills the live columns of the server table from the Haulnet registry monitor (ADR: the page stays static;
// without the monitor it still lists every server). No libraries, no third-party requests.
(() => {
  "use strict";
  const table = document.querySelector("table[data-status-url]");
  const note = document.getElementById("status-note");
  if (!table || !note || !window.fetch) return;
  const url = table.dataset.statusUrl;
  const REFRESH_MS = 60000;
  const STALE_S = 600;

  const ago = (iso, now) => {
    const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
    if (s < 90) return `${s} s`;
    if (s < 5400) return `${Math.round(s / 60)} min`;
    if (s < 172800) return `${Math.round(s / 3600)} h`;
    return `${Math.round(s / 86400)} days`;
  };
  const percent = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(v >= 0.9995 || v === 0 ? 0 : 1)} %`);
  const set = (row, name, text, cls, title) => {
    const cell = row.querySelector(`[data-col="${name}"]`);
    if (!cell) return;
    cell.textContent = text;
    cell.className = cls || "";
    if (title) cell.title = title; else cell.removeAttribute("title");
  };
  const reasons = {
    timeout: "no answer within 5 s", refused: "connection refused", unreachable: "network unreachable",
    dns: "name does not resolve", not_public: "no public address", no_ipv4: "IPv6 only (the monitor uses IPv4)",
    http_503: "LuxonServer is not running", http_error: "unexpected HTTP answer", bad_answer: "not a haulnet-server answer",
    too_large: "answer too large",
  };

  const clear = () => {
    historyLoaded = 0;
    for (const row of table.tBodies[0].rows) {
      if (!row.dataset.id) continue;
      for (const col of ["state", "load", "version", "uptime", "history"]) set(row, col, "—");
    }
  };

  const render = (doc) => {
    const now = Date.now();
    const byId = new Map(doc.servers.map((s) => [s.id, s]));
    for (const row of table.tBodies[0].rows) {
      const s = byId.get(row.dataset.id);
      if (!s) continue;
      const st = s.status;
      const why = s.last_error ? reasons[s.last_error] || s.last_error : "";
      if (s.state === "online") {
        let text = "Online", cls = "ok";
        if (s.compatible === false) { text = "Outdated"; cls = "warn"; }
        else if (st && st.draining) { text = "Updating"; cls = "warn"; }
        else if (st && !st.accepting) { text = "Full"; cls = "warn"; }
        const extra = s.last_error ? ` · last check failed: ${why}` : "";
        const ping = s.connect_ms !== null ? ` · connect ${Math.round(s.connect_ms)} ms from the monitor` : "";
        set(row, "state", text, cls, `Online for ${ago(s.since, now)}${ping}${extra}`);
      } else if (s.state === "offline") {
        set(row, "state", `Offline for ${ago(s.since, now)}`, "bad", why);
      } else {
        set(row, "state", "Checking…", "", why);
      }
      if (st && s.state === "online") {
        const players = st.max_players ? `${st.players}/${st.max_players}` : `${st.players}`;
        const rooms = st.max_rooms ? `${st.rooms}/${st.max_rooms}` : `${st.rooms}`;
        set(row, "load", `${players} players`, "", `${rooms} games`);
        set(row, "version", st.haulnet_server, s.compatible === false ? "warn" : "",
          `haulnet-server ${st.haulnet_server}, LuxonServer ${st.luxon}${s.compatible === false ? " (not supported by the current game)" : ""}`);
      } else {
        set(row, "load", "—");
        set(row, "version", st ? st.haulnet_server : "—", "", st ? `last seen: LuxonServer ${st.luxon}` : "");
      }
      set(row, "uptime", percent(s.uptime["30d"]), "", `24 h: ${percent(s.uptime["24h"])} · 7 days: ${percent(s.uptime["7d"])}`);
    }
    const where = doc.monitor.location ? ` in ${doc.monitor.location}` : "";
    const age = (now - Date.parse(doc.generated_at)) / 1000;
    note.textContent = age > STALE_S
      ? `Live status is out of date: the monitor's last check was ${ago(doc.generated_at, now)} ago.`
      : `Live status from the Haulnet monitor${where} (${doc.monitor.address_family}), checked every ` +
        `${doc.monitor.interval_s < 60 ? `${doc.monitor.interval_s} s` : `${Math.round(doc.monitor.interval_s / 60)} min`}; ` +
        `last check ${ago(doc.generated_at, now)} ago. ` +
        `It shows whether a server answers, not your ping: the game measures that itself.`;
  };

  // History: one bar per UTC day for the last 30 days, loaded once per page view and every 10 minutes.
  const DAYS = 30;
  const HISTORY_MS = 600000;
  const SVG = "http://www.w3.org/2000/svg";
  let historyLoaded = 0;
  const historyUrl = (id) => url.replace(/status\.json$/, `history/${encodeURIComponent(id)}.json`);
  const dayKey = (t) => new Date(t).toISOString().slice(0, 10);

  const drawHistory = (cell, doc, now) => {
    const byDate = new Map(doc.days.map((d) => [d.date, d]));
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", `0 0 ${DAYS * 5} 18`);
    svg.setAttribute("class", "bars");
    svg.setAttribute("role", "img");
    let known = 0, good = 0;
    for (let i = 0; i < DAYS; i++) {
      const date = dayKey(now - (DAYS - 1 - i) * 86400000);
      const d = byDate.get(date);
      const rect = document.createElementNS(SVG, "rect");
      rect.setAttribute("x", String(i * 5));
      rect.setAttribute("width", "3.5");
      rect.setAttribute("rx", "1");
      const title = document.createElementNS(SVG, "title");
      if (d && d.checks > 0) {
        const share = d.ok / d.checks;
        known += d.checks; good += d.ok;
        rect.setAttribute("y", "0");
        rect.setAttribute("height", "18");
        rect.setAttribute("class", share >= 0.99 ? "ok" : share >= 0.9 ? "warn" : "bad");
        const players = d.players_max === null ? "" : `, up to ${d.players_max} players`;
        title.textContent = `${date}: ${percent(share)} answered${players}`;
      } else {
        rect.setAttribute("y", "8");
        rect.setAttribute("height", "2");
        rect.setAttribute("class", "none");
        title.textContent = `${date}: no data`;
      }
      rect.appendChild(title);
      svg.appendChild(rect);
    }
    svg.setAttribute("aria-label", known ? `Last ${DAYS} days: ${percent(good / known)} of checks answered` : "No history yet");
    cell.textContent = "";
    cell.className = "";
    cell.appendChild(svg);
  };

  const loadHistory = (servers) => {
    const now = Date.now();
    if (now - historyLoaded < HISTORY_MS) return;
    historyLoaded = now;
    for (const row of table.tBodies[0].rows) {
      const cell = row.querySelector('[data-col="history"]');
      if (!cell || !servers.some((s) => s.id === row.dataset.id)) continue;
      fetch(historyUrl(row.dataset.id))
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((doc) => {
          if (doc.schema_version !== 1 || !Array.isArray(doc.days)) throw new Error("unknown format");
          drawHistory(cell, doc, now);
        })
        .catch(() => { cell.textContent = "—"; });
    }
  };

  const load = () => {
    fetch(url, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((doc) => {
        if (doc.schema_version !== 1 || !Array.isArray(doc.servers)) throw new Error("unknown format");
        render(doc);
        loadHistory(doc.servers);
      })
      .catch(() => {
        clear();
        note.textContent = "Live status is unavailable right now. The list itself is up to date.";
      });
  };
  load();
  setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
})();
