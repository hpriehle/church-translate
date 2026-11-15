/* Conversation page: two people, one laptop, English ↔ Spanish (server side: app/talk.py).
 * The microphone streams to /ws/talk only while someone may speak; the server sends back each turn's
 * text and its translation (as a WAV), which is played here, then a chime, then "played". */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const KEY = params.get("key") || params.get("pin");
  const q = KEY ? "?key=" + encodeURIComponent(KEY) : "";
  const wsUrl = (path) => (location.protocol === "https:" ? "wss://" : "ws://") + location.host + path + q;
  const NAME = { en: "English", es: "Spanish" };

  const BANNER = {
    idle:        ["Press Start", "One laptop between two people. Speak one at a time. · Hablen de uno en uno."],
    listening:   ["Go ahead · Adelante", "Speak in English or Spanish · Hable en inglés o español"],
    hearing:     ["Listening… · Escuchando…", "Pause when you're done · Haga una pausa al terminar"],
    translating: ["Translating… · Traduciendo…", "Please wait · Espere, por favor"],
    speaking:    ["🔊", "Please wait · Espere, por favor"],
  };

  let ctx = null, stream = null, node = null, ws = null, running = false;
  let state = "idle", forced = "", playing = null, pending = [], pendingLen = 0;
  let pauseS = parseFloat(localStorage.getItem("talk-pause") || "1.2");

  // ------------------------------------------------------------ banner and buttons
  function show(st, speakingLang) {
    state = st;
    const [big, small] = BANNER[st] || BANNER.idle;
    $("banner").className = st;
    $("b-big").textContent = st === "speaking"
      ? (speakingLang === "es" ? "🔊 En español…" : "🔊 In English…") : big;
    $("b-small").textContent = small;
    if (st !== "hearing" && st !== "listening") $("level").style.width = "0";
  }
  function setForced(lang) {
    forced = lang;
    $("force-en").classList.toggle("on", forced === "en");
    $("force-es").classList.toggle("on", forced === "es");
  }
  function setRunning(on) {
    running = on;
    $("start").classList.toggle("on", on);
    $("start").textContent = on ? "■ End" : "🎙 Start conversation";
    for (const id of ["force-en", "force-es", "reset"]) $(id).disabled = !on;
    if (!on) { show("idle"); setForced(""); }
  }

  // ------------------------------------------------------------ feed
  const turns = new Map();
  function turnEl(n) {
    let el = turns.get(n);
    if (!el) {
      const empty = $("feed").querySelector(".empty");
      if (empty) empty.remove();
      el = document.createElement("div");
      el.innerHTML = '<div class="said"></div><div class="tr">…</div><div class="meta"></div>';
      $("feed").appendChild(el);
      turns.set(n, el);
    }
    return el;
  }
  function onHeard(m) {
    const el = turnEl(m.turn);
    el.className = "turn " + m.lang;
    el.querySelector(".said").textContent = m.text;
    el.querySelector(".tr").textContent = "…";
    el.dataset.lang = m.lang;
    let how = m.forced ? `${NAME[m.lang]} (chosen)` : `heard as ${NAME[m.lang]}`;
    if (m.probs && !m.forced) how += ` · ${Math.round(100 * (m.probs[m.lang] || 0))}%`;
    el.querySelector(".meta").innerHTML = "";
    const span = document.createElement("span");
    span.textContent = how;
    el.querySelector(".meta").appendChild(span);
    // Only the latest turn can be heard again in the other language.
    document.querySelectorAll(".turn .redo").forEach((a) => a.remove());
    const other = m.lang === "en" ? "es" : "en";
    const a = document.createElement("a");
    a.className = "redo";
    a.textContent = `Wrong language? Redo as ${NAME[other]}`;
    a.onclick = () => redo(other);
    el.querySelector(".meta").appendChild(a);
    $("feed").scrollTop = $("feed").scrollHeight;
  }
  function onTranslated(m) {
    const el = turnEl(m.turn);
    el.querySelector(".tr").textContent = m.text || `(${m.note || "no translation"})`;
    if (m.timing) {
      const span = document.createElement("span");
      span.textContent = `${(m.timing.wait_ms / 1000).toFixed(1)} s`;
      span.title = `speech recognition ${m.timing.stt_ms} ms · translation ${m.timing.mt_ms} ms · voice ${m.timing.tts_ms} ms`;
      el.querySelector(".meta").insertBefore(span, el.querySelector(".redo"));
    }
    $("feed").scrollTop = $("feed").scrollHeight;
    if (m.audio) play(m.audio, m.lang);
  }

  // ------------------------------------------------------------ playback
  async function play(b64, lang) {
    stopPlayback(false);
    show("speaking", lang);
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    let buf;
    try { buf = await ctx.decodeAudioData(bytes.buffer); } catch { finishTurn(); return; }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.onended = () => { if (playing === src) { playing = null; finishTurn(); } };
    playing = src;
    src.start();
  }
  function stopPlayback(finish) {
    if (!playing) return;
    const p = playing;
    playing = null;
    try { p.stop(); } catch {}
    if (finish) finishTurn();
  }
  // "Your turn": two rising notes, so the person who can't see the screen knows the mic is open again.
  function chime() {
    return new Promise((resolve) => {
      const t = ctx.currentTime + 0.05;
      [[660, 0], [880, 0.11]].forEach(([f, dt]) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = f;
        g.gain.setValueAtTime(0, t + dt);
        g.gain.linearRampToValueAtTime(0.18, t + dt + 0.01);
        g.gain.exponentialRampToValueAtTime(0.001, t + dt + 0.1);
        o.connect(g); g.connect(ctx.destination);
        o.start(t + dt); o.stop(t + dt + 0.12);
      });
      setTimeout(resolve, 400);  // chime plus a little room echo, before the mic opens
    });
  }
  async function finishTurn() {
    await chime();
    send({ type: "played" });
  }

  // ------------------------------------------------------------ controls
  function send(msg) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg)); }
  function force(lang) { send({ type: "force", lang: forced === lang ? "" : lang }); }
  function redo(lang) { stopPlayback(false); send({ type: "redo", lang }); }

  $("force-en").onclick = () => force("en");
  $("force-es").onclick = () => force("es");
  $("reset").onclick = () => {
    stopPlayback(false);
    turns.clear();
    $("feed").innerHTML = '<div class="empty">New conversation. · Nueva conversación.</div>';
    send({ type: "reset" });
  };
  $("pause").value = pauseS;
  $("pause-v").textContent = pauseS.toFixed(1) + " s";
  $("pause").oninput = () => {
    pauseS = parseFloat($("pause").value);
    $("pause-v").textContent = pauseS.toFixed(1) + " s";
    localStorage.setItem("talk-pause", String(pauseS));
    send({ type: "config", end_pause_s: pauseS });
  };
  // Space only ever stops the voice: it must not press whichever button was clicked last.
  document.addEventListener("keydown", (e) => {
    if (e.code === "Space" && running && e.target.type !== "range") {
      e.preventDefault();
      if (playing) stopPlayback(true);
    }
  });
  document.addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") e.target.blur(); });

  // ------------------------------------------------------------ microphone
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
    $("start").disabled = true;
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
        // The mic is only sent while someone may speak: never during the translation or the chime.
        if (state !== "listening" && state !== "hearing") { pending = []; pendingLen = 0; return; }
        const f = e.data;
        let peak = 0;
        for (let i = 0; i < f.length; i += 4) peak = Math.max(peak, Math.abs(f[i]));
        const db = 20 * Math.log10(peak + 1e-6);
        $("level").style.width = Math.max(0, Math.min(100, (db + 60) / 60 * 100)) + "%";
        pending.push(f); pendingLen += f.length;
        if (pendingLen >= ctx.sampleRate * 0.1) flush();
      };
      ws = new WebSocket(wsUrl("/ws/talk"));
      ws.binaryType = "arraybuffer";
      ws.onopen = () => ws.send(JSON.stringify({ type: "hello", sample_rate: ctx.sampleRate, end_pause_s: pauseS }));
      ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "ready") { setRunning(true); $("start").disabled = false; }
        else if (m.type === "state") {
          if (m.state !== "speaking" || !playing) show(m.state === "speaking" ? "translating" : m.state);
          setForced(m.forced || "");
        }
        else if (m.type === "heard") onHeard(m);
        else if (m.type === "translated") onTranslated(m);
        else if (m.type === "dropped") { /* noise or nothing understood: just listen again */ }
        else if (m.type === "error") $("err").textContent = m.message;
      };
      ws.onclose = (ev) => {
        if (ev.code === 4403) $("err").textContent = "You are signed out. Reload this page and enter the password.";
        else if (running) $("err").textContent = "Connection to the server dropped. Press Start to continue.";
        stopLocal();
      };
    } catch (e) {
      $("err").textContent = e && e.name === "NotAllowedError" ? "Microphone permission was refused." : "Could not start: " + (e.message || e);
      stopLocal();
    }
  }

  function stopLocal() {
    stopPlayback(false);
    setRunning(false);
    $("start").disabled = false;
    try { node && node.disconnect(); } catch {}
    try { stream && stream.getTracks().forEach((t) => t.stop()); } catch {}
    try { ctx && ctx.close(); } catch {}
    node = stream = ctx = null;
  }

  $("start").onclick = () => {
    if (running) { try { ws && ws.close(); } catch {} stopLocal(); }
    else start();
  };
})();
