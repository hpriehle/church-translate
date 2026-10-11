/* Speak page: sends this device's microphone to the server (/ws/speak) and shows what was
 * heard and how it was translated (from the operator feed). */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const KEY = params.get("key") || params.get("pin");
  const q = KEY ? "?key=" + encodeURIComponent(KEY) : "";
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
          if ("wakeLock" in navigator) navigator.wakeLock.request("screen").then((w) => { wakeLock = w; }).catch(() => {});
        } else if (m.type === "error") {
          $("err").textContent = m.message === "already running"
            ? "A translation run is already going (another speak page or the operator). Stop it first, or wait a few seconds."
            : m.message;
        }
      };
      ws.onclose = (ev) => {
        if (ev.code === 4403) $("err").textContent = "This page needs the key in its link (?key=…).";
        else if (sending) $("err").textContent = "Connection to the server dropped. Press Start to resume.";
        stopLocal();
      };
    } catch (e) {
      $("err").textContent = e && e.name === "NotAllowedError" ? "Microphone permission was refused." : "Could not start: " + (e.message || e);
      stopLocal();
    }
  }

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

  // ------------------------------------------------------------------ results feed (operator stream)
  const cards = new Map();
  const lags = [];
  function upsert(r) {
    let el = cards.get(r.seq);
    if (!el) {
      el = document.createElement("div");
      cards.set(r.seq, el);
      $("feed").prepend(el);
      while (cards.size > 120) { const k = cards.keys().next().value; cards.get(k).remove(); cards.delete(k); }
    }
    el.className = "card " + r.status;
    const meta = [];
    if (r.engine) meta.push(`<span class="eng ${esc(r.engine)}">${esc(r.engine)}</span>`);
    if (r.caption_lag != null) meta.push(`caption ${r.caption_lag.toFixed(1)}s after you paused`);
    if (r.air_lag != null) meta.push(`voice ${r.air_lag.toFixed(1)}s`);
    if (r.status === "skipped") meta.push("skipped (" + esc(r.note || "no speech") + ")");
    else if (r.note) meta.push(esc(r.note));
    el.innerHTML = `<div class="en">${esc(r.en) || "…"}</div><div class="fr">${esc(r.fr)}</div><div class="meta">${meta.join(" · ")}</div>`;
    if (r.status === "aired" && r.caption_lag != null && !el.dataset.c) {
      el.dataset.c = "1"; lags.push(r.caption_lag);
      const s = [...lags].sort((a, b) => a - b);
      $("lag").textContent = `${s[Math.floor(s.length / 2)].toFixed(1)} s (median of ${s.length})`;
    }
  }

  let urlShown = "";
  function status(st) {
    $("listeners").textContent = st.listeners.captions;
    $("backlog").textContent = st.pipeline.backlog_s.toFixed(1) + " s";
    $("claude").textContent = st.translate.claude.state + " · " + st.translate.claude.model;
    $("stt").textContent = st.pipeline.stt_ready ? "ready" : "loading…";
    if (st.listener_url && st.listener_url !== urlShown) {
      urlShown = st.listener_url;
      $("url").textContent = urlShown; $("url").href = urlShown;
      $("qr").src = "/qr.svg?t=" + Date.now();
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
