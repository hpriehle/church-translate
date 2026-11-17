"""Speech-to-text. mlx-whisper (large-v3-turbo) on Apple Silicon; faster-whisper only for dev boxes."""
from __future__ import annotations

import logging
import os
import platform
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from .config import SttCfg

log = logging.getLogger("stt")

# Phrases Whisper invents on silence/music. Dropped only when the audio looks like non-speech.
_HALLUCINATIONS = [
    "thanks for watching", "thank you for watching", "please subscribe", "subtitles by",
    "subtitled by", "amara.org", "transcribed by", "transcription by", "captions by",
    "see you next time", "i'll see you in the next video",
]
_WEAK = {"thank you", "thank you.", "you", "bye", "bye.", "so", "okay", "oh", "um", "uh", "hmm", "."}


@dataclass
class SttResult:
    text: str
    ms: float
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0
    dropped_reason: str = ""
    language: str = ""          # what the chunk was recognised as (normally one of the meeting's speaker languages)
    detected: str = ""          # the language detector's most likely language for it
    add: str = ""               # a language heard that isn't a speaker language yet, to be added (AutoLanguage)


# Speaker languages Whisper doesn't know (they have their own recogniser: wolof_asr.py).
WHISPER_UNKNOWN = {"wo"}

# Several speaker languages: a chunk keeps the previous chunk's language unless another one of the meeting's
# languages is clearly more likely (by this much probability), so a short or unclear chunk doesn't flip.
STICKY = 0.15


def pick_language(probs: dict[str, float], choices: list[str], prev: str = "") -> str:
    """The most likely of the meeting's speaker languages (Whisper's detector scores all ~100 of its languages)."""
    scored = {c: float(probs.get(c, 0.0)) for c in choices}
    best = max(choices, key=lambda c: (scored[c], -choices.index(c)))
    if prev in scored and scored[best] - scored[prev] < STICKY:
        return prev
    return best


# Recognition is never forced into a speaker language the speech isn't in: told "English" while people speak
# Russian, Whisper writes its own rough English, which listeners would get as a free pass-through. So each chunk's
# language is detected among all of Whisper's languages; the speaker set is only a prior.
OUTSIDE_MARGIN = 0.2     # a language outside the set is recognised as itself when this much more likely than any in it
ADD_CONFIDENT = 0.5      # ... and added to the meeting's speaker languages once this likely
ADD_AFTER = 2            # ... in this many chunks in a row (one odd chunk never changes the set)


@dataclass
class LangDecision:
    language: str            # what the chunk is recognised as
    detected: str = ""       # the detector's most likely language ("" when there is no detector)
    add: str = ""            # a language confirmed outside the speaker set, to be added to it now


class AutoLanguage:
    """Picks each chunk's recognition language from the detector's probabilities and the meeting's speaker
    languages, and notices a language spoken that isn't among them (docs/v3-languages.md)."""

    def __init__(self, addable: set[str] | None = None):
        self.addable = addable      # languages that can become speaker languages (None: any)
        self.reset()

    def reset(self) -> None:
        """A new meeting."""
        self.added: list[str] = []      # added automatically in this meeting (and not removed since)
        self.dismissed: set[str] = set()   # added, then removed by the operator: not added again this meeting
        self._streak_lang, self._streak = "", 0

    def removed(self, code: str) -> None:
        """The operator took an automatically added language out of the speaker set."""
        if code in self.added:
            self.added.remove(code)
            self.dismissed.add(code)

    def decide(self, probs: dict[str, float], speakers: list[str], prev: str = "") -> LangDecision:
        in_set = pick_language(probs, speakers, prev)
        if not probs:
            return LangDecision(in_set)
        top = max(probs, key=lambda c: probs[c])
        p = float(probs[top])
        best_set = max(float(probs.get(c, 0.0)) for c in speakers)
        margin = 0.0 if top == prev else OUTSIDE_MARGIN   # still speaking the language of the chunk before
        if top in speakers or p - best_set < margin:
            self._streak_lang, self._streak = "", 0
            return LangDecision(in_set, top)
        if p >= ADD_CONFIDENT:
            self._streak = self._streak + 1 if top == self._streak_lang else 1
            self._streak_lang = top
        else:
            self._streak_lang, self._streak = "", 0
        add = ""
        if (self._streak >= ADD_AFTER and top not in self.dismissed
                and (self.addable is None or top in self.addable)):
            add = top
            self.added.append(top)
            self._streak_lang, self._streak = "", 0
        return LangDecision(top, top, add)


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[♪♫]+", "", text).strip()
    return text


