/* Listener page: live French captions over WebSocket + French audio.
 *
 * Audio modes
 *   stream (default): one never-ending <audio src="/stream.mp3">, like internet radio. iOS keeps
 *                     playing it with the screen locked. The server fills gaps with silence.
 *   clips (?mode=clips): fallback that plays each translated clip in turn on the same element.
 *
 * Everything reconnects by itself: captions resume from the last one seen, audio restarts
 * at "live" (never replays old audio).
 */
(() => {
  "use strict";
  const MODE = new URLSearchParams(location.search).get("mode") === "clips" ? "clips" : "stream";
  const $ = (id) => document.getElementById(id);
  const captionsEl = $("captions"), waitingEl = $("waiting"), jumpEl = $("jump");
  const connEl = $("conn"), listenBtn = $("listen"), listenLabel = $("listenLabel"), stateEl = $("audioState");
  const awakeBox = $("awake");

  // ------------------------------------------------------------------ text size
  let scale = parseFloat(localStorage.getItem("scale") || "1");
  const applyScale = () => document.documentElement.style.setProperty("--scale", String(scale));
  applyScale();
  $("smaller").onclick = () => { scale = Math.max(0.7, +(scale - 0.1).toFixed(2)); localStorage.setItem("scale", scale); applyScale(); };
  $("bigger").onclick = () => { scale = Math.min(2.2, +(scale + 0.1).toFixed(2)); localStorage.setItem("scale", scale); applyScale(); };

  // ------------------------------------------------------------------ captions
  let lastSeq = 0;
  let boot = null;          // server instance; caption numbers restart when it changes
  let live = false;
  let followLive = true;

  function nearBottom() {
    return captionsEl.scrollHeight - captionsEl.scrollTop - captionsEl.clientHeight < 80;
  }
  captionsEl.addEventListener("scroll", () => {
    followLive = nearBottom();
    jumpEl.hidden = followLive;
  }, { passive: true });
  jumpEl.onclick = () => { followLive = true; jumpEl.hidden = true; captionsEl.scrollTop = captionsEl.scrollHeight; };

  function addCaption(c) {
    if (!c || typeof c.seq !== "number" || c.seq <= lastSeq) return;
    lastSeq = c.seq;
    const p = document.createElement("p");
    p.dataset.seq = String(c.seq);
    p.textContent = c.fr;
    captionsEl.appendChild(p);
    const all = captionsEl.querySelectorAll("p");
    all.forEach((el, i) => el.classList.toggle("recent", i >= all.length - 2));
    while (captionsEl.children.length > 250) captionsEl.removeChild(captionsEl.firstChild);
    updateWaiting();
    if (followLive) captionsEl.scrollTop = captionsEl.scrollHeight;
  }

  function markOnAir(seq) {
    const prev = captionsEl.querySelector("p.onair");
    if (prev) prev.classList.remove("onair");
    const el = captionsEl.querySelector(`p[data-seq="${seq}"]`);
    if (el) el.classList.add("onair");
  }

  function updateWaiting() {
    waitingEl.hidden = live || captionsEl.children.length > 0;
    if (!live && captionsEl.children.length > 0) {
      waitingEl.hidden = false;
      waitingEl.textContent = "La traduction est en pause.";
    } else {
      waitingEl.textContent = "La traduction commencera au début de la réunion.";
    }
  }

  // ------------------------------------------------------------------ websocket
  let ws = null, wsDelay = 1000, wsTimer = null, lastPong = Date.now();

  function setConn(state) {
    connEl.className = "pill " + (state === "ok" ? "ok" : state === "wait" ? "wait" : "bad");
    connEl.textContent = state === "ok" ? "Connecté" : state === "wait" ? "Connexion…" : "Reconnexion…";
  }

  function connect() {
    clearTimeout(wsTimer);
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    setConn("wait");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/listen`);
    ws.onopen = () => {
      wsDelay = 1000;
      lastPong = Date.now();
      ws.send(JSON.stringify({ type: "hello", last_seq: lastSeq, boot, mode: MODE }));
      setConn("ok");
    };
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      switch (m.type) {
        case "init":
          if (boot !== null && m.boot !== boot) {
            // Server restarted: keep the text on screen, but its numbering starts again.
            lastSeq = 0;
            captionsEl.querySelectorAll("p[data-seq]").forEach((p) => p.removeAttribute("data-seq"));
          }
          boot = m.boot;
          live = !!m.live; (m.history || []).forEach(addCaption); updateWaiting(); break;
        case "caption": addCaption(m); break;
        case "onair": markOnAir(m.seq); break;
        case "status": live = !!m.live; updateWaiting(); break;
        case "clip": if (MODE === "clips") enqueueClip(m); break;
        case "pong": lastPong = Date.now(); break;
      }
      lastPong = Date.now();
    };
    ws.onclose = () => { setConn("bad"); scheduleReconnect(); };
    ws.onerror = () => { try { ws.close(); } catch {} };
  }
  function scheduleReconnect() {
    clearTimeout(wsTimer);
    wsTimer = setTimeout(connect, wsDelay);
    wsDelay = Math.min(wsDelay * 2, 8000);
  }
  setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "ping", t: Date.now() }));
      if (Date.now() - lastPong > 45000) { try { ws.close(); } catch {} }
    }
  }, 15000);
  window.addEventListener("online", () => { wsDelay = 500; connect(); if (listening) restartAudio("online"); });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    connect();
    if (listening) { ensureAudio(); if (awakeBox.checked) requestWakeLock(); }
  });

  // ------------------------------------------------------------------ audio
  const audio = new Audio();
  audio.preload = "none";
  audio.setAttribute("playsinline", "");
  audio.setAttribute("webkit-playsinline", "");
  let listening = false;
  let audioDelay = 1000, audioTimer = null;
  let lastTime = 0, stuckSince = 0;
  let ignorePauseUntil = 0;   // our own src changes also fire "pause"

  function setState(text, cls) {
    stateEl.textContent = text;
    stateEl.className = "state" + (cls ? " " + cls : "");
  }
  function updateButton() {
    listenBtn.classList.toggle("on", listening);
    listenBtn.querySelector(".icon").textContent = listening ? "■" : "▶";
    listenLabel.textContent = listening ? "Arrêter le son" : "Écouter";
    if (!listening) setState("🎧 Utilisez des écouteurs");
  }

  function startStream() {
    ignorePauseUntil = Date.now() + 1500;
    audio.src = "/stream.mp3?t=" + Date.now();
    lastTime = 0; stuckSince = 0;
    const p = audio.play();
    if (p) p.catch((err) => {
      if (err && err.name === "NotAllowedError") {
        listening = false; updateButton();
        setState("Touchez « Écouter » pour activer le son", "warn");
      } else {
        restartAudio("play failed");
      }
    });
  }

  function stopAudio() {
    clearTimeout(audioTimer);
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    clipQueue.length = 0;
    clipPlaying = false;
  }

  function restartAudio(why) {
    if (!listening) return;
    clearTimeout(audioTimer);
    setState("Reconnexion du son…", "warn");
    audioTimer = setTimeout(() => {
      if (!listening) return;
      if (MODE === "stream") startStream(); else playNextClip();
    }, audioDelay);
    audioDelay = Math.min(audioDelay * 2, 8000);
  }

  function ensureAudio() {
    if (MODE === "stream" && (audio.paused || audio.ended || audio.error)) restartAudio("ensure");
  }

  audio.addEventListener("playing", () => { audioDelay = 1000; setState("● En direct", "live"); });
  audio.addEventListener("waiting", () => { if (listening) setState("Chargement…"); });
  audio.addEventListener("error", () => { if (listening && MODE === "stream") restartAudio("error"); });
  audio.addEventListener("ended", () => {
    if (!listening) return;
    if (MODE === "stream") restartAudio("ended"); else playNextClip();
  });
  audio.addEventListener("pause", () => {
    // Paused by the system (phone call, headphones unplugged) rather than by us.
    if (!listening || MODE !== "stream" || audio.ended || Date.now() < ignorePauseUntil) return;
    listening = false;
    updateButton();
    setState("En pause — touchez « Écouter »", "warn");
  });

  // Watchdog: a dropped Wi-Fi often stalls the stream without an error event.
  setInterval(() => {
    if (!listening || MODE !== "stream" || audio.paused) return;
    const t = audio.currentTime;
    if (t > lastTime + 0.05) { lastTime = t; stuckSince = 0; }
    else if (!stuckSince) stuckSince = Date.now();
    else if (Date.now() - stuckSince > 7000) { stuckSince = 0; restartAudio("stalled"); }
    // If this phone has drifted behind (buffered audio piling up), jump back to live.
    try {
      const b = audio.buffered;
      if (b.length && b.end(b.length - 1) - t > 8) restartAudio("drift");
    } catch {}
  }, 2000);

  // ------------------------------------------------------------------ clip mode
  const clipQueue = [];
  let clipPlaying = false;
  function enqueueClip(m) {
    if (!listening) return;
    clipQueue.push(m.url);
    if (clipQueue.length > 3) clipQueue.splice(0, clipQueue.length - 2); // fell behind: skip ahead
    if (!clipPlaying) playNextClip();
  }
  function playNextClip() {
    const url = clipQueue.shift();
    if (!url) { clipPlaying = false; if (listening) setState("● En direct", "live"); return; }
    clipPlaying = true;
    ignorePauseUntil = Date.now() + 1500;
    audio.src = url;
    audio.play().catch(() => { clipPlaying = false; setState("Touchez « Écouter » pour activer le son", "warn"); });
  }
  function unlockForClips() {
    // iOS only lets a page play audio after a tap; play a short silence on this element now.
    ignorePauseUntil = Date.now() + 1500;
    audio.src = "/static/listener/silence.mp3";
    audio.play().then(() => setState("● En direct", "live")).catch(() => {});
  }

  // ------------------------------------------------------------------ wake lock + media session
  let wakeLock = null;
  async function requestWakeLock() {
    try {
      if ("wakeLock" in navigator) {
        wakeLock = await navigator.wakeLock.request("screen");
        wakeLock.addEventListener("release", () => { wakeLock = null; });
      }
    } catch { /* unsupported or refused */ }
  }
  function releaseWakeLock() { if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; } }
  awakeBox.checked = MODE === "clips" || localStorage.getItem("awake") === "1";
  awakeBox.onchange = () => {
    localStorage.setItem("awake", awakeBox.checked ? "1" : "0");
    if (awakeBox.checked && listening) requestWakeLock(); else releaseWakeLock();
  };

  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.metadata = new MediaMetadata({ title: "Traduction en direct", artist: "Français" });
      navigator.mediaSession.setActionHandler("play", () => { listening = true; updateButton(); startListening(); });
      navigator.mediaSession.setActionHandler("pause", () => { listening = false; stopAudio(); updateButton(); });
      navigator.mediaSession.setActionHandler("stop", () => { listening = false; stopAudio(); updateButton(); });
    } catch {}
  }

  function startListening() {
    audioDelay = 1000;
    setState("Chargement…");
    if (MODE === "stream") startStream(); else unlockForClips();
    if (awakeBox.checked) requestWakeLock();
  }

  listenBtn.onclick = () => {
    listening = !listening;
    updateButton();
    if (listening) startListening(); else { stopAudio(); releaseWakeLock(); }
  };

  // Read-only hook for automated browser tests (scripts/browser_check.mjs).
  window.__listener = { audio, get lastSeq() { return lastSeq; }, get listening() { return listening; }, mode: MODE };

  updateWaiting();
  connect();
})();
