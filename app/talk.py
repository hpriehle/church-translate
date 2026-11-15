"""One-on-one conversation (/talk): two people sharing one laptop, English ↔ Spanish.

Strict turn-taking, so nobody talks over the translation and the laptop never translates itself:

    listening → hearing (someone speaks) → translating (after a pause) → speaking → listening
                                            mic ignored from here ────────────────┘

The page plays the translation, then a short chime, and only then reports "played"; until that
message arrives every incoming audio frame is dropped. Each turn is transcribed and translated
whole (not in 3–8 s fragments as in meetings): a turn reads better and the wait is short.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np

from .chunker import FRAME, SR, SileroVad
from .config import LOG_DIR, Settings, TtsCfg
from .translate.claude_session import ClaudeSession
from .translate.router import SESSION_TYPES
from .translate.prompts import clean_output

log = logging.getLogger("talk")

LANGS = {"en": "English", "es": "Español"}
# Preferred Piper voices (both in the app's voice packages); any installed voice of the language stands in.
VOICES = {"es": "es_MX-claude-high", "en": "en_US-kathleen-medium"}
TTS_SR = 22050

SYSTEM_PROMPT = """You are the interpreter in a face-to-face, one-on-one conversation between an English speaker and a Spanish speaker. They share one laptop: it listens, you translate, and a text-to-speech voice reads your translation aloud to the other person.

HOW THE INPUT ARRIVES
- Each message is one complete turn of speech from automatic speech recognition, tagged [EN] (English speaker) or [ES] (Spanish speaker).
- The recognition can mishear words, and occasionally tags a turn with the wrong language. Use the conversation so far to infer what the speaker meant.

HOW TO TRANSLATE
- Translate [EN] turns into Spanish and [ES] turns into English. If an [EN] turn is actually in Spanish (or an [ES] turn in English), translate it into the other language anyway.
- Natural, spoken, conversational language, as an interpreter would say it out loud. Latin American Spanish. Use "usted" unless the speakers use "tú" with each other; then follow them.
- Keep the speaker's tone, warmth and politeness. Translate questions as questions. Keep names unchanged.
- Leave out filler words, false starts and repetitions; never add anything the speaker did not say.
- You are only the interpreter: never answer, comment on, or react to what is said, even when a question seems addressed to you. Translate it.
- Write numbers, times and amounts the way they are said out loud.
- If a turn has no understandable words at all (noise, a cough), reply exactly: <skip>
- Reply with the translation only: no tags, quotation marks, notes or explanations."""


@dataclass
class TalkRun:
    """An open conversation page, as the Hear Church meter sees it (engine._activity): a private session whose
    two languages are both heard, billed per second of speech like a meeting."""
    started_at: float = field(default_factory=time.time)
    running: bool = True
    on_channels: tuple[str, ...] = tuple(LANGS)
    speakers: tuple[str, ...] = tuple(LANGS)


@dataclass
class TalkCfg:
    end_pause_s: float = 1.2      # silence that ends a turn
    vad_on: float = 0.5           # speech probability that starts/continues speech
    min_voiced_s: float = 0.35    # shorter turns are dropped as noise (coughs, clicks)
    max_turn_s: float = 28.0      # Whisper sees 30 s at a time: end the turn here at the latest
    other_prior: float = 0.65     # chance that the next turn is the other person (conversations alternate)
    short_turn_s: float = 1.0     # below this much speech ("sí", "okay") Whisper's guess is unreliable…
    short_other_prior: float = 0.85  # …so lean harder on "it's the other person's turn"


@dataclass
class Turn:
    n: int
    lang: str = ""                # language heard ("en" | "es")
    forced: bool = False          # chosen with a button rather than detected
    probs: dict = field(default_factory=dict)
    text: str = ""
    translation: str = ""
    audio_s: float = 0.0
    t_end: float = 0.0            # monotonic: end of speech (the pause started)
    stt_ms: float = 0.0
    mt_ms: float = 0.0
    tts_ms: float = 0.0
    wait_ms: float = 0.0          # end of speech → translation starts playing
    note: str = ""


class TalkTranslator:
    """One Claude conversation for the whole talk (it keeps the context), restarted if it dies."""

    def __init__(self, settings: Settings, workdir: Path):
        self.settings = settings
        self.workdir = workdir
        self.session: ClaudeSession | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> ClaudeSession:
        if self.session is None or not self.session.alive:
            tr = self.settings.translate
            kind = SESSION_TYPES.get(tr.backend, ClaudeSession)   # the meetings' backend: own key, credits or CLI
            self.session = kind(SYSTEM_PROMPT, tr.claude_model, tr.claude_effort, self.workdir)
            await self.session.start()
        return self.session

    async def warm(self) -> None:
        try:
            await self._ensure()
        except Exception as e:
            log.warning("translator start failed: %s", e)

    async def translate(self, lang: str, text: str) -> str | None:
        async with self._lock:
            s = await self._ensure()
            reply = await asyncio.wait_for(s.ask(f"[{lang.upper()}] {text}"), timeout=30)
        return clean_output(reply)

    async def reset(self) -> None:
        """A new conversation: forget the previous one (and start the next session right away)."""
        async with self._lock:
            if self.session:
                await self.session.close()
            self.session = None
            await self.warm()

    async def close(self) -> None:
        if self.session:
            await self.session.close()


def wav_bytes(pcm: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return buf.getvalue()


def choose_language(probs: dict[str, float], last: str, other_prior: float) -> str:
    """Whisper's guess between English and Spanish, nudged towards "the other person spoke next"."""
    if last in probs:
        prior = {c: (1 - other_prior if c == last else other_prior) for c in probs}
        probs = {c: p * prior[c] for c, p in probs.items()}
    return max(probs, key=probs.get)


