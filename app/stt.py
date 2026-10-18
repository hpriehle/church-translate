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

            self._fw_model = WhisperModel(self.cfg.faster_whisper_model, device="cpu", compute_type="int8")
            self._transcribe_raw(np.zeros(16000, dtype=np.float32), None)
        else:
            raise ValueError(f"unknown stt engine {self.engine!r}")
        self.load_s = time.monotonic() - t0
        self.ready = True
        log.info("STT ready (%s) in %.1fs", self.engine, self.load_s)

    def _transcribe_raw(self, audio: np.ndarray, prompt: str | None) -> tuple[str, float, float, float]:
        """Returns (text, max no_speech_prob, mean avg_logprob, max compression ratio)."""
        if self.engine == "mlx":
            import mlx_whisper

            r = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self.cfg.model,
                language=self.cfg.language,
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
            seg_iter, _info = self._fw_model.transcribe(
                audio, language=self.cfg.language, initial_prompt=prompt or None,
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

    def transcribe(self, audio: np.ndarray, prompt: str = "") -> SttResult:
        t0 = time.monotonic()
        text, nsp, alp, cr = self._transcribe_raw(audio, prompt)
        res = SttResult(text=text, ms=(time.monotonic() - t0) * 1000, no_speech_prob=nsp, avg_logprob=alp)
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


def build_prompt(hints: list[str], previous: str) -> str:
    """Whisper 'initial prompt': spelling hints + the tail of the previous sentence (≤ ~200 chars)."""
    hint = ", ".join(hints[:40])
    prev = previous[-200:]
    if prev and " " in prev:
        prev = prev[prev.index(" ") + 1:] if len(previous) > 200 else prev
    parts = []
    if hint:
        parts.append(f"Church talk. Names: {hint}.")
    if prev:
        parts.append(prev)
    return " ".join(parts)
