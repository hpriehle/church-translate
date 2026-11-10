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
  // Language from the link: /#french or /#wolof (what the printed QR codes use), then ?lang=, then
  // what this phone chose last time; "" = the server's default.
  const HASH_LANGS = { french: "fr", francais: "fr", "français": "fr", fr: "fr", wolof: "wo", wo: "wo",
                       spanish: "es", es: "es", russian: "ru", ru: "ru",
                       portuguese: "pt", pt: "pt", ukrainian: "uk", uk: "uk", persian: "fa", farsi: "fa", fa: "fa",
                       nepali: "ne", ne: "ne", vietnamese: "vi", vi: "vi", chinese: "zh", mandarin: "zh", zh: "zh" };
  const hashLang = () => HASH_LANGS[decodeURIComponent(location.hash.slice(1)).toLowerCase()] || "";
  let lang = hashLang() || params.get("lang") || localStorage.getItem("lang") || "";
  if (hashLang()) localStorage.setItem("lang", lang);
  // hearchurch.com/<slug>: a congregation's page. It follows that congregation's live session (when it has one)
  // and says to wait when it isn't live. Also the older /c/<slug>. Captions and sound come from /c/<slug>/….
  const SLUG = (location.pathname.match(/^\/(?:c\/)?([a-z0-9]+(?:-[a-z0-9]+)*)\/?$/) || [])[1];
  const BASE = SLUG ? "/c/" + SLUG : "";
  const $ = (id) => document.getElementById(id);
  // The page's own words: French for the legacy ward page; on /c/<slug> the phone's language (en/fr/es/ru).
  const UI_TEXT = {
    fr: { conn: ["Connecté", "Connexion…", "Reconnexion…"], mute: "Couper le son", unmute: "Activer le son",
          tap: "Touchez pour écouter · utilisez des écouteurs", tapBtn: "Touchez le bouton pour activer le son",
          notLive: "En attente", live: "En direct", loading: "Chargement…", title: "Traduction en direct",
          off: "Cette traduction est arrêtée pour le moment.", liveWait: "En direct — la traduction va s'afficher ici.",
          done: "La traduction est terminée.", begin: "La traduction commencera au début de la réunion.",
          speaker: "L'orateur parle {name}. Écoutez dans la salle." },
    en: { conn: ["Connected", "Connecting…", "Reconnecting…"], mute: "Mute", unmute: "Turn sound on",
          tap: "Tap to listen · use headphones", tapBtn: "Tap the button to turn the sound on",
          notLive: "Waiting", live: "Live", loading: "Loading…", title: "Live translation",
          off: "This translation is paused for now.", liveWait: "Live — the translation will appear here.",
          done: "The translation has ended.", begin: "The translation will begin when the meeting starts.",
          speaker: "The speaker is speaking {name}. Listen in the room." },
    es: { conn: ["Conectado", "Conectando…", "Reconectando…"], mute: "Silenciar", unmute: "Activar el sonido",
          tap: "Toque para escuchar · use auriculares", tapBtn: "Toque el botón para activar el sonido",
          notLive: "En espera", live: "En vivo", loading: "Cargando…", title: "Traducción en vivo",
          off: "Esta traducción está en pausa por ahora.", liveWait: "En vivo — la traducción aparecerá aquí.",
          done: "La traducción ha terminado.", begin: "La traducción empezará cuando comience la reunión.",
          speaker: "El orador está hablando {name}. Escuche en la sala." },
    ru: { conn: ["Подключено", "Подключение…", "Переподключение…"], mute: "Выключить звук", unmute: "Включить звук",
          tap: "Нажмите, чтобы слушать · используйте наушники", tapBtn: "Нажмите кнопку, чтобы включить звук",
          notLive: "Ожидание", live: "В эфире", loading: "Загрузка…", title: "Перевод в прямом эфире",
          off: "Этот перевод пока приостановлен.", liveWait: "В эфире — перевод появится здесь.",
          done: "Перевод завершён.", begin: "Перевод начнётся, когда начнётся собрание.",
          speaker: "Выступающий говорит на языке: {name}. Слушайте в зале." },
  };
  const U = BASE ? UI_TEXT[(navigator.language || "en").slice(0, 2)] || UI_TEXT.en : UI_TEXT.fr;
  if (BASE) {
    document.documentElement.lang = (navigator.language || "en").slice(0, 2);
    document.querySelector(".title").textContent = U.title;
    $("mute").setAttribute("aria-label", U.unmute);
  }
  let nextMeeting = null;   // {day, time, label} from the server
  const KEEP_LINES = 6;
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
  // The speaker talks in this phone's language right now: nothing to translate, listen in the room.
  let speaker = "en";
  function speakerText() {
    let name = speaker;
    try { name = new Intl.DisplayNames([document.documentElement.lang || "fr"], { type: "language" }).of(speaker); } catch {}
    return U.speaker.replace("{name}", name);
  }
  function updateWaiting() {
    const any = captionsEl.children.length > 0;
    const paused = live && speaker === lang;
    waitingEl.hidden = live && any && langOn && !paused;
    if (paused) waitingEl.textContent = speakerText();
    else if (!langOn) waitingEl.textContent = (!BASE && LANG_OFF[lang]) || U.off;
    else if (live) waitingEl.textContent = U.liveWait;
    else if (BASE && !any) waitingEl.textContent = notLiveText();
    else if (any) waitingEl.textContent = U.done;
    else waitingEl.textContent = U.begin;
  }

  const NOT_LIVE = {
    en: ["Wait for the meeting to start.", "Next meeting:"],
    fr: ["Attendez le début de la réunion.", "Prochaine réunion :"],
    es: ["Espere a que empiece la reunión.", "Próxima reunión:"],
    ru: ["Дождитесь начала собрания.", "Следующее собрание:"],
  };
  const DAY_INDEX = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  function notLiveText() {
    const code = (navigator.language || "en").slice(0, 2);
    const [none, nextLabel] = NOT_LIVE[code] || NOT_LIVE.en;
    const n = nextMeeting;
    if (!n) return none;
    let day = n.label;
    try {
      day = new Intl.DateTimeFormat(navigator.language, { weekday: "long" }).format(new Date(2024, 0, 1 + DAY_INDEX.indexOf(n.day))) + " " + n.time;
    } catch {}
    return `${none} ${nextLabel} ${day}`;
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
  window.addEventListener("hashchange", () => { const h = hashLang(); if (h) switchLang(h); });
  function switchLang(code) {
    if (code === lang) return;
    lang = code;
    if (!startOverlay.hidden) showStart(true);  // the Start button follows the language
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
    connEl.textContent = U.conn[state === "ok" ? 0 : state === "wait" ? 1 : 2];
  }

  function connect() {
    clearTimeout(wsTimer);
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    setConn("wait");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}${BASE}/ws/listen`);
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
          nextMeeting = m.next_meeting || null;
          if (m.congregation) {
            document.title = m.congregation.name;
            const t = document.querySelector("header .title");
            if (t) t.textContent = m.congregation.name;
          }
          langOn = m.on !== false;
          speaker = m.speaker || "en";
          renderLangs(m.languages || []);
          boot = m.boot;
          session = m.session;
          live = !!m.live;
          (m.history || []).forEach(addCaption);
          updateWaiting();
          if (BASE && live && soundOn) ensureAudio();
          break;
        case "reload":   // the congregation just went live: join its session
          try { ws.onclose = null; ws.close(); } catch {}
          ws = null;
          connect();
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
        case "speaker": speaker = m.speaker || "en"; updateWaiting(); break;
        case "status":
          live = !!m.live;
          if (!live) session = null;  // run over: a reconnect after this must not treat it as current
          updateWaiting();
          if (BASE && !live) setTimeout(() => { try { ws.onclose = null; ws.close(); } catch {} ws = null; connect(); }, 2500);
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
    muteBtn.setAttribute("aria-label", soundOn ? U.mute : U.unmute);
    if (!soundOn) setState(U.tap);
  }

  function startStream() {
    if (BASE && !live) {   // nothing to hear until the congregation goes live
      audio.removeAttribute("src");
      setState(U.notLive, "");
      return;
    }
    ignorePauseUntil = Date.now() + 1500;
    audio.src = (lang ? `${BASE}/stream/${lang}.mp3` : "/stream.mp3") + "?t=" + Date.now();   // always join at the live point
    lastTime = 0; stuckSince = 0;
    const p = audio.play();
    if (p) p.catch((err) => {
      if (err && err.name === "NotAllowedError") {
        soundOn = false; showMute();
        setState(U.tapBtn, "warn");
        showStart(true);
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
      if (audio.paused || !audio.src) { setState(U.loading); startStream(); }
      else setState(U.live, "live");
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

  // Sound is on by default. Phones (iPhones always) only allow sound after a tap, so when the
  // phone refuses, a full-screen Start button in the listener's language asks for that one tap.
  const START_TEXT = {
    fr: { label: "Commencer", hint: "Touchez pour écouter la traduction en français. Utilisez des écouteurs." },
    wo: { label: "Tàmbali", hint: "Bësal fii ngir déglu tekki bi ci wolof. Jëfandikool ay écouteurs." },
    es: { label: "Empezar", hint: "Toque para escuchar la traducción en español. Use auriculares." },
    en: { label: "Start", hint: "Tap to hear the translation in English. Use headphones." },
    ru: { label: "Начать", hint: "Нажмите, чтобы слушать перевод на русский. Используйте наушники." },
  };
  const startOverlay = $("startOverlay");
  function showStart(on) {
    const t = START_TEXT[lang] || START_TEXT.fr;
    $("startLabel").textContent = t.label;
    $("startHint").textContent = t.hint;
    startOverlay.hidden = !on;
  }
  $("startBtn").onclick = () => { showStart(false); unmute(); };

  audio.addEventListener("playing", () => { audioDelay = 1000; if (soundOn) setState(U.live, "live"); });
  audio.addEventListener("waiting", () => { if (soundOn) setState(U.loading); });
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
    if (!url) { clipPlaying = false; if (soundOn) setState(U.live, "live"); return; }
    clipPlaying = true;
    ignorePauseUntil = Date.now() + 1500;
    audio.src = url;
    audio.play().catch(() => { clipPlaying = false; setState(U.tapBtn, "warn"); });
  }
  function unlockForClips() {
    // Phones only allow sound after a tap: play a short silence on this element now.
    ignorePauseUntil = Date.now() + 1500;
    audio.src = "/static/listener/silence.mp3";
    audio.play().then(() => setState(U.live, "live")).catch(() => {});
  }
  let wakeLock = null;   // clips mode only: clip-by-clip playback needs the screen on
  async function requestWakeLock() {
    try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); } catch {}
  }
  function releaseWakeLock() { if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; } }

  // ------------------------------------------------------------------ lock screen
  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.metadata = new MediaMetadata({ title: U.title, artist: BASE ? "" : "Français" });
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
  if (MODE === "stream") unmute();   // try to start the sound right away (the Start button appears if refused)
  else showStart(true);
})();
