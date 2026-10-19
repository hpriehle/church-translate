/* Speak page: sends this device's microphone to the server (/ws/speak) and shows what was
 * heard and how it was translated (from the operator feed). */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const KEY = params.get("key") || params.get("pin");
  const q = KEY ? "?key=" + encodeURIComponent(KEY) : "";
  // Opened through a public address (tunnel)? Then show that address, not the server's LAN one.
  const VIA_PUBLIC = !/^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.|\[|[\d.]+$)/.test(location.hostname);
  const shownUrl = (serverUrl) => (VIA_PUBLIC && !/^https:/.test(serverUrl || "") ? location.origin + "/" : serverUrl);
  const qrFor = (url) => "/api/qr?text=" + encodeURIComponent(url) + (KEY ? "&key=" + encodeURIComponent(KEY) : "");
  const wsUrl = (path) => (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path + q;
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  let ctx = null, stream = null, node = null, ws = null, sending = false, wakeLock = null;
  let pending = [], pendingLen = 0;

  function setPill(text, live) { $("pill").textContent = text; $("pill").className = "pill" + (live ? " live" : ""); }
  function setButton() {
    $("btn").classList.toggle("on", sending);
    $("btn").textContent = sending ? "■ Stop" : "🎙 Start speaking";
  }

  function flush() {
    if (!pendingLen || !ws || ws.readyState !== WebSocket.OPEN) { pending = []; pendingLen = 0; return; }
    const out = new Int16Array(pendingLen);
    let o = 0;
    for (const f of pending) for (let i = 0; i < f.length; i++) {
      const v = Math.max(-1, Math.min(1, f[i]));
      out[o++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    pending = []; pendingLen = 0;
    ws.send(out.buffer);
  }

  async function start() {
    $("err").textContent = "";
    $("btn").disabled = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      await ctx.audioWorklet.addModule("/static/speak/capture-worklet.js");
      const src = ctx.createMediaStreamSource(stream);
      node = new AudioWorkletNode(ctx, "capture", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 });
      const mute = ctx.createGain();
      mute.gain.value = 0;
      src.connect(node); node.connect(mute); mute.connect(ctx.destination);
      node.port.onmessage = (e) => {
        const f = e.data;
        let peak = 0;
        for (let i = 0; i < f.length; i += 4) peak = Math.max(peak, Math.abs(f[i]));
        const db = 20 * Math.log10(peak + 1e-6);
        $("level").style.width = Math.max(0, Math.min(100, (db + 60) / 60 * 100)) + "%";
        if (!sending) return;
        pending.push(f); pendingLen += f.length;
        if (pendingLen >= ctx.sampleRate * 0.1) flush();
      };
      const label = (stream.getAudioTracks()[0] || {}).label || "web page";
      ws = new WebSocket(wsUrl("/ws/speak"));
      ws.binaryType = "arraybuffer";
      ws.onopen = () => ws.send(JSON.stringify({ type: "hello", sample_rate: ctx.sampleRate, device: label }));
      ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "started") {
          sending = true; setButton(); setPill("● LIVE", true); $("btn").disabled = false;
          if ("wakeLock" in navigator) navigator.wakeLock.request("screen").then((w) => { wakeLock = w; w.onrelease = () => { wakeLock = null; }; }).catch(() => {});
        } else if (m.type === "error") {
          $("err").textContent = m.message === "already running"
            ? "A translation run is already going (another speak page or the operator). Stop it first, or wait a few seconds."
            : m.message;
        }
      };
      ws.onclose = (ev) => {
        if (ev.code === 4403) $("err").textContent = "You are signed out. Reload this page and enter the password.";
        else if (sending) $("err").textContent = "Connection to the server dropped. Press Start to resume.";
        stopLocal();
      };
    } catch (e) {
      $("err").textContent = e && e.name === "NotAllowedError" ? "Microphone permission was refused." : "Could not start: " + (e.message || e);
      stopLocal();
    }
  }

  // Phones release the screen lock when the page is hidden; take it again when it comes back.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && sending && !wakeLock && "wakeLock" in navigator)
      navigator.wakeLock.request("screen").then((w) => { wakeLock = w; w.onrelease = () => { wakeLock = null; }; }).catch(() => {});
  });

  function stopLocal() {
    sending = false;
    setButton(); setPill("stopped");
    $("btn").disabled = false;
    $("level").style.width = "0";
    try { node && node.disconnect(); } catch {}
    try { stream && stream.getTracks().forEach((t) => t.stop()); } catch {}
    try { ctx && ctx.close(); } catch {}
    if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
    node = stream = ctx = null;
  }

  function stop() {
    flush();
    if (ws && ws.readyState === WebSocket.OPEN) { ws.send(JSON.stringify({ type: "stop" })); ws.close(); }
    stopLocal();
  }

  $("btn").onclick = () => (sending ? stop() : start());

  // ------------------------------------------------------------------ voices (same settings as the operator page)
  const withKey = (path) => KEY ? path + (path.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(KEY) : path;
  async function post(path, body) {
    const r = await fetch(withKey(path), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || r.statusText);
    return d;
  }
  const voiceMsg = (t) => { $("voiceMsg").textContent = t; if (t) setTimeout(() => { if ($("voiceMsg").textContent === t) $("voiceMsg").textContent = ""; }, 4000); };

  async function loadVoices() {
    let st;
    try { st = await (await fetch(withKey("/api/state"))).json(); } catch { return; }
    renderSwitches(st);
    const box = $("voiceSelects");
    box.innerHTML = "";
    const chans = (st.status && st.status.pipeline && st.status.pipeline.channels) || {};
    for (const l of st.languages || []) {
      const label = document.createElement("label");
      label.textContent = `${({ fr: "French", wo: "Wolof" })[l.code] || l.name} voice`;
      const sel = document.createElement("select");
      const list = (st.voices[l.code] || []).map((v) => ({ value: v.id, label: v.label }));
      if (!list.length) list.push({ value: "", label: "No voice installed" });
      const ch = (chans[l.code] || {}).tts || {};
      const cur = `${ch.engine}:${ch.voice}`;
      for (const v of list) {
        const o = document.createElement("option");
        o.value = v.value; o.textContent = v.label; o.selected = v.value === cur;
        sel.appendChild(o);
      }
      sel.onchange = async () => {
        const [engine, ...rest] = sel.value.split(":");
        const voice = rest.join(":");
        try {
          await post("/api/settings", l.code === "fr" ? { tts: { engine, voice } } : { voices: { [l.code]: { engine, voice } } });
          voiceMsg(`${l.name}: ${sel.options[sel.selectedIndex].textContent}`);
        } catch (e) { voiceMsg("Could not change voice: " + e.message); }
      };
      label.appendChild(sel);
      box.appendChild(label);
      if (l.code !== "fr") {  // speed for this language's voice
        const sp = document.createElement("label");
        const range = document.createElement("input");
        Object.assign(range, { type: "range", min: "0.8", max: "1.5", step: "0.05" });
        range.value = ((st.settings.voices || {})[l.code] || {}).speed || 1;
        const show = () => { sp.firstChild.textContent = `${l.name} speed ${Math.round(+range.value * 100)}%`; };
        sp.appendChild(document.createTextNode(""));
        show();
        let t = null;
        range.oninput = () => {
          show();
          clearTimeout(t);
          t = setTimeout(async () => {
            const [engine, ...rest] = sel.value.split(":");
            try { await post("/api/settings", { voices: { [l.code]: { engine, voice: rest.join(":"), speed: +range.value } } }); }
            catch (e) { voiceMsg("Could not change speed: " + e.message); }
          }, 400);
        };
        sel.onchange = async () => {  // keep the chosen speed when the voice changes
          const [engine, ...rest] = sel.value.split(":");
          try {
            await post("/api/settings", { voices: { [l.code]: { engine, voice: rest.join(":"), speed: +range.value } } });
            voiceMsg(`${l.name}: ${sel.options[sel.selectedIndex].textContent}`);
          } catch (e) { voiceMsg("Could not change voice: " + e.message); }
        };
        sp.appendChild(range);
        box.appendChild(sp);
      }
    }
  }
  // Same live on/off switches as the operator page.
  function renderSwitches(st) {
    const box = $("langSwitches");
    box.innerHTML = "";
    for (const x of (st.status && st.status.language_switches) || []) {
      const lab = document.createElement("label");
      lab.className = "switch";
      lab.innerHTML = `<span>${esc(x.name)}</span><input type="checkbox" value="${x.code}"${x.on ? " checked" : ""}>`;
      lab.querySelector("input").onchange = async () => {
        const codes = [...box.querySelectorAll("input:checked")].map((i) => i.value);
        try { await post("/api/settings", { languages: codes }); loadVoices(); }
        catch (e) { voiceMsg("Could not switch: " + e.message); }
      };
      box.appendChild(lab);
    }
  }
  $("testVoice").onclick = async () => {
    try { await post("/api/test-voice", {}); voiceMsg("Playing on the listening page…"); }
    catch (e) { voiceMsg("Test failed: " + e.message); }
  };
  loadVoices();

  // ------------------------------------------------------------------ results feed (operator stream)
  const cards = new Map();
  const lags = {};
  function upsert(r) {
    let el = cards.get(r.seq);
    if (!el) {
      el = document.createElement("div");
      cards.set(r.seq, el);
      $("feed").prepend(el);
      while (cards.size > 120) { const k = cards.keys().next().value; cards.get(k).remove(); cards.delete(k); }
    }
    el.className = "card " + r.status;
    const out = r.langs || {};
    const many = Object.keys(out).length > 1;
    let html = `<div class="en">${esc(r.en) || "…"}</div>`;
    if (r.status === "skipped") html += `<div class="meta">skipped (${esc(r.note || "no speech")})</div>`;
    for (const [code, x] of Object.entries(out)) {
      const meta = [];
      if (x.engine) meta.push(`<span class="eng ${esc(x.engine)}">${esc(x.engine)}</span>`);
      if (x.caption_lag != null) meta.push(`caption ${x.caption_lag.toFixed(1)}s after you paused`);
      if (x.air_lag != null) meta.push(`voice ${x.air_lag.toFixed(1)}s`);
      if (["skipped", "failed", "merged", "dropped"].includes(x.status)) meta.push(esc(x.status + (x.note ? " — " + x.note : "")));
      html += `<div class="fr">${many ? `<span class="lc">${code.toUpperCase()}</span> ` : ""}${esc(x.text)}</div>`
        + `<div class="meta">${meta.join(" · ")}</div>`;
      if (x.status === "aired" && x.caption_lag != null && !el.dataset["c" + code]) {
        el.dataset["c" + code] = "1";
        (lags[code] = lags[code] || []).push(x.caption_lag);
      }
    }
    el.innerHTML = html;
    $("lag").textContent = Object.entries(lags).map(([code, a]) => {
      const sorted = [...a].sort((p, q) => p - q);
      return `${Object.keys(lags).length > 1 ? code.toUpperCase() + " " : ""}${sorted[Math.floor(sorted.length / 2)].toFixed(1)} s`;
    }).join(" · ") || "–";
  }

  let urlShown = "";
  function status(st) {
    $("listeners").textContent = st.listeners.captions;
    $("backlog").textContent = st.pipeline.backlog_s.toFixed(1) + " s";
    $("claude").textContent = st.translate.claude.state + " · " + st.translate.claude.model;
    $("stt").textContent = st.pipeline.stt_ready ? "ready" : "loading…";
    const url = shownUrl(st.listener_url);
    if (url && url !== urlShown) {
      urlShown = url;
      $("url").textContent = urlShown; $("url").href = urlShown;
      $("qr").src = qrFor(urlShown);
    }
  }

  let feedDelay = 1000, f = null;
  setInterval(() => { if (f && f.readyState === 1) f.send(JSON.stringify({ type: "ping" })); }, 20000);
  function connectFeed() {
    f = new WebSocket(wsUrl("/ws/operator"));
    f.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "status") status(m);
      else if (m.type === "chunk") upsert(m);
    };
    f.onopen = () => { feedDelay = 1000; };
    f.onclose = (ev) => {
      if (ev.code === 4403) { $("err").textContent = "This page needs the key in its link (?key=…)."; return; }
      setTimeout(connectFeed, feedDelay); feedDelay = Math.min(feedDelay * 2, 8000);
    };
  }
  connectFeed();
  window.addEventListener("pagehide", () => { if (sending) stop(); });
})();
