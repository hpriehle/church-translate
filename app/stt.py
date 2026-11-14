"""Speech-to-text. mlx-whisper (large-v3-turbo) on Apple Silicon; faster-whisper only for dev boxes."""
from __future__ import annotations

import logging
import platform
import re
import sys
import time
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


class Transcriber:
    def __init__(self, cfg: SttCfg):
        self.cfg = cfg
        self.engine = resolve_engine(cfg.engine)
        self._fw_model = None
        self._fw_name = ""
        self.ready = False
        self.load_s = 0.0

    def load(self) -> None:
        t0 = time.monotonic()
        if self.engine == "mlx":
            import mlx_whisper  # noqa: F401  (import errors surface on the operator page)
            # A first call loads the weights into memory (and downloads them once).
            self._transcribe_raw(np.zeros(16000, dtype=np.float32), None)
        elif self.engine == "faster-whisper":
            from faster_whisper import WhisperModel

            self._fw_name = self._fw_wanted()
            self._fw_model = WhisperModel(self._fw_name, device="cpu", compute_type="int8")
            self._transcribe_raw(np.zeros(16000, dtype=np.float32), None)
        else:
            raise ValueError(f"unknown stt engine {self.engine!r}")
        self.load_s = time.monotonic() - t0
        self.ready = True
        log.info("STT ready (%s) in %.1fs", self.engine, self.load_s)

    def _fw_wanted(self) -> str:
        """The faster-whisper model for the speaker languages: "small.en" becomes "small" unless only English is
        spoken (English-only models know only English, and can't tell languages apart)."""
        name = self.cfg.faster_whisper_model
        english_only = (self.cfg.languages or [self.cfg.language]) == ["en"]
        return name[:-3] if not english_only and name.endswith(".en") else name

    def _fw_sync(self) -> None:
        if self._fw_wanted() != self._fw_name:   # the speaker languages changed: English-only models know only English
            from faster_whisper import WhisperModel

            self._fw_name = self._fw_wanted()
            self._fw_model = WhisperModel(self._fw_name, device="cpu", compute_type="int8")

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

    def transcribe(self, audio: np.ndarray, prompt: str = "", language: str | None = None) -> SttResult:
        t0 = time.monotonic()
        text, nsp, alp, cr = self._transcribe_raw(audio, prompt, language)
        res = SttResult(text=text, ms=(time.monotonic() - t0) * 1000, no_speech_prob=nsp, avg_logprob=alp,
                        language=language or self.cfg.language)
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
