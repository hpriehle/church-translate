(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const PIN = new URLSearchParams(location.search).get("pin") || new URLSearchParams(location.search).get("key");
  // Opened through a public address (tunnel)? Then show that address, not the server's LAN one.
  const VIA_PUBLIC = !/^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.|\[|[\d.]+$)/.test(location.hostname);
  const shownUrl = (serverUrl) => (VIA_PUBLIC ? location.origin + "/" : serverUrl);
  const qrFor = (url) => "/api/qr?text=" + encodeURIComponent(url) + (PIN ? "&key=" + encodeURIComponent(PIN) : "");
  const withPin = (url) => PIN ? url + (url.includes("?") ? "&" : "?") + "pin=" + encodeURIComponent(PIN) : url;
  const api = async (path, body, method) => {
    const opts = body instanceof FormData ? { method: "POST", body }
      : body !== undefined ? { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : {};
    const r = await fetch(withPin(path), opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.statusText);
    return data;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  let state = null, devices = [], glossary = null, listenerUrl = "";

  // ------------------------------------------------------------------ log
  function log(text, level = "info") {
    const el = document.createElement("div");
    el.className = level;
    el.textContent = new Date().toLocaleTimeString() + "  " + text;
    $("log").prepend(el);
    while ($("log").children.length > 300) $("log").lastChild.remove();
  }

  // ------------------------------------------------------------------ settings form
  function fillSelect(sel, items, value) {
    sel.innerHTML = "";
    for (const it of items) {
      const o = document.createElement("option");
      o.value = it.value; o.textContent = it.label;
      if (String(it.value) === String(value)) o.selected = true;
      sel.appendChild(o);
    }
  }

  function updateChannels() {
    const d = devices.find((x) => x.name === $("device").value);
    const n = d ? d.channels : 2;
    const items = [{ value: 0, label: "Mix all" }];
    for (let i = 1; i <= n; i++) items.push({ value: i, label: "Input " + i });
    fillSelect($("channel"), items, state.settings.audio.input_channel);
  }

  async function load() {
    state = await api("/api/state");
    const s = state.settings;
    devices = state.devices;
    const devItems = devices.map((d) => ({ value: d.name, label: d.name + (d.default ? " (default)" : "") + ` · ${d.channels}ch` }));
    if (!devItems.length) devItems.push({ value: "", label: "No input devices found" });
    const devVal = s.audio.input_device || (devices.find((d) => d.default) || {}).name || "";
    fillSelect($("device"), devItems, devices.some((d) => d.name === devVal) ? devVal : (devItems[0] || {}).value);
    updateChannels();
    $("gain").value = s.audio.input_gain;

    fillSelect($("sample"), state.samples.map((x) => ({ value: x.name, label: `${x.name} (${x.mb} MB)` })), "");
    if (!state.samples.length) fillSelect($("sample"), [{ value: "", label: "No recordings in samples/ — upload one" }], "");

    const voices = state.voices.map((v) => ({ value: v.name, label: `${v.name} · ${v.lang}` }));
    if (!voices.length) voices.push({ value: "", label: s.tts.engine === "piper" ? "Piper voice" : "No French voices installed" });
    fillSelect($("voice"), voices, s.tts.voice || (state.status.pipeline.tts || {}).voice);
    $("rate").value = s.tts.rate; $("rateVal").textContent = s.tts.rate + " wpm";
    $("volume").value = s.tts.volume; $("volVal").textContent = Math.round(s.tts.volume * 100) + "%";
    $("mode").value = s.translate.mode === "ollama" ? "ollama" : "auto";
    $("claudeModel").value = s.translate.claude_model;
    $("timeout").value = s.translate.claude_timeout_s;
    $("ollamaModel").value = s.translate.ollama_model;

    const ips = state.lan_candidates.map((c) => ({ value: c.ip, label: `${c.ip} (${c.iface})` }));
    ips.unshift({ value: "", label: "Automatic" });
    fillSelect($("lanIp"), ips, s.server.lan_ip);

    glossary = state.glossary;
    renderGlossary();
    setUrl(state.listener_url);
    updateStatus(state.status);
  }

  function setUrl(serverUrl) {
    const url = shownUrl(serverUrl);
    if (url === listenerUrl) return;
    listenerUrl = url;
    $("url").textContent = url; $("url").href = url;
    $("qr").src = qrFor(url);
  }

  const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  async function saveSetting(section, values) {
    try { await api("/api/settings", { [section]: values }); log(`Saved ${section}: ${JSON.stringify(values)}`); }
    catch (e) { log("Settings: " + e.message, "error"); }
  }
  const saveSlow = debounce(saveSetting, 400);

  $("device").onchange = () => { updateChannels(); saveSetting("audio", { input_device: $("device").value }); };
  $("channel").onchange = () => saveSetting("audio", { input_channel: +$("channel").value });
  $("gain").onchange = () => saveSetting("audio", { input_gain: +$("gain").value });
  $("voice").onchange = () => saveSetting("tts", { voice: $("voice").value });
  $("rate").oninput = () => { $("rateVal").textContent = $("rate").value + " wpm"; saveSlow("tts", { rate: +$("rate").value }); };
  $("volume").oninput = () => { $("volVal").textContent = Math.round($("volume").value * 100) + "%"; saveSlow("tts", { volume: +$("volume").value }); };
  $("mode").onchange = () => saveSetting("translate", { mode: $("mode").value });
  $("claudeModel").onchange = () => saveSetting("translate", { claude_model: $("claudeModel").value });
  $("timeout").onchange = () => saveSetting("translate", { claude_timeout_s: +$("timeout").value });
  $("ollamaModel").onchange = () => saveSetting("translate", { ollama_model: $("ollamaModel").value.trim() });
  $("lanIp").onchange = async () => { await saveSetting("server", { lan_ip: $("lanIp").value }); listenerUrl = ""; };

  document.querySelectorAll("input[name=src]").forEach((r) => r.onchange = () => {
    const file = document.querySelector("input[name=src]:checked").value === "file";
    $("fileBox").hidden = !file; $("micBox").hidden = file;
  });

  $("upload").onchange = async () => {
    const f = $("upload").files[0];
    if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    log("Uploading " + f.name + "…");
    try {
      const r = await api("/api/upload", fd);
      fillSelect($("sample"), r.samples.map((x) => ({ value: x.name, label: `${x.name} (${x.mb} MB)` })), f.name);
      log("Uploaded " + f.name);
    } catch (e) { log("Upload failed: " + e.message, "error"); }
  };

  $("start").onclick = async () => {
    const src = document.querySelector("input[name=src]:checked").value;
    const body = { source: src, record: $("record").checked };
    if (src === "file") Object.assign(body, { file: $("sample").value, speed: +$("fileSpeed").value, start_s: +$("fileStart").value });
    else body.device = $("device").value;
    try { await api("/api/start", body); } catch (e) { log("Start failed: " + e.message, "error"); alert(e.message); }
  };
  $("stop").onclick = () => api("/api/stop", {}).catch((e) => log(e.message, "error"));
  $("testVoice").onclick = () => api("/api/test-voice", {}).then(() => log("Test sentence sent")).catch((e) => log(e.message, "error"));

  // ------------------------------------------------------------------ status
  let lastErr = "", lastClaudeErr = "", lastOllamaErr = "";
  function cls(el, c) { el.classList.remove("ok", "warn", "bad"); if (c) el.classList.add(c); }

  function updateStatus(st) {
    const p = st.pipeline, t = st.translate;
    const badge = $("live");
    if (p.running) { badge.className = "badge on"; badge.textContent = p.capturing ? "LIVE" : "FINISHING"; }
    else { badge.className = "badge off"; badge.textContent = "STOPPED"; }
    $("start").disabled = p.running; $("stop").disabled = !p.capturing;

    $("listeners").textContent = `${st.listeners.captions}` + (st.listeners.stream !== st.listeners.captions ? ` (🔊${st.listeners.stream})` : "");
    $("backlog").textContent = p.backlog_s.toFixed(1) + " s";
    cls($("backlog"), p.backlog_s > 15 ? "bad" : p.backlog_s > 8 ? "warn" : "ok");
    $("speed").textContent = p.speed.toFixed(2) + "×" + (p.condense ? " · condensing" : "");
    cls($("speed"), p.speed > 1 ? "warn" : "");
    $("level").style.width = Math.max(0, Math.min(100, (p.level_db + 60) / 60 * 100)) + "%";

    $("sttState").textContent = p.stt_ready ? `ready (${p.stt_engine})` : "loading…";
    cls($("sttState"), p.stt_ready ? "ok" : "warn");

    const c = t.claude;
    let ctext = c.state;
    if (c.session) ctext += ` · #${c.session} (${c.turns}/${c.rotate_at})`;
    if (c.state === "busy" && c.busy_for > 3) ctext += ` ${c.busy_for}s`;
    $("claudeState").textContent = ctext + " · " + c.model;
    cls($("claudeState"), c.state === "ready" || c.state === "busy" ? "ok" : c.state === "disabled" ? "" : c.state === "starting" ? "warn" : "bad");

    const o = t.ollama;
    $("ollamaState").textContent = o.warm ? "ready" : !o.available ? "not running" : !o.model_present ? "model missing" : "loading…";
    cls($("ollamaState"), o.warm ? "ok" : o.available ? "warn" : "bad");

    const u = t.usage;
    const kin = ((u.input + u.cache_read + u.cache_write) / 1000).toFixed(0);
    $("usage").textContent = u.turns ? `${u.turns} turns · ${kin}k in · ${(u.output / 1000).toFixed(1)}k out · $${u.cost_usd.toFixed(2)}` : "–";

    const counts = t.counts || {};
    $("engines").textContent = Object.keys(counts).length ? Object.entries(counts).map(([k, v]) => `${k} ${v}`).join(" · ") : "–";

    if (p.last_error && p.last_error !== lastErr) { log(p.last_error, "error"); }
    lastErr = p.last_error;
    if (t.last_error && t.last_error !== lastClaudeErr) log("Translator: " + t.last_error, "warn");
    lastClaudeErr = t.last_error;
    if (o.error && o.error !== lastOllamaErr) log("Ollama: " + o.error, "warn");
    lastOllamaErr = o.error;
    if (p.tts && p.tts.error) $("voice").title = p.tts.error;
    if (st.listener_url) setUrl(st.listener_url);
  }

  // ------------------------------------------------------------------ chunk table
  const rows = new Map();
  const capLags = [], airLags = [];
  let dropped = 0;
  const pct = (arr, q) => { if (!arr.length) return null; const a = [...arr].sort((x, y) => x - y); return a[Math.min(a.length - 1, Math.floor(q * a.length))]; };
  const fmtS = (x) => x == null ? "–" : x.toFixed(1);

  function upsertChunk(r) {
    let tr = rows.get(r.seq);
    if (!tr) {
      tr = document.createElement("tr");
      rows.set(r.seq, tr);
      $("chunks").tBodies[0].prepend(tr);
      while (rows.size > 400) { const k = rows.keys().next().value; rows.get(k).remove(); rows.delete(k); }
    }
    tr.className = r.status;
    const eng = r.engine ? `<span class="eng ${esc(r.engine)}">${esc(r.engine)}</span>` : "";
    const extra = [r.condensed ? "condensed" : "", r.speed > 1 ? r.speed.toFixed(2) + "×" : ""].filter(Boolean).join(" · ");
    const note = [r.note, r.status === "skipped" || r.status === "dropped" || r.status === "merged" ? r.status : ""].filter(Boolean).join(" — ");
    const ms = (x) => x ? Math.round(x) : "–";
    tr.innerHTML = `
      <td class="n" title="${esc(r.cut_reason)} · ${r.audio_dur_s}s audio">${r.seq}<br><small>${r.audio_dur_s}s</small></td>
      <td class="en">${esc(r.en)}</td>
      <td class="fr">${esc(r.fr)}${note ? `<span class="note">${esc(note)}</span>` : ""}</td>
      <td>${eng}${extra ? `<br><small class="muted">${esc(extra)}</small>` : ""}</td>
      <td class="t">${ms(r.stt_ms)} · ${ms(r.mt_ms)} · ${ms(r.tts_ms)}</td>
      <td class="t">${fmtS(r.caption_lag)} · ${r.status === "dropped" ? "dropped" : fmtS(r.air_lag)}</td>`;
    if (r.status === "aired" && r.air_lag != null && !tr.dataset.counted) {
      tr.dataset.counted = "1"; airLags.push(r.air_lag); if (r.caption_lag != null) capLags.push(r.caption_lag);
    }
    if (r.status === "dropped" && !tr.dataset.dropped) { tr.dataset.dropped = "1"; dropped++; if (r.caption_lag != null) capLags.push(r.caption_lag); }
    $("chunkCount").textContent = rows.size;
    $("dropped").textContent = dropped;
    $("capLag").textContent = capLags.length ? `p50 ${fmtS(pct(capLags, .5))}s · p95 ${fmtS(pct(capLags, .95))}s` : "–";
    $("airLag").textContent = airLags.length ? `p50 ${fmtS(pct(airLags, .5))}s · p95 ${fmtS(pct(airLags, .95))}s` : "–";
  }

  // ------------------------------------------------------------------ websocket
  let ws, wsDelay = 1000;
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(withPin(`${proto}://${location.host}/ws/operator`));
    ws.onopen = () => { wsDelay = 1000; log("Connected to server"); };
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "status") updateStatus(m);
      else if (m.type === "chunk") upsertChunk(m);
      else if (m.type === "log") log(m.text, m.level);
    };
    ws.onclose = () => { log("Disconnected from server — retrying", "warn"); setTimeout(connect, wsDelay); wsDelay = Math.min(wsDelay * 2, 8000); };
  }
  setInterval(() => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" })); }, 20000);

  // ------------------------------------------------------------------ glossary editor
  let gTab = "local_names";
  function renderGlossary() {
    $("localNames").value = (glossary.local_names || []).join("\n");
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === gTab));
    $("gLocal").hidden = gTab !== "local_names";
    $("gTable").hidden = gTab === "local_names";
    if (gTab === "local_names") return;
    const body = document.querySelector(".gt tbody");
    body.innerHTML = "";
    const filter = $("gFilter").value.toLowerCase();
    (glossary[gTab] || []).forEach((item, i) => {
      if (filter && !(item.en + " " + item.fr).toLowerCase().includes(filter)) return;
      const tr = document.createElement("tr");
      tr.innerHTML = `<td><input data-i="${i}" data-k="en" value="${esc(item.en)}"></td>
        <td><input data-i="${i}" data-k="fr" value="${esc(item.fr)}"${item.review ? ' title="Please have a French speaker check this"' : ""}></td>
        <td><button data-del="${i}" title="Delete">✕</button></td>`;
      body.appendChild(tr);
    });
  }
  document.querySelector(".gt tbody").addEventListener("input", (e) => {
    const t = e.target; if (!t.dataset.k) return;
    glossary[gTab][+t.dataset.i][t.dataset.k] = t.value;
  });
  document.querySelector(".gt tbody").addEventListener("click", (e) => {
    const i = e.target.dataset.del; if (i === undefined) return;
    glossary[gTab].splice(+i, 1); renderGlossary();
  });
  document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
    glossary.local_names = $("localNames").value.split("\n").map((x) => x.trim()).filter(Boolean);
    gTab = b.dataset.tab; renderGlossary();
  });
  $("gFilter").oninput = renderGlossary;
  $("gAdd").onclick = () => { glossary[gTab].unshift({ en: "", fr: "" }); $("gFilter").value = ""; renderGlossary(); };
  $("gSave").onclick = async () => {
    glossary.local_names = $("localNames").value.split("\n").map((x) => x.trim()).filter(Boolean);
    for (const k of ["terms", "names", "books"]) glossary[k] = (glossary[k] || []).filter((x) => x.en.trim() && x.fr.trim());
    try { const r = await api("/api/glossary", glossary); $("gMsg").textContent = r.message; log(r.message); renderGlossary(); }
    catch (e) { $("gMsg").textContent = e.message; log(e.message, "error"); }
  };

  load().then(connect).catch((e) => { log("Could not load state: " + e.message, "error"); connect(); });
})();
