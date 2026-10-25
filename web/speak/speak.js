/* Speak page: one button that sends this device's microphone to the server (/ws/speak), with a
 * level meter and a one-line status (from the operator feed). Everything else is on /operator. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const KEY = params.get("key") || params.get("pin");
  const q = KEY ? "?key=" + encodeURIComponent(KEY) : "";
  // Opened through a public address (tunnel)? Then show that address, not the server's LAN one.
  const VIA_PUBLIC = !/^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.|\[|[\d.]+$)/.test(location.hostname);
  const shownUrl = (serverUrl) => (VIA_PUBLIC && !/^https:/.test(serverUrl || "") ? location.origin + "/" : serverUrl);
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

  // ------------------------------------------------------------------ status line (operator feed)
  function status(st) {
    const hub = (st.engine || {}).hub;
    const where = hub && hub.running ? hub.name : "";
    const n = st.listeners.captions;
    const live = st.pipeline.running || (hub && hub.running);
    $("status").innerHTML = live
      ? `<b>${n}</b> listener${n === 1 ? "" : "s"}` + (where ? ` · running on ${esc(where)}` : "")
      : `${n} listener${n === 1 ? "" : "s"} connected`;
    const url = shownUrl(st.listener_url);
    if (url) $("listenUrl").textContent = url.replace(/^https?:\/\//, "").replace(/\/$/, "");
  }

  let feedDelay = 1000, f = null;
  setInterval(() => { if (f && f.readyState === 1) f.send(JSON.stringify({ type: "ping" })); }, 20000);
  function connectFeed() {
    f = new WebSocket(wsUrl("/ws/operator"));
    f.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "status") status(m);
    };
    f.onopen = () => { feedDelay = 1000; };
    f.onclose = (ev) => {
      if (ev.code === 4403) { $("err").textContent = "You are signed out. Reload this page and enter the password."; return; }
      setTimeout(connectFeed, feedDelay); feedDelay = Math.min(feedDelay * 2, 8000);
    };
  }
  connectFeed();
  window.addEventListener("pagehide", () => { if (sending) stop(); });
})();