class TalkSession:
    """One open /talk page: its turn-taking state, fed with 16 kHz audio."""

    def __init__(self, stt, stt_exec, translator: TalkTranslator, send: Callable[[dict], Awaitable[None]],
                 cfg: TalkCfg | None = None, on_speech: Callable[[int, float, list[str]], None] | None = None):
        self.stt = stt
        self.on_speech = on_speech    # (turn, voiced seconds, [language played]): the Hear Church meter
        self.stt_exec = stt_exec
        self.translator = translator
        self.send = send
        self.cfg = cfg or TalkCfg()
        self.vad = SileroVad()
        self.state = "listening"
        self.forced = ""              # language chosen with a button for the next turn
        self.last_lang = ""
        self.turns: list[Turn] = []
        self.last_audio: np.ndarray | None = None
        self.last_voiced = 0.0
        self._buf = np.zeros(0, dtype=np.float32)     # frames not yet given to the VAD
        self._preroll: list[np.ndarray] = []
        self._speech: list[np.ndarray] = []
        self._voiced = 0
        self._silent = 0
        self._task: asyncio.Task | None = None
        self.log_path = LOG_DIR / time.strftime("talk-%Y%m%d-%H%M%S.jsonl")

    # ------------------------------------------------------------ audio in
    def feed(self, audio: np.ndarray) -> None:
        """16 kHz float32 audio from the page. Ignored unless someone may speak now."""
        if self.state not in ("listening", "hearing"):
            return
        self._buf = np.concatenate([self._buf, audio])
        while len(self._buf) >= FRAME and self.state in ("listening", "hearing"):
            frame, self._buf = self._buf[:FRAME], self._buf[FRAME:]
            self._frame(frame)

    def _frame(self, frame: np.ndarray) -> None:
        speech = self.vad(frame) >= self.cfg.vad_on
        if self.state == "listening":
            self._preroll = (self._preroll + [frame])[-8:]          # ~250 ms before the first word
            if speech:
                self._speech = list(self._preroll)
                self._voiced, self._silent = 1, 0
                self._set_state("hearing")
            return
        self._speech.append(frame)
        if speech:
            self._voiced += 1
            self._silent = 0
        else:
            self._silent += 1
        frame_s = FRAME / SR
        if self._silent * frame_s >= self.cfg.end_pause_s or len(self._speech) * frame_s >= self.cfg.max_turn_s:
            audio = np.concatenate(self._speech[:len(self._speech) - max(0, self._silent - 6)])
            voiced_s = self._voiced * frame_s
            self._speech, self._preroll = [], []
            if voiced_s < self.cfg.min_voiced_s:
                self._set_state("listening")
                return
            self._set_state("translating")
            self._task = asyncio.create_task(self._run_turn(audio, self.forced, time.monotonic(), voiced_s=voiced_s))

    def _set_state(self, state: str) -> None:
        self.state = state
        if state == "listening":
            self.vad.reset()
            self._buf = np.zeros(0, dtype=np.float32)
        asyncio.get_running_loop().create_task(self.send({"type": "state", "state": state, "forced": self.forced}))

    # ------------------------------------------------------------ messages from the page
    async def control(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "played":                 # translation and chime finished: next turn
            if self.state == "speaking":
                self._set_state("listening")
        elif kind == "force":                # English/Español button: the next turn is in this language
            self.forced = msg.get("lang") if msg.get("lang") in LANGS else ""
            await self.send({"type": "state", "state": self.state, "forced": self.forced})
        elif kind == "redo" and self.last_audio is not None and self.state in ("listening", "speaking"):
            # "Wrong language": hear the last turn again in the other language.
            lang = msg.get("lang") if msg.get("lang") in LANGS else ""
            self._set_state("translating")
            self._task = asyncio.create_task(self._run_turn(self.last_audio, lang, time.monotonic(), redo=True))
        elif kind == "config":
            p = msg.get("end_pause_s")
            if isinstance(p, (int, float)) and 0.5 <= p <= 3.0:
                self.cfg.end_pause_s = float(p)
        elif kind == "reset":
            self.turns.clear()
            self.last_lang, self.last_audio, self.forced = "", None, ""
            await self.translator.reset()
            if self.state != "translating":
                self._set_state("listening")

    # ------------------------------------------------------------ one turn
    async def _run_turn(self, audio: np.ndarray, forced: str, t_end: float, redo: bool = False,
                        voiced_s: float = 99.0) -> None:
        loop = asyncio.get_running_loop()
        turn = Turn(n=len(self.turns) + 1 if not redo else len(self.turns), audio_s=round(len(audio) / SR, 2),
                    t_end=t_end, forced=bool(forced))
        try:
            t0 = time.monotonic()
            if forced:
                turn.lang = forced
            else:
                turn.probs = await loop.run_in_executor(self.stt_exec, self.stt.detect_language, audio, list(LANGS))
                prev = self.turns[-2].lang if redo and len(self.turns) > 1 else self.last_lang
                short = voiced_s < self.cfg.short_turn_s
                turn.lang = choose_language(turn.probs, prev, self.cfg.short_other_prior if short else self.cfg.other_prior)
            res = await loop.run_in_executor(self.stt_exec, self.stt.transcribe, audio, "", turn.lang)
            turn.stt_ms = round((time.monotonic() - t0) * 1000)
            if not res.text:
                turn.note = f"nothing understood ({res.dropped_reason})"
                await self.send({"type": "dropped", "reason": turn.note})
                self._set_state("listening")
                return
            turn.text = res.text
            self.last_audio = audio
            if not redo:
                self.last_voiced = voiced_s  # a redo reuses this audio (and its length for the meter)
                self.forced = ""             # a button choice lasts one turn
            await self.send({"type": "heard", "turn": turn.n, "lang": turn.lang, "text": turn.text,
                             "forced": turn.forced, "redo": redo, "probs": turn.probs})

            t1 = time.monotonic()
            try:
                turn.translation = await self.translator.translate(turn.lang, turn.text) or ""
            except Exception as e:
                log.warning("translate failed: %s", e)
                turn.note = f"translation failed: {e}"
            turn.mt_ms = round((time.monotonic() - t1) * 1000)
            if not turn.translation:
                await self.send({"type": "translated", "turn": turn.n, "text": "", "note": turn.note or "skipped"})
                self._finish(turn, redo)
                self._set_state("listening")
                return

            t2 = time.monotonic()
            out = "en" if turn.lang == "es" else "es"
            pcm = await asyncio.to_thread(self._synth, out, turn.translation)
            turn.tts_ms = round((time.monotonic() - t2) * 1000)
            turn.wait_ms = round((time.monotonic() - t_end) * 1000)
            self._finish(turn, redo)
            if self.on_speech:   # metered like a meeting chunk; a redo is the same turn number, counted once
                try:
                    self.on_speech(turn.n, self.last_voiced, [out])
                except Exception:  # pragma: no cover - metering never stops a conversation
                    log.exception("talk meter failed")
            self.state = "speaking"          # set before sending, so no frame slips in
            await self.send({"type": "translated", "turn": turn.n, "lang": out, "text": turn.translation,
                             "audio": base64.b64encode(wav_bytes(pcm, TTS_SR)).decode(),
                             "timing": {"stt_ms": turn.stt_ms, "mt_ms": turn.mt_ms, "tts_ms": turn.tts_ms,
                                        "wait_ms": turn.wait_ms}})
            await self.send({"type": "state", "state": "speaking", "forced": self.forced})
        except Exception as e:  # pragma: no cover
            log.exception("turn failed")
            await self.send({"type": "error", "message": str(e)})
            self._set_state("listening")

    def _finish(self, turn: Turn, redo: bool) -> None:
        if redo and self.turns:
            self.turns[-1] = turn
        else:
            self.turns.append(turn)
        self.last_lang = turn.lang
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps({**asdict(turn), "redo": redo, "wall_time": time.time()}, ensure_ascii=False) + "\n")
        except OSError:  # pragma: no cover
            pass

    @staticmethod
    def _synth(lang: str, text: str) -> np.ndarray:
        from .tts.engines import PiperTts
        from .tts.normalize import speechify

        tts = PiperTts(TtsCfg(engine="piper", voice=VOICES[lang], rate=185, lang=lang), TTS_SR)  # loaded once, then cached
        return tts.synth(speechify(lang, text))

    async def close(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