def _repetitive(text: str) -> bool:
    words = text.lower().split()
    if len(words) < 12:
        return False
    for n in (1, 2, 3, 4):
        grams = [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
        if grams and max(grams.count(g) for g in set(grams)) * n > 0.6 * len(words):
            return True
    return False


def resolve_engine(engine: str) -> str:
    """"auto": mlx-whisper on Apple Silicon Macs, faster-whisper (CPU) everywhere else."""
    if engine == "auto":
        return "mlx" if sys.platform == "darwin" and platform.machine() == "arm64" else "faster-whisper"
    return engine


# CPU speech recognition (faster-whisper) on a computer too slow for the configured model steps down this ladder,
# one size at a time, while the measured speed stays above STEP_DOWN_RTF (seconds of work per second of speech), or
# while lines wait longer than STEP_DOWN_WAIT_S for their turn (median over the window): at an RTF of 0.5-0.6 bursts of
# speech still queue up, and captions end up 10-20 s behind (docs/intel-latency.md, docs/windows-engine.md).
FW_LADDER = ["small", "base"]
STEP_DOWN_RTF = 0.8
STEP_DOWN_WAIT_S = 4.0
RTF_WINDOW = 8           # judged on the last this many chunks (one slow chunk alone never steps down)
RTF_MIN_CHUNKS = 6       # chunks measured on a model before it can be judged too slow
RTF_MIN_AUDIO_S = 2.0    # shorter chunks say little about speed


_CUDA: bool | None = None   # faster-whisper on an NVIDIA GPU: None = not checked yet


def _cuda_usable() -> bool:
    """An NVIDIA GPU that CTranslate2 sees (Windows/Linux PCs; never a Mac). Optional: HEARCHURCH_STT_DEVICE=cpu
    turns it off. Whether its CUDA libraries really load is only known at the first model (_fw_load)."""
    global _CUDA
    if _CUDA is None:
        _CUDA = False
        if os.environ.get("HEARCHURCH_STT_DEVICE", "").lower() != "cpu" and sys.platform != "darwin":
            try:
                import ctranslate2

                _CUDA = ctranslate2.get_cuda_device_count() > 0
            except Exception:
                pass
    return _CUDA


class Transcriber:
    def __init__(self, cfg: SttCfg):
        self.cfg = cfg
        self.engine = resolve_engine(cfg.engine)
        self._fw_model = None
        self._fw_name = ""
        self.ready = False
        self.load_s = 0.0
        self.rtf = 0.0             # seconds of work per second of speech, last RTF_WINDOW chunks (faster-whisper)
        self.wait = 0.0            # median seconds a line waited for its turn, last RTF_WINDOW chunks (faster-whisper)
        self._recent: deque[tuple[float, float, float]] = deque(maxlen=RTF_WINDOW)   # (speech s, work s, waited s)
        self._next_wait = 0.0      # how long the chunk about to be recognised waited in the queue (note_wait)
        self._step = 0             # sizes stepped down from cfg.faster_whisper_model (kept while the app runs)
        self.stepped_down = ""     # what the operator is told when it happened
        self.events: list[str] = []   # step-downs and background downloads, for the meeting report
        self._fetch_at: dict[str, float] = {}   # smaller models being fetched in the background: when it started

    @property
    def model_name(self) -> str:
        return self._fw_name

    def load(self) -> None:
        t0 = time.monotonic()
        if self.engine == "mlx":
            import mlx_whisper  # noqa: F401  (import errors surface on the operator page)
            # A first call loads the weights into memory (and downloads them once).
            self._transcribe_raw(np.zeros(16000, dtype=np.float32), None)
        elif self.engine == "faster-whisper":
            self._fw_name = self._fw_wanted()
            self._fw_model = self._fw_load(self._fw_name, local_only=False)
            self._transcribe_raw(np.zeros(16000, dtype=np.float32), None)
        else:
            raise ValueError(f"unknown stt engine {self.engine!r}")
        self.load_s = time.monotonic() - t0
        self.ready = True
        log.info("STT ready (%s) in %.1fs", self.engine, self.load_s)

    def _fw_wanted(self) -> str:
        """The faster-whisper model for the speaker languages: "small.en" becomes "small" unless only English is
        spoken (English-only models know only English, and can't tell languages apart), and a smaller size once
        this computer has proved too slow for it (see _measure)."""
        name = self.cfg.faster_whisper_model
        en = name.endswith(".en")
        size = name[:-3] if en else name
        if self._step and size in FW_LADDER:
            size = FW_LADDER[min(FW_LADDER.index(size) + self._step, len(FW_LADDER) - 1)]
        english_only = (self.cfg.languages or [self.cfg.language]) == ["en"]
        return size + ".en" if en and english_only else size

    def note_wait(self, seconds: float) -> None:
        """The pipeline, before the next chunk: how long it waited in the queue for speech recognition."""
        self._next_wait = max(0.0, seconds)

    def _measure(self, audio_s: float, work_s: float) -> None:
        """Track the real-time factor and the queue wait; step down one model size when this CPU can't keep up with
        the speaker: too slow on average, or lines waiting too long for their turn."""
        waited, self._next_wait = self._next_wait, 0.0
        if self.engine != "faster-whisper" or audio_s < RTF_MIN_AUDIO_S:
            return
        self._recent.append((audio_s, work_s, waited))
        self.rtf = sum(w for _, w, _ in self._recent) / sum(a for a, _, _ in self._recent)
        waits = sorted(q for _, _, q in self._recent)
        self.wait = waits[len(waits) // 2]
        slow, behind = self.rtf > STEP_DOWN_RTF, self.wait > STEP_DOWN_WAIT_S
        if len(self._recent) < RTF_MIN_CHUNKS or not (slow or behind):
            return
        before = self._fw_wanted()
        self._step += 1
        after = self._fw_wanted()
        if after == before:   # already the smallest: nothing left to step down to
            self._step -= 1
            return
        if not self._on_disk(after):   # e.g. installed before it was part of the download: fetch it, step later
            self._step -= 1
            self._fetch(after)
            return
        if slow:
            self.stepped_down = (f"Speech recognition was too slow on this computer ({self.rtf:.2f}s of work per "
                                 f"second of speech with {before}): switching to the smaller {after} model")
            why = f"it needed {self.rtf:.2f}s per second of speech"
        else:
            self.stepped_down = (f"Speech recognition fell behind on this computer (lines waited {self.wait:.0f}s for "
                                 f"their turn with {before}): switching to the smaller {after} model")
            why = f"lines waited {self.wait:.1f}s for their turn, at {self.rtf:.2f}s per second of speech"
        log.warning(self.stepped_down)
        self.events.append(f"model {before} → {after} ({why})")
        self._recent.clear()   # judge the new model on its own speed (it loads at the next chunk)

    @staticmethod
    def _fw_load(name: str, local_only: bool):
        """Load a faster-whisper model by name from the model cache (downloading it unless local_only). Resolved to
        its cache folder first: given a bare name, faster-whisper loads a folder of that name in the working
        directory instead when one exists (a folder called "base" broke the step-down in a test)."""
        from faster_whisper import WhisperModel, download_model

        path = download_model(name, local_files_only=local_only)
        if _cuda_usable():
            try:
                model = WhisperModel(path, device="cuda", compute_type="int8_float16")
                # CUDA libraries (cuBLAS, cuDNN) are only loaded on the first transcription: try one now.
                list(model.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
                log.info("speech recognition on the NVIDIA GPU (%s)", name)
                return model
            except Exception as e:
                global _CUDA
                _CUDA = False
                log.warning("NVIDIA GPU found but not usable for speech recognition (%s): using the CPU", e)
        return WhisperModel(path, device="cpu", compute_type="int8")

    @staticmethod
    def _on_disk(name: str) -> bool:
        from faster_whisper import download_model

        try:
            download_model(name, local_files_only=True)
            return True
        except Exception:
            return False

    def _fetch(self, name: str) -> None:
        """Download a smaller model in the background (never in the way of the meeting); retried after 5 minutes."""
        if time.monotonic() - self._fetch_at.get(name, -1e9) < 300:
            return
        self._fetch_at[name] = time.monotonic()
        self.events.append(f"too slow ({self.rtf:.2f}s per second of speech): downloading the smaller {name} model")
        log.warning("speech recognition too slow (RTF %.2f): downloading %s in the background", self.rtf, name)

        def run() -> None:
            from faster_whisper import download_model

            try:
                download_model(name)
                log.info("downloaded the %s speech model", name)
            except Exception as e:
                log.warning("couldn't download the %s speech model: %s", name, e)

        threading.Thread(target=run, name=f"fetch-{name}", daemon=True).start()

    def _fw_sync(self) -> None:
        """Load the model _fw_wanted names when it changed: the speaker languages changed (English-only models know
        only English), or this CPU proved too slow for the model. A smaller model is only ever loaded from disk:
        _measure fetched it first."""
        if self._fw_wanted() == self._fw_name:
            return
        try:
            self._fw_model = self._fw_load(self._fw_wanted(), local_only=bool(self._step))
        except Exception:
            if not self._step:
                raise
            log.exception("couldn't load the smaller model %s: keeping %s", self._fw_wanted(), self._fw_name)
            self._step -= 1     # e.g. not downloaded and offline
        else:
            self._fw_name = self._fw_wanted()

    def language_probs(self, audio: np.ndarray) -> dict[str, float]:
        """The detector's probability for each of Whisper's ~100 languages, from the first 30 s of `audio`.
        {} with an English-only model (dev boxes' "small.en"): it can't tell languages apart."""
        if self.engine == "mlx":
            import mlx.core as mx
            from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
            from mlx_whisper.transcribe import ModelHolder

            model = ModelHolder.get_model(self.cfg.model, mx.float16)   # the one transcribe() uses (fp16)
            mel = log_mel_spectrogram(audio, n_mels=model.dims.n_mels, padding=N_SAMPLES)
            _, probs = model.detect_language(pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16))
            probs = probs[0] if isinstance(probs, list) else probs
        else:
            self._fw_sync()
            if self._fw_name.endswith(".en"):
                return {}
            _lang, _p, all_probs = self._fw_model.detect_language(audio)
            probs = dict(all_probs)
        return {str(c): float(p) for c, p in probs.items()}

    def detect(self, audio: np.ndarray, choices: list[str], prev: str = "") -> str:
        """Which of `choices` `audio` is in (one choice: that one, nothing is run)."""
        if len(choices) <= 1:
            return choices[0] if choices else self.cfg.language
        return pick_language(self.language_probs(audio), choices, prev)

    def _transcribe_raw(self, audio: np.ndarray, prompt: str | None,
                        language: str | None = None) -> tuple[str, float, float, float]:
        """Returns (text, max no_speech_prob, mean avg_logprob, max compression ratio)."""
        language = language or self.cfg.language
        if language in WHISPER_UNKNOWN:   # e.g. the warm-up of a Wolof-only meeting: Whisper detects instead
            language = None
        if self.engine == "mlx":
            import mlx_whisper

            r = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self.cfg.model,
                language=language,
                task="transcribe",
                initial_prompt=prompt or None,
                condition_on_previous_text=False,
                temperature=(0.0, 0.2, 0.4),
                compression_ratio_threshold=2.4,
                logprob_threshold=-1.0,
                no_speech_threshold=0.6,
                verbose=None,
            )
            segs = r.get("segments", [])
            text = r.get("text", "")
        else:
            self._fw_sync()
            seg_iter, _info = self._fw_model.transcribe(
                audio, language=language, initial_prompt=prompt or None,
                condition_on_previous_text=False, beam_size=1, temperature=[0.0, 0.2, 0.4],
                vad_filter=False,
            )
            segs = [{"text": s.text, "no_speech_prob": s.no_speech_prob, "avg_logprob": s.avg_logprob,
                     "compression_ratio": s.compression_ratio} for s in seg_iter]
            text = "".join(s["text"] for s in segs)
        if not segs:
            return _clean(text), 1.0, -2.0, 0.0
        nsp = max(float(s.get("no_speech_prob", 0.0)) for s in segs)
        alp = float(np.mean([float(s.get("avg_logprob", 0.0)) for s in segs]))
        cr = max(float(s.get("compression_ratio", 0.0)) for s in segs)
        return _clean(text), nsp, alp, cr

    def detect_language(self, audio: np.ndarray, choices: list[str]) -> dict[str, float]:
        """Probability of each language in `choices` (renormalized among them), from the first 30 s (/talk)."""
        probs = self.language_probs(audio)
        picked = {c: float(probs.get(c, 0.0)) for c in choices}
        total = sum(picked.values()) or 1.0
        return {c: p / total for c, p in picked.items()}

    def transcribe(self, audio: np.ndarray, prompt: str = "", language: str | None = None,
                   raw=None) -> SttResult:
        """`raw`: another model's recogniser, audio -> (text, no-speech prob, mean logprob, compression ratio), whose
        output gets the same checks (the Wolof model, see wolof_asr.py)."""
        t0 = time.monotonic()
        reloaded = raw is not None or (self.engine == "faster-whisper" and self._fw_wanted() != self._fw_name)
        text, nsp, alp, cr = raw(audio) if raw is not None else self._transcribe_raw(audio, prompt, language)
        text = _clean(text)
        res = SttResult(text=text, ms=(time.monotonic() - t0) * 1000, no_speech_prob=nsp, avg_logprob=alp,
                        language=language or self.cfg.language)
        if not reloaded:   # a chunk that also loaded a model (or another model's) says nothing about Whisper's speed
            self._measure(len(audio) / 16000, res.ms / 1000)
        low = text.lower().strip(" .!?,")
        if not text:
            res.dropped_reason = "empty"
        elif any(h in low for h in _HALLUCINATIONS):
            res.dropped_reason = "hallucination"
        elif (low in _WEAK or text.lower() in _WEAK) and nsp > 0.3:
            res.dropped_reason = "non-speech"
        elif nsp > 0.6 and alp < -1.0:
            res.dropped_reason = "non-speech"
        elif cr > 2.6 or _repetitive(text):
            res.dropped_reason = "repetition"
        elif prompt and len(text) > 20 and text.strip() in prompt:
            res.dropped_reason = "prompt echo"
        if res.dropped_reason:
            res.text = ""
        return res


def build_prompt(hints: list[str], previous: str, lang: str = "en") -> str:
    """Whisper 'initial prompt': spelling hints + the tail of the previous sentence (≤ ~200 chars).
    Not English words before a French talk: Whisper follows the prompt's language."""
    hint = ", ".join(hints[:40])
    prev = previous[-200:]
    if prev and " " in prev:
        prev = prev[prev.index(" ") + 1:] if len(previous) > 200 else prev
    parts = []
    if hint:
        parts.append(f"Church talk. Names: {hint}." if lang == "en" else f"{hint}.")
    if prev:
        parts.append(prev)
    return " ".join(parts)
