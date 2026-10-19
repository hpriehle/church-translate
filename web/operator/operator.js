(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const PIN = new URLSearchParams(location.search).get("pin") || new URLSearchParams(location.search).get("key");
  // Opened through a public address (tunnel)? Then show that address, not the server's LAN one.
  const VIA_PUBLIC = !/^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.|\[|[\d.]+$)/.test(location.hostname);
  const shownUrl = (serverUrl) => (VIA_PUBLIC && !/^https:/.test(serverUrl || "") ? location.origin + "/" : serverUrl);
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
  let langs = [{ code: "fr", name: "Français" }];   // enabled output languages, from the server
  const SHORT = (c) => c.toUpperCase();

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

    langs = state.languages || langs;
    renderVoiceSelects(s);
    renderLangChecks(s);
    renderTableHead();
    $("rate").value = s.tts.rate; $("rateVal").textContent = s.tts.rate + " wpm";
    $("volume").value = s.tts.volume; $("volVal").textContent = Math.round(s.tts.volume * 100) + "%";
    $("mode").value = ["ollama", "claude"].includes(s.translate.mode) ? s.translate.mode : "auto";
    $("claudeModel").value = s.translate.claude_model;
    $("timeout").value = s.translate.claude_timeout_s;
    $("ollamaModel").value = s.translate.ollama_model;

    const links = [];
    if (s.server.public_url) links.push({ value: "public", label: `Public — ${s.server.public_url}` });
    links.push({ value: "lan:", label: "Local network (automatic)" });
    state.lan_candidates.forEach((c) => links.push({ value: "lan:" + c.ip, label: `Local network — ${c.ip} (${c.iface})` }));
    const usePublic = s.server.public_url && s.server.use_public_url !== false;
    fillSelect($("lanIp"), links, usePublic ? "public" : "lan:" + (s.server.lan_ip || ""));

    glossaries = state.glossaries || {};
    fillSelect($("gLang"), langs.map((l) => ({ value: l.code, label: l.name })), gLang);
    gLang = $("gLang").value;
    glossary = glossaries[gLang] || { terms: [], names: [], books: [], local_names: [] };
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

  // Claude credentials are write-only: the page only learns which kind is in use.
  const ACCESS = { token: "Subscription token (saved on this server)", api_key: "API key (saved on this server)",
                   login: "This computer's own Claude login" };
  async function saveAccess(kind, value) {
    try {
      const r = await api("/api/claude-access", { kind, value });
      $("credValue").value = "";
      $("claudeAccess").textContent = ACCESS[r.access] || r.access;
      log(r.message);
    } catch (e) { log("Claude access: " + e.message, "error"); }
  }
  $("credSave").onclick = () => { const v = $("credValue").value.trim(); if (v) saveAccess($("credKind").value, v); };
  $("credForget").onclick = () => { if (confirm("Remove the saved token or key and use this computer's own Claude login?")) saveAccess("login", ""); };

  $("device").onchange = () => { updateChannels(); saveSetting("audio", { input_device: $("device").value }); };
  $("channel").onchange = () => saveSetting("audio", { input_channel: +$("channel").value });
  $("gain").onchange = () => saveSetting("audio", { input_gain: +$("gain").value });
  // One voice menu per language. French lives in [tts]; others in [voices.<code>].
  function renderVoiceSelects(s) {
    const box = $("voiceSelects");
    box.innerHTML = "";
    for (const l of langs) {
      const lab = document.createElement("label");
      lab.textContent = `${l.name} voice `;
      const sel = document.createElement("select");
      const list = ((state.voices || {})[l.code] || []).map((v) => ({ value: v.id, label: v.label }));
      if (!list.length) list.push({ value: "", label: l.code === "wo" ? "Wolof voice not installed on this machine" : "No voices installed" });
      const chTts = ((state.status.pipeline.channels || {})[l.code] || {}).tts || {};
      const cur = l.code === "fr" ? `${s.tts.engine}:${chTts.voice || s.tts.voice}`
        : `${(s.voices[l.code] || {}).engine || chTts.engine || ""}:${chTts.voice || (s.voices[l.code] || {}).voice || ""}`;
      fillSelect(sel, list, cur);
      sel.dataset.lang = l.code;
      sel.onchange = () => {
        const [engine, ...rest] = sel.value.split(":");
        const voice = rest.join(":");
        if (l.code === "fr") saveSetting("tts", { engine, voice });
        else saveSetting("voices", { [l.code]: { engine, voice } });
      };
      lab.appendChild(sel);
      box.appendChild(lab);
    }
  }
  // One live on/off switch per language (no restart needed).
  function renderLangChecks(s) {
    const box = $("langChecks");
    box.innerHTML = "";
    for (const l of state.all_languages || langs) {
      const on = s.languages.includes(l.code);
      const lab = document.createElement("label");
      lab.className = "switch";
      lab.innerHTML = `<span>${esc(l.name)}</span><span class="state">${on ? "on" : "off"}</span>`
        + `<input type="checkbox" value="${l.code}"${on ? " checked" : ""} aria-label="Translate into ${esc(l.name)}">`;
      const input = lab.querySelector("input");
      input.onchange = async () => {
        const codes = [...box.querySelectorAll("input:checked")].map((x) => x.value);
        lab.querySelector(".state").textContent = input.checked ? "on" : "off";
        try {
          await api("/api/settings", { languages: codes });
          log(`${l.name} translation ${input.checked ? "on" : "off"}`);
        } catch (e) { log("Languages: " + e.message, "error"); input.checked = !input.checked; }
      };
      box.appendChild(lab);
    }
  }
  function syncLangSwitches(st) {
    const sw = st.language_switches;
    if (!sw) return;
    for (const x of sw) {
      const input = $("langChecks").querySelector(`input[value="${x.code}"]`);
      if (input && input.checked !== x.on) {
        input.checked = x.on;
        input.closest("label").querySelector(".state").textContent = x.on ? "on" : "off";
      }
    }
    const codes = (st.languages || []).map((l) => l.code).join(",");
    if (codes !== langs.map((l) => l.code).join(",")) {  // columns and voice menus follow the switches
      langs = st.languages || [];
      if (state) { state.languages = langs; state.settings.languages = langs.map((l) => l.code); renderTableHead(); renderVoiceSelects(state.settings); }
    }
  }
  $("rate").oninput = () => { $("rateVal").textContent = $("rate").value + " wpm"; saveSlow("tts", { rate: +$("rate").value }); };
  $("volume").oninput = () => { $("volVal").textContent = Math.round($("volume").value * 100) + "%"; saveSlow("tts", { volume: +$("volume").value }); };
  $("mode").onchange = () => saveSetting("translate", { mode: $("mode").value });
  $("claudeModel").onchange = () => saveSetting("translate", { claude_model: $("claudeModel").value });
  $("timeout").onchange = () => saveSetting("translate", { claude_timeout_s: +$("timeout").value });
  $("ollamaModel").onchange = () => saveSetting("translate", { ollama_model: $("ollamaModel").value.trim() });
  $("lanIp").onchange = async () => {
    const v = $("lanIp").value;
    await saveSetting("server", v === "public" ? { use_public_url: true } : { use_public_url: false, lan_ip: v.slice(4) });
    listenerUrl = "";
  };

  function showSource() {
    const v = document.querySelector("input[name=src]:checked").value;
    $("fileBox").hidden = v !== "file"; $("micBox").hidden = v !== "mic"; $("browserBox").hidden = v !== "browser";
    if (v === "browser") listBrowserMics(false);
  }
  document.querySelectorAll("input[name=src]").forEach((r) => r.onchange = showSource);

  // ------------------------------------------------------------------ microphone recorded by this browser
  // The browser has its own microphone permission, so this works even when the server runs as a
  // background service that macOS won't give the microphone to. Audio goes to /ws/speak.
  const bm = { ctx: null, stream: null, node: null, ws: null, pending: [], n: 0, active: false };

  async function listBrowserMics(ask) {
    if (!window.isSecureContext || !navigator.mediaDevices) {
      $("bhint").textContent = "The browser microphone needs this page at http://localhost:8000/operator (on the Mac) or the https link.";
      return;
    }
    try {
      if (ask) (await navigator.mediaDevices.getUserMedia({ audio: true })).getTracks().forEach((t) => t.stop());
      const mics = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "audioinput");
      const saved = localStorage.getItem("bmic") || "";
      const items = mics.map((d, i) => ({ value: d.deviceId, label: d.label || `Microphone ${i + 1}` }));
      if (!items.length) items.push({ value: "", label: "Default microphone" });
      fillSelect($("bdevice"), items, items.some((x) => x.value === saved) ? saved : items[0].value);
      if (!ask && mics.length && !mics[0].label) {
        // Names stay hidden until the page is allowed to use the microphone once.
        const o = document.createElement("option"); o.value = "__ask"; o.textContent = "Show microphone names…";
        $("bdevice").appendChild(o);
      }
    } catch (e) { log("Microphone list: " + e.message, "warn"); }
  }
  $("bdevice").onchange = () => {
    if ($("bdevice").value === "__ask") { listBrowserMics(true); return; }
    localStorage.setItem("bmic", $("bdevice").value);
  };
  navigator.mediaDevices && navigator.mediaDevices.addEventListener && navigator.mediaDevices.addEventListener("devicechange", () => listBrowserMics(false));

  function bmFlush() {
    if (!bm.n || !bm.ws || bm.ws.readyState !== WebSocket.OPEN) { bm.pending = []; bm.n = 0; return; }
    const out = new Int16Array(bm.n);
    let o = 0;
    for (const f of bm.pending) for (let i = 0; i < f.length; i++) {
      const v = Math.max(-1, Math.min(1, f[i]));
      out[o++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    bm.pending = []; bm.n = 0;
    bm.ws.send(out.buffer);
  }

  async function startBrowserMic(record) {
    const id = $("bdevice").value;
    const proc = $("bproc").checked;
    const constraints = { channelCount: 1, echoCancellation: proc, noiseSuppression: proc, autoGainControl: proc };
    if (id && id !== "__ask") constraints.deviceId = { exact: id };
    bm.stream = await navigator.mediaDevices.getUserMedia({ audio: constraints });
    listBrowserMics(false);
    bm.ctx = new (window.AudioContext || window.webkitAudioContext)();
    await bm.ctx.audioWorklet.addModule("/static/speak/capture-worklet.js");
    const src = bm.ctx.createMediaStreamSource(bm.stream);
    bm.node = new AudioWorkletNode(bm.ctx, "capture", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 });
    const mute = bm.ctx.createGain(); mute.gain.value = 0;
    src.connect(bm.node); bm.node.connect(mute); mute.connect(bm.ctx.destination);
    bm.node.port.onmessage = (e) => {
      if (!bm.active) return;
      bm.pending.push(e.data); bm.n += e.data.length;
      if (bm.n >= bm.ctx.sampleRate * 0.1) bmFlush();
    };
    const label = (bm.stream.getAudioTracks()[0] || {}).label || "browser";
    const proto = location.protocol === "https:" ? "wss" : "ws";
    await new Promise((resolve, reject) => {
      bm.ws = new WebSocket(withPin(`${proto}://${location.host}/ws/speak`));
      bm.ws.binaryType = "arraybuffer";
      bm.ws.onopen = () => bm.ws.send(JSON.stringify({ type: "hello", sample_rate: bm.ctx.sampleRate, device: label, record }));
      bm.ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "started") { bm.active = true; log(`Recording from ${label} (this browser)`); resolve(); }
        else if (m.type === "error") reject(new Error(m.message));
      };
      bm.ws.onclose = (ev) => {
        if (bm.active) log("Browser microphone connection closed — press Start to continue", "warn");
        stopBrowserMic(false);
        reject(new Error(ev.code === 4403 ? "not allowed (key missing)" : "connection closed"));
      };
    });
  }

  function stopBrowserMic(sendStop = true) {
    if (sendStop && bm.ws && bm.ws.readyState === WebSocket.OPEN) {
      bmFlush();
      try { bm.ws.send(JSON.stringify({ type: "stop" })); bm.ws.close(); } catch {}
    }
    bm.active = false;
    try { bm.node && bm.node.disconnect(); } catch {}
    try { bm.stream && bm.stream.getTracks().forEach((t) => t.stop()); } catch {}
    try { bm.ctx && bm.ctx.close(); } catch {}
    bm.node = bm.stream = bm.ctx = null;
  }
  window.addEventListener("pagehide", () => { if (bm.active) stopBrowserMic(); });

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
    if (src === "browser") {
      $("start").disabled = true;
      try { await startBrowserMic($("record").checked); }
      catch (e) {
        stopBrowserMic(false);
        log("Start failed: " + (e.name === "NotAllowedError" ? "the browser was not allowed to use the microphone" : e.message), "error");
        alert(e.name === "NotAllowedError" ? "Allow this page to use the microphone (browser address bar), then press Start again." : e.message);
        $("start").disabled = false;
      }
      return;
    }
    const body = { source: src, record: $("record").checked };
    if (src === "file") Object.assign(body, { file: $("sample").value, speed: +$("fileSpeed").value, start_s: +$("fileStart").value });
    else body.device = $("device").value;
    try { await api("/api/start", body); } catch (e) { log("Start failed: " + e.message, "error"); alert(e.message); }
  };
  $("stop").onclick = () => {
    if (bm.active) stopBrowserMic();
    api("/api/stop", {}).catch((e) => log(e.message, "error"));
  };
  $("testVoice").onclick = () => api("/api/test-voice", {}).then(() => log("Test sentence sent")).catch((e) => log(e.message, "error"));

  // ------------------------------------------------------------------ status
  let lastErr = "", lastClaudeErr = "", lastOllamaErr = "";
  const ttsErrs = {};
  function cls(el, c) { el.classList.remove("ok", "warn", "bad"); if (c) el.classList.add(c); }

  function updateStatus(st) {
    syncLangSwitches(st);
    const p = st.pipeline, t = st.translate;
    const badge = $("live");
    if (p.running) { badge.className = "badge on"; badge.textContent = p.capturing ? "LIVE" : "FINISHING"; }
    else { badge.className = "badge off"; badge.textContent = "STOPPED"; }
    $("start").disabled = p.running; $("stop").disabled = !p.capturing;

    $("listeners").textContent = `${st.listeners.captions}` + (st.listeners.stream !== st.listeners.captions ? ` (🔊${st.listeners.stream})` : "");
    const chans = p.channels || {};
    const codes = Object.keys(chans);
    const many = codes.length > 1;
    $("backlog").innerHTML = codes.map((c) => `${many ? `<small>${SHORT(c)}</small> ` : ""}${chans[c].backlog_s.toFixed(1)} s`).join(" · ") || "–";
    cls($("backlog"), p.backlog_s > 15 ? "bad" : p.backlog_s > 8 ? "warn" : "ok");
    const maxSpeed = Math.max(1, ...codes.map((c) => chans[c].speed));
    const condensing = codes.filter((c) => chans[c].condense).map(SHORT);
    $("speed").textContent = maxSpeed.toFixed(2) + "×" + (condensing.length ? ` · condensing ${condensing.join(", ")}` : "");
    cls($("speed"), maxSpeed > 1 ? "warn" : "");
    $("level").style.width = Math.max(0, Math.min(100, (p.level_db + 60) / 60 * 100)) + "%";

    $("sttState").textContent = p.stt_ready ? `ready (${p.stt_engine})` : "loading…";
    cls($("sttState"), p.stt_ready ? "ok" : "warn");

    const tl = Object.values(t);           // one translator status per language
    const parts = Object.entries(t).map(([code, x]) => {
      const c = x.claude;
      return `${many ? `<small>${SHORT(code)}</small> ` : ""}${c.state}${c.state === "busy" && c.busy_for > 3 ? ` ${c.busy_for}s` : ""}`;
    });
    $("claudeState").innerHTML = parts.join(" · ") + ` <small>${esc((tl[0] || {}).claude?.model || "")}</small>`;
    if (st.claude_access) {
      const signedOut = tl.some((x) => /not logged in|invalid api key|authentication|oauth token/i.test(x.last_error || ""));
      $("claudeAccess").textContent = (ACCESS[st.claude_access] || st.claude_access)
        + (signedOut ? " — Claude is not signed in: paste a token or key below" : "");
      cls($("claudeAccess"), signedOut ? "bad" : "");
    }
    const states = tl.map((x) => x.claude.state);
    cls($("claudeState"), states.every((x) => x === "ready" || x === "busy") ? "ok" : states.some((x) => x === "starting") ? "warn"
      : states.every((x) => x === "disabled") ? "" : "bad");

    const o = (tl.find((x) => x.ollama && x.ollama.used) || tl[0] || {}).ollama || {};
    $("ollamaState").textContent = o.warm ? "ready" : !o.available ? "not running" : !o.model_present ? "model missing"
      : o.warming ? "loading…" : "standby (loads on Start)";
    cls($("ollamaState"), o.warm ? "ok" : o.available && o.model_present ? (o.warming ? "warn" : "") : "bad");

    if (st.server) {
      const sv = st.server;
      if (bootSeen === null) bootSeen = sv.started;
      const since = new Date(sv.started * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
      $("serverInfo").textContent = `Running since ${since} · code ${sv.version || "?"} · started by ${sv.managed_by}`;
    }

    const u = tl.reduce((a, x) => { for (const k in x.usage) a[k] = (a[k] || 0) + x.usage[k]; return a; }, {});
    const kin = (((u.input || 0) + (u.cache_read || 0) + (u.cache_write || 0)) / 1000).toFixed(0);
    $("usage").textContent = u.turns ? `${u.turns} turns · ${kin}k in · ${((u.output || 0) / 1000).toFixed(1)}k out · $${(u.cost_usd || 0).toFixed(2)}` : "–";

    $("engines").innerHTML = Object.entries(t).map(([code, x]) => {
      const e = Object.entries(x.counts || {}).map(([k, v]) => `${k} ${v}`).join(", ");
      return e ? `${many ? `<small>${SHORT(code)}</small> ` : ""}${e}` : "";
    }).filter(Boolean).join(" · ") || "–";

    if (p.last_error && p.last_error !== lastErr) { log(p.last_error, "error"); }
    lastErr = p.last_error;
    const terr = tl.map((x) => x.last_error && `${SHORT(x.lang || "")}: ${x.last_error}`).filter(Boolean).join(" | ");
    if (terr && terr !== lastClaudeErr) log("Translator " + terr, "warn");
    lastClaudeErr = terr;
    if (o.error && o.error !== lastOllamaErr) log("Ollama: " + o.error, "warn");
    lastOllamaErr = o.error;
    for (const c of codes) {
      const err = chans[c].tts && chans[c].tts.error;
      if (err && err !== ttsErrs[c]) log(`Voice (${SHORT(c)}): ${err}`, "error");
      ttsErrs[c] = err;
    }
    if (st.listener_url) setUrl(st.listener_url);
    const tn = st.tunnel || {};
    $("tunnelState").textContent = tn.state === "connected" ? `connected (${tn.connections})`
      : tn.state === "offline" ? "offline — use Local network" : tn.state === "not running" ? "not on this machine" : "–";
    cls($("tunnelState"), tn.state === "connected" ? "ok" : tn.state === "offline" ? "bad" : "");
  }

  // ------------------------------------------------------------------ restart
  let bootSeen = null;
  function waitForRestart() {
    $("overlay").hidden = false;
    const t0 = Date.now();
    const poll = async () => {
      try {
        const h = await (await fetch("/health", { cache: "no-store" })).json();
        if (h.started && h.started !== bootSeen) { location.reload(); return; }
      } catch {}
      $("overlayText").textContent = `Restarting the server… ${Math.round((Date.now() - t0) / 1000)} s`;
      setTimeout(poll, 1000);
    };
    setTimeout(poll, 1500);
  }
  $("restart").onclick = async () => {
    if (!confirm("Restart the translation server?\n\nA run in progress stops. Phones reconnect by themselves in about 15 seconds.")) return;
    try {
      const h = await (await fetch("/health", { cache: "no-store" })).json();
      bootSeen = h.started ?? bootSeen;
      await api("/api/restart", {});
      waitForRestart();
    }
    catch (e) { log("Restart failed: " + e.message, "error"); }
  };

  // ------------------------------------------------------------------ chunk table
  const rows = new Map();
  const lags = {};          // code → {cap: [], air: []}
  let dropped = 0;
  const pct = (arr, q) => { if (!arr.length) return null; const a = [...arr].sort((x, y) => x - y); return a[Math.min(a.length - 1, Math.floor(q * a.length))]; };
  const fmtS = (x) => x == null ? "–" : x.toFixed(1);
  const ms = (x) => x ? Math.round(x) : "–";

  function renderTableHead() {
    const tr = $("chunks").tHead.rows[0];
    tr.innerHTML = `<th>#</th><th>English (heard)</th>` + langs.map((l) => `<th>${esc(l.name)}</th>`).join("");
  }

  function langCell(code, x) {
    if (!x) return `<td class="lang"></td>`;
    const eng = x.engine ? `<span class="eng ${esc(x.engine)}">${esc(x.engine)}</span> ` : "";
    const bits = [];
    if (x.mt_ms) bits.push(`MT ${ms(x.mt_ms)}`);
    if (x.tts_ms) bits.push(`voice ${ms(x.tts_ms)}`);
    if (x.caption_lag != null) bits.push(`caption ${fmtS(x.caption_lag)}s`);
    if (x.status === "dropped") bits.push("audio dropped");
    else if (x.air_lag != null) bits.push(`audio ${fmtS(x.air_lag)}s`);
    if (x.condensed) bits.push("condensed");
    if (x.speed > 1) bits.push(x.speed.toFixed(2) + "×");
    const note = [x.note, ["skipped", "merged", "failed"].includes(x.status) ? x.status : ""].filter(Boolean).join(" — ");
    return `<td class="lang ${esc(x.status)}">${esc(x.text)}${note ? `<span class="note">${esc(note)}</span>` : ""}`
      + `<span class="meta">${eng}${bits.join(" · ")}</span></td>`;
  }

  function upsertChunk(r) {
    let tr = rows.get(r.seq);
    if (!tr) {
      tr = document.createElement("tr");
      rows.set(r.seq, tr);
      $("chunks").tBodies[0].prepend(tr);
      while (rows.size > 400) { const k = rows.keys().next().value; rows.get(k).remove(); rows.delete(k); }
    }
    tr.className = r.status;
    const out = r.langs || {};
    tr.innerHTML = `
      <td class="n" title="${esc(r.cut_reason)} · ${r.audio_dur_s}s audio · STT ${ms(r.stt_ms)} ms">${r.seq}<br><small>${r.audio_dur_s}s</small></td>
      <td class="en">${esc(r.en)}${r.status === "skipped" ? `<span class="note">skipped — ${esc(r.note)}</span>` : ""}</td>`
      + langs.map((l) => langCell(l.code, out[l.code])).join("");
    for (const [code, x] of Object.entries(out)) {
      const key = `${code}-counted`;
      const L = (lags[code] = lags[code] || { cap: [], air: [] });
      if ((x.status === "aired" || x.status === "dropped") && !tr.dataset[key]) {
        tr.dataset[key] = "1";
        if (x.caption_lag != null) L.cap.push(x.caption_lag);
        if (x.status === "aired" && x.air_lag != null) L.air.push(x.air_lag);
        if (x.status === "dropped") dropped++;
      }
    }
    $("chunkCount").textContent = rows.size;
    $("dropped").textContent = dropped;
    const many = langs.length > 1;
    const show = (k) => langs.filter((l) => lags[l.code] && lags[l.code][k].length)
      .map((l) => `${many ? `<small>${SHORT(l.code)}</small> ` : ""}p50 ${fmtS(pct(lags[l.code][k], .5))}s · p95 ${fmtS(pct(lags[l.code][k], .95))}s`).join("<br>") || "–";
    $("capLag").innerHTML = show("cap");
    $("airLag").innerHTML = show("air");
  }

  // ------------------------------------------------------------------ websocket
  let ws, wsDelay = 1000;
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(withPin(`${proto}://${location.host}/ws/operator`));
    ws.onopen = () => { wsDelay = 1000; log("Connected to server"); };
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "restarting") { if ($("overlay").hidden) waitForRestart(); }
      else if (m.type === "status") updateStatus(m);
      else if (m.type === "chunk") upsertChunk(m);
      else if (m.type === "log") log(m.text, m.level);
    };
    ws.onclose = () => { log("Disconnected from server — retrying", "warn"); setTimeout(connect, wsDelay); wsDelay = Math.min(wsDelay * 2, 8000); };
  }
  setInterval(() => { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" })); }, 20000);

  // ------------------------------------------------------------------ glossary editor
  let gTab = "local_names";
  let gLang = "fr";
  let glossaries = {};
  $("gLang").onchange = () => {
    glossary.local_names = $("localNames").value.split("\n").map((x) => x.trim()).filter(Boolean);
    glossaries[gLang] = glossary;
    gLang = $("gLang").value;
    glossary = glossaries[gLang] || { terms: [], names: [], books: [], local_names: [] };
    renderGlossary();
  };
  function renderGlossary() {
    const lname = (langs.find((l) => l.code === gLang) || {}).name || gLang;
    document.querySelector(".gt thead tr").innerHTML = `<th>English</th><th>${esc(lname)}</th><th></th>`;
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
    try { const r = await api("/api/glossary?lang=" + gLang, glossary); glossaries[gLang] = glossary; $("gMsg").textContent = r.message; log(r.message); renderGlossary(); }
    catch (e) { $("gMsg").textContent = e.message; log(e.message, "error"); }
  };

  showSource();
  load().then(connect).catch((e) => { log("Could not load state: " + e.message, "error"); connect(); });
})();
