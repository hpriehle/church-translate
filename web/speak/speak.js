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
    cards.clear(); $("feed").innerHTML = ""; lags.length = 0;  // a new meeting starts with an empty feed
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

  // ------------------------------------------------------------------ live translation (operator feed)
  const cards = new Map();
  const lags = [];
  function upsert(r) {
    let el = cards.get(r.seq);
    if (!el) {
      el = document.createElement("div");
      cards.set(r.seq, el);
      $("feed").prepend(el);
      while (cards.size > 60) { const k = cards.keys().next().value; cards.get(k).remove(); cards.delete(k); }
    }
    el.className = "card " + r.status;
    const out = r.langs || {};
    const many = Object.keys(out).length > 1;
    let html = `<div class="en">${esc(r.en) || "…"}</div>`;
    for (const [code, x] of Object.entries(out)) {
      const note = ["skipped", "failed", "dropped"].includes(x.status) ? `<div class="meta">${esc(x.status)}</div>` : "";
      html += `<div class="fr">${many ? `<span class="lc">${code.toUpperCase()}</span>` : ""}${esc(x.text) || "…"}</div>${note}`;
      if (x.status === "aired" && x.caption_lag != null && !el.dataset["c" + code]) {
        el.dataset["c" + code] = "1";
        lags.push(x.caption_lag);
        if (lags.length > 20) lags.shift();
      }
    }
    el.innerHTML = html;
  }

  // ------------------------------------------------------------------ status line
  function status(st) {
    const hub = (st.engine || {}).hub;
    const where = hub && hub.running ? hub.name : "";
    const n = st.listeners.captions;
    const live = st.pipeline.running || (hub && hub.running);
    const lag = lags.length ? [...lags].sort((a, b) => a - b)[Math.floor(lags.length / 2)] : null;
    $("status").innerHTML = live
      ? `<b>${n}</b> listener${n === 1 ? "" : "s"}` + (lag != null ? ` · <span id="lag">${lag.toFixed(1)} s</span> behind` : "")
        + (where ? ` · on ${esc(where)}` : "")
      : `${n} listener${n === 1 ? "" : "s"} connected`;
    const url = shownUrl(st.listener_url);
    if (url) $("listenUrl").textContent = url.replace(/^https?:\/\//, "").replace(/\/$/, "");
    const notice = ((st.engine || {}).client || {}).notice || "";
    if (notice !== lastNotice) {   // the hub refused or ended the meeting (e.g. out of credit): say so, refresh credit
      lastNotice = notice;
      if (notice) $("err").textContent = notice;
      loadBilling();
    }
  }

  // ------------------------------------------------------------------ credit (Hear Church prepaid credits)
  let lastNotice = "", fastUntil = 0;
  const money = (usd) => (usd < 0 ? "-$" : "$") + Math.abs(usd).toFixed(2);
  async function loadBilling() {
    let b;
    try {
      const r = await fetch("/api/billing" + q, { cache: "no-store" });
      if (!r.ok) return;
      b = await r.json();
    } catch { return; }
    if (!b || !b.billing) { $("credit").hidden = true; return; }
    $("credit").hidden = false;
    const out = !!b.out_of_credit;
    $("credit").classList.toggle("out", out);
    if (b.billing !== "credits") {
      $("creditLine").textContent = "Own Anthropic key";
      $("creditSmall").textContent = b.message || "";
      $("buyRow").hidden = true;
      return;
    }
    $("creditLine").textContent = out ? "Out of credit: buy credit to start the next meeting"
      : `${money(b.balance_usd)} credit · ${b.language_hours_left.toFixed(1)} language-hours left`;
    $("creditSmall").textContent = (out ? `Balance ${money(b.balance_usd)}. ` : "")
      + (b.balance.free_left_min > 0 ? `Includes ${(b.balance.free_left_min / 60).toFixed(1)} free language-hours. ` : "")
      + `$${b.usd_per_language_hour} per language-hour of speech. A meeting that's running is never cut off.`
      + (b.can_buy ? "" : " Buying credit isn't available yet.");
    $("buyRow").hidden = !b.can_buy;
    if (b.can_buy && !$("buyUsd").options.length)
      for (const usd of b.buy_usd) $("buyUsd").add(new Option(`$${usd} · ${usd / b.usd_per_language_hour} h`, usd));
  }

  $("buyBtn").onclick = async () => {
    $("buyBtn").disabled = true;
    const tab = window.open("", "_blank");   // opened now, while it's still a click: popup blockers allow it
    try {
      const r = await fetch("/api/buy-credit" + q, { method: "POST", headers: { "Content-Type": "application/json" },
                                                     body: JSON.stringify({ usd: Number($("buyUsd").value) }) });
      const a = await r.json().catch(() => ({}));
      if (!r.ok || !a.url) throw new Error(a.message || a.detail || "Couldn't open the payment page.");
      if (tab) tab.location = a.url; else location.href = a.url;
      fastUntil = Date.now() + 10 * 60 * 1000;   // the credit arrives a few seconds after paying
    } catch (e) {
      if (tab) tab.close();
      $("err").textContent = e.message || String(e);
    } finally {
      $("buyBtn").disabled = false;
    }
  };
  setInterval(() => { if (Date.now() < fastUntil) loadBilling(); }, 10000);
  setInterval(() => { if (Date.now() >= fastUntil) loadBilling(); }, 60000);
  loadBilling();

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
      if (ev.code === 4403) { $("err").textContent = "You are signed out. Reload this page and enter the password."; return; }
      setTimeout(connectFeed, feedDelay); feedDelay = Math.min(feedDelay * 2, 8000);
    };
  }
  connectFeed();
  window.addEventListener("pagehide", () => { if (sending) stop(); });
})();
