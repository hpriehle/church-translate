"""Speech-to-text. mlx-whisper (large-v3-turbo) on Apple Silicon; faster-whisper only for dev boxes."""
from __future__ import annotations

import logging
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
    language: str = ""          # what the chunk was recognised as (one of the meeting's speaker languages)


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
# one size at a time, while the measured speed stays above STEP_DOWN_RTF (seconds of work per second of speech).
FW_LADDER = ["small", "base"]
STEP_DOWN_RTF = 0.8
RTF_WINDOW = 8           # judged on the last this many chunks (one slow chunk alone never steps down)
RTF_MIN_CHUNKS = 6       # chunks measured on a model before it can be judged too slow
RTF_MIN_AUDIO_S = 2.0    # shorter chunks say little about speed


class Transcriber:
    def __init__(self, cfg: SttCfg):
        self.cfg = cfg
        self.engine = resolve_engine(cfg.engine)
        self._fw_model = None
        self._fw_name = ""
        self.ready = False
        self.load_s = 0.0
        self.rtf = 0.0             # seconds of work per second of speech, last RTF_WINDOW chunks (faster-whisper)
        self._recent: deque[tuple[float, float]] = deque(maxlen=RTF_WINDOW)   # (speech s, work s)
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

    def _measure(self, audio_s: float, work_s: float) -> None:
        """Track the real-time factor; step down one model size when this CPU can't keep up with the speaker."""
        if self.engine != "faster-whisper" or audio_s < RTF_MIN_AUDIO_S:
            return
        self._recent.append((audio_s, work_s))
        self.rtf = sum(w for _, w in self._recent) / sum(a for a, _ in self._recent)
        if len(self._recent) < RTF_MIN_CHUNKS or self.rtf <= STEP_DOWN_RTF:
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
        self.stepped_down = (f"Speech recognition was too slow on this computer ({self.rtf:.2f}s of work per second "
                             f"of speech with {before}): switching to the smaller {after} model")
        log.warning(self.stepped_down)
        self.events.append(f"model {before} → {after} (it needed {self.rtf:.2f}s per second of speech)")
        self._recent.clear()   # judge the new model on its own speed (it loads at the next chunk)

    @staticmethod
    def _fw_load(name: str, local_only: bool):
        """Load a faster-whisper model by name from the model cache (downloading it unless local_only). Resolved to
        its cache folder first: given a bare name, faster-whisper loads a folder of that name in the working
        directory instead when one exists (a folder called "base" broke the step-down in a test)."""
        from faster_whisper import WhisperModel, download_model

        return WhisperModel(download_model(name, local_files_only=local_only), device="cpu", compute_type="int8")

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

    def detect(self, audio: np.ndarray, choices: list[str], prev: str = "") -> str:
        """Which of the meeting's speaker languages `audio` is in (one choice: that one, nothing is run)."""
        if len(choices) <= 1:
            return choices[0] if choices else self.cfg.language
        if self.engine == "mlx":
            import mlx.core as mx
            from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
            from mlx_whisper.transcribe import ModelHolder

            model = ModelHolder.get_model(self.cfg.model, mx.float16)   # the one transcribe() uses (fp16)
            mel = log_mel_spectrogram(audio, n_mels=model.dims.n_mels, padding=N_SAMPLES)
            _, probs = model.detect_language(pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16))
        else:
            self._fw_sync()
            _lang, _p, all_probs = self._fw_model.detect_language(audio)
            probs = dict(all_probs)
        return pick_language(probs, choices, prev)

    def _transcribe_raw(self, audio: np.ndarray, prompt: str | None,
                        language: str | None = None) -> tuple[str, float, float, float]:
        """Returns (text, max no_speech_prob, mean avg_logprob, max compression ratio)."""
        language = language or self.cfg.language
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
        if self.engine == "mlx":
            import mlx.core as mx
            from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
            from mlx_whisper.transcribe import ModelHolder

            model = ModelHolder.get_model(self.cfg.model, mx.float16)
            mel = log_mel_spectrogram(audio, n_mels=model.dims.n_mels, padding=N_SAMPLES)
            _, probs = model.detect_language(pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16))
            probs = probs[0] if isinstance(probs, list) else probs
        else:
            self._fw_sync()
            _lang, _p, all_probs = self._fw_model.detect_language(audio)
            probs = dict(all_probs)
        picked = {c: float(probs.get(c, 0.0)) for c in choices}
        total = sum(picked.values()) or 1.0
        return {c: p / total for c, p in picked.items()}

    def transcribe(self, audio: np.ndarray, prompt: str = "", language: str | None = None) -> SttResult:
        t0 = time.monotonic()
        reloaded = self.engine == "faster-whisper" and self._fw_wanted() != self._fw_name
        text, nsp, alp, cr = self._transcribe_raw(audio, prompt, language)
        res = SttResult(text=text, ms=(time.monotonic() - t0) * 1000, no_speech_prob=nsp, avg_logprob=alp,
                        language=language or self.cfg.language)
        if not reloaded:   # a chunk that also loaded a model says nothing about its speed
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
