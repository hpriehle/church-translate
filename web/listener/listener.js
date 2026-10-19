/* Listener page: live only. French captions over WebSocket + the live French voice.
 *
 * The one control is mute / unmute. The first tap starts the live stream (phones only allow
 * sound after a tap). Muting just silences it while it keeps running, so unmuting is instant
 * and always at the live point. There is no history to scroll back through.
 *
 * Audio modes
 *   stream (default): one never-ending /stream.mp3 (like internet radio; iOS keeps playing it
 *                     with the screen locked). The server fills gaps with silence.
 *   clips (?mode=clips): fallback that plays each new clip as it arrives (never old ones).
 *
 * Everything reconnects by itself; captions resume within the current session only.
 */
(() => {
  "use strict";
  const params = new URLSearchParams(location.search);
  const MODE = params.get("mode") === "clips" ? "clips" : "stream";
  let lang = params.get("lang") || localStorage.getItem("lang") || "";   // "" = server default
  const KEEP_LINES = 6;
  const $ = (id) => document.getElementById(id);
  const captionsEl = $("captions"), waitingEl = $("waiting"), connEl = $("conn"), stateEl = $("audioState");
  const muteBtn = $("mute");

  // ------------------------------------------------------------------ captions (live only)
  let lastSeq = 0;
  let boot = null;          // server instance; caption numbers restart when it changes
  let session = null;       // translation run; only the current one is ever shown
  let live = false;

  function addCaption(c) {
    if (!c || typeof c.seq !== "number" || c.seq <= lastSeq) return;
    lastSeq = c.seq;
    const p = document.createElement("p");
    p.dataset.seq = String(c.seq);
    p.textContent = c.text || c.fr || "";
    captionsEl.appendChild(p);
    while (captionsEl.children.length > KEEP_LINES) captionsEl.removeChild(captionsEl.firstChild);
    const all = captionsEl.querySelectorAll("p");
    all.forEach((el, i) => el.classList.toggle("recent", i >= all.length - 2));
    updateWaiting();
  }

  function markOnAir(seq) {
    const prev = captionsEl.querySelector("p.onair");
    if (prev) prev.classList.remove("onair");
    const el = captionsEl.querySelector(`p[data-seq="${seq}"]`);
    if (el) el.classList.add("onair");
  }

  function clearCaptions() {
    captionsEl.innerHTML = "";
    lastSeq = 0;
  }

  let langOn = true;            // the operator can pause a language
  const LANG_OFF = { fr: "La traduction en français est arrêtée pour le moment.",
                     wo: "La traduction en wolof est arrêtée pour le moment." };
  function updateWaiting() {
    const any = captionsEl.children.length > 0;
    waitingEl.hidden = live && any && langOn;
    if (!langOn) waitingEl.textContent = LANG_OFF[lang] || "Cette traduction est arrêtée pour le moment.";
    else if (live) waitingEl.textContent = "En direct — la traduction va s'afficher ici.";
    else if (any) waitingEl.textContent = "La traduction est terminée.";
    else waitingEl.textContent = "La traduction commencera au début de la réunion.";
  }

  // ------------------------------------------------------------------ language picker
  let shownLang = null;
  const langsEl = $("langs");
  function renderLangs(list) {
    // Show the picker when there is a choice, or when this phone's language was paused.
    langsEl.hidden = list.length < 2 && list.some((l) => l.code === lang);
    langsEl.innerHTML = "";
    for (const l of list) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = l.name;
      b.setAttribute("aria-pressed", l.code === lang ? "true" : "false");
      b.onclick = () => switchLang(l.code);
      langsEl.appendChild(b);
    }
  }
  function switchLang(code) {
    if (code === lang) return;
    lang = code;
    localStorage.setItem("lang", code);
    langsEl.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", "false"));
    clearCaptions();
    // Reconnect the captions in the new language, and move the audio to that language's live stream.
    try { if (ws) { ws.onclose = null; ws.onmessage = null; ws.close(); } } catch {}
    ws = null;
    connect();
    clipQueue.length = 0;
    if (MODE === "stream" && audio.src && !audio.paused) startStream();
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
      ws.send(JSON.stringify({ type: "hello", last_seq: lastSeq, boot, session, lang, mode: MODE }));
      setConn("ok");
    };
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      lastPong = Date.now();
      switch (m.type) {
        case "init":
          // Different server, run or language than what's on screen: start clean.
          if (m.boot !== boot || m.session !== session || (m.lang && m.lang !== shownLang)) clearCaptions();
          if (m.lang) { lang = m.lang; shownLang = m.lang; }
          langOn = m.on !== false;
          renderLangs(m.languages || []);
          boot = m.boot;
          session = m.session;
          live = !!m.live;
          (m.history || []).forEach(addCaption);
          updateWaiting();
          break;
        case "session":
          // A new run started: wipe the previous one.
          if (m.session !== session) clearCaptions();
          session = m.session;
          live = true;
          updateWaiting();
          break;
        case "caption":
          if (m.session && session && m.session !== session) clearCaptions();
          if (m.session) session = m.session;
          addCaption(m);
          break;
        case "onair": markOnAir(m.seq); break;
        case "languages": renderLangs(m.languages || []); break;
        case "lang_state": langOn = !!m.on; updateWaiting(); break;
        case "status":
          live = !!m.live;
          if (!live) session = null;  // run over: a reconnect after this must not treat it as current
          updateWaiting();
          break;
        case "clip": if (MODE === "clips") enqueueClip(m); break;
      }
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
  window.addEventListener("online", () => { wsDelay = 500; connect(); if (soundOn) restartAudio(); });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    connect();
    if (soundOn) ensureAudio();
  });

  // ------------------------------------------------------------------ audio: mute / unmute
  const audio = new Audio();
  audio.preload = "none";
  audio.setAttribute("playsinline", "");
  audio.setAttribute("webkit-playsinline", "");
  let soundOn = false;          // what the listener chose
  let audioDelay = 1000, audioTimer = null;
  let lastTime = 0, stuckSince = 0;
  let ignorePauseUntil = 0;     // our own src changes also fire "pause"

  function setState(text, cls) {
    stateEl.textContent = text;
    stateEl.className = "state" + (cls ? " " + cls : "");
  }
  function showMute() {
    muteBtn.classList.toggle("on", soundOn);
    muteBtn.setAttribute("aria-pressed", soundOn ? "true" : "false");
    muteBtn.setAttribute("aria-label", soundOn ? "Couper le son" : "Activer le son");
    if (!soundOn) setState("Touchez pour écouter · utilisez des écouteurs");
  }

  function startStream() {
    ignorePauseUntil = Date.now() + 1500;
    audio.src = (lang ? `/stream/${lang}.mp3` : "/stream.mp3") + "?t=" + Date.now();   // always join at the live point
    lastTime = 0; stuckSince = 0;
    const p = audio.play();
    if (p) p.catch((err) => {
      if (err && err.name === "NotAllowedError") {
        soundOn = false; showMute();
        setState("Touchez le bouton pour activer le son", "warn");
      } else {
        restartAudio();
      }
    });
  }

  function restartAudio() {
    if (!soundOn) return;
    clearTimeout(audioTimer);
    setState("Reconnexion du son…", "warn");
    audioTimer = setTimeout(() => { if (soundOn) (MODE === "stream" ? startStream() : playNextClip()); }, audioDelay);
    audioDelay = Math.min(audioDelay * 2, 8000);
  }

  function ensureAudio() {
    if (MODE === "stream" && (audio.paused || audio.ended || audio.error)) restartAudio();
  }

  function unmute() {
    soundOn = true;
    audio.muted = false;
    audioDelay = 1000;
    showMute();
    if (MODE === "stream") {
      if (audio.paused || !audio.src) { setState("Chargement…"); startStream(); }
      else setState("En direct", "live");
    } else {
      unlockForClips();
      requestWakeLock();
    }
  }

  function mute() {
    soundOn = false;
    audio.muted = true;              // the stream keeps running silently, so unmuting is instantly live
    clipQueue.length = 0;            // clips mode: never play what was said while muted
    showMute();
    releaseWakeLock();
  }

  muteBtn.onclick = () => (soundOn ? mute() : unmute());

  audio.addEventListener("playing", () => { audioDelay = 1000; if (soundOn) setState("En direct", "live"); });
  audio.addEventListener("waiting", () => { if (soundOn) setState("Chargement…"); });
  audio.addEventListener("error", () => { if (soundOn && MODE === "stream") restartAudio(); });
  audio.addEventListener("ended", () => {
    if (MODE === "stream") { if (soundOn) restartAudio(); } else playNextClip();
  });
  audio.addEventListener("pause", () => {
    // Paused by the phone (call, headphones unplugged, lock-screen control): treat as muted.
    if (MODE !== "stream" || audio.ended || Date.now() < ignorePauseUntil || !soundOn) return;
    soundOn = false;
    showMute();
  });

  // Keep the stream at the live point: restart if it stalls (Wi-Fi drop) or drifts behind.
  setInterval(() => {
    if (MODE !== "stream" || !audio.src || audio.paused) return;
    const t = audio.currentTime;
    if (t > lastTime + 0.05) { lastTime = t; stuckSince = 0; }
    else if (!stuckSince) stuckSince = Date.now();
    else if (Date.now() - stuckSince > 7000) { stuckSince = 0; if (soundOn) restartAudio(); else startStream(); }
    try {
      const b = audio.buffered;
      if (b.length && b.end(b.length - 1) - t > 6) { if (soundOn) restartAudio(); else startStream(); }
    } catch {}
  }, 2000);

  // ------------------------------------------------------------------ clip mode (fallback)
  const clipQueue = [];
  let clipPlaying = false;
  function enqueueClip(m) {
    if (!soundOn) return;
    clipQueue.push(m.url);
    if (clipQueue.length > 2) clipQueue.splice(0, clipQueue.length - 1);  // behind: jump to the newest
    if (!clipPlaying) playNextClip();
  }
  function playNextClip() {
    const url = soundOn ? clipQueue.shift() : null;
    if (!url) { clipPlaying = false; if (soundOn) setState("En direct", "live"); return; }
    clipPlaying = true;
    ignorePauseUntil = Date.now() + 1500;
    audio.src = url;
    audio.play().catch(() => { clipPlaying = false; setState("Touchez le bouton pour activer le son", "warn"); });
  }
  function unlockForClips() {
    // Phones only allow sound after a tap: play a short silence on this element now.
    ignorePauseUntil = Date.now() + 1500;
    audio.src = "/static/listener/silence.mp3";
    audio.play().then(() => setState("En direct", "live")).catch(() => {});
  }
  let wakeLock = null;   // clips mode only: clip-by-clip playback needs the screen on
  async function requestWakeLock() {
    try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); } catch {}
  }
  function releaseWakeLock() { if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; } }

  // ------------------------------------------------------------------ lock screen
  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.metadata = new MediaMetadata({ title: "Traduction en direct", artist: "Français" });
      navigator.mediaSession.setActionHandler("play", () => unmute());
      navigator.mediaSession.setActionHandler("pause", () => { mute(); audio.pause(); });
      navigator.mediaSession.setActionHandler("stop", () => { mute(); audio.pause(); });
    } catch {}
  }

  // Read-only hook for automated browser tests (scripts/browser_check.mjs).
  window.__listener = { audio, get lastSeq() { return lastSeq; }, get listening() { return soundOn; }, mode: MODE };

  showMute();
  updateWaiting();
  connect();
})();
