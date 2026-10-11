"""Silero VAD (ONNX, no torch) + a chunker that cuts live audio on natural pauses.

Rules (all configurable in [vad]):
  * never cut before min_chunk_s unless there is a long pause (flush_pause_ms)
  * between min_chunk_s and target_max_s cut on a pause of pause_ms
  * after target_max_s cut on any short pause (short_pause_ms)
  * at hard_max_s cut at the quietest point of the last ~2.5 s and carry the rest over
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import MODELS_DIR, VadCfg

SR = 16000
FRAME = 512                      # Silero v5 window at 16 kHz
FRAME_S = FRAME / SR             # 32 ms
CONTEXT = 64                     # Silero v5 prepends 64 samples of context


class SileroVad:
    def __init__(self, path: Path = MODELS_DIR / "silero_vad.onnx"):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(str(path), sess_options=opts,
                                            providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame.reshape(1, FRAME).astype(np.float32)], axis=1)
        out, self._state = self.session.run(
            None, {"input": x, "state": self._state, "sr": np.array(SR, dtype=np.int64)})
        self._context = x[:, -CONTEXT:]
        return float(out[0][0])


@dataclass
class Chunk:
    seq: int
    audio: np.ndarray            # float32 mono 16 kHz
    start_s: float               # position in the input stream (seconds)
    end_s: float
    t_speech_start: float        # monotonic clock when the first voiced sample was captured
    t_speech_end: float          # monotonic clock when the last voiced sample was captured
    t_cut: float                 # monotonic clock when the chunk was emitted
    voiced_s: float
    reason: str

    @property
    def duration(self) -> float:
        return len(self.audio) / SR


@dataclass
class _Frame:
    audio: np.ndarray
    prob: float
    t: float                     # monotonic capture time of the frame's last sample
    pos: float                   # stream position (s) of the frame's first sample
    speech: bool = field(default=False)


class Chunker:
    """Feed 512-sample frames with capture timestamps; collect finished Chunks from feed()."""

    PREROLL_FRAMES = 6           # ~190 ms kept before speech starts
    TAIL_KEEP_FRAMES = 4         # ~130 ms of trailing silence kept on a chunk

    def __init__(self, cfg: VadCfg, vad: SileroVad | None = None):
        self.cfg = cfg
        self.vad = vad or SileroVad()
        self.seq = 0
        self._frames: list[_Frame] = []
        self._preroll: deque[_Frame] = deque(maxlen=self.PREROLL_FRAMES)
        self._in_chunk = False
        self._speaking = False
        self._silence_run = 0
        self._pos = 0.0

    # ------------------------------------------------------------------ api
    def feed(self, frame: np.ndarray, t_capture: float) -> list[Chunk]:
        prob = self.vad(frame)
        f = _Frame(frame, prob, t_capture, self._pos)
        self._pos += FRAME_S

        thr = self.cfg.threshold
        if self._speaking:
            self._speaking = prob >= thr - 0.15
        else:
            self._speaking = prob >= thr
        f.speech = self._speaking

        if not self._in_chunk:
            if f.speech:
                self._in_chunk = True
                self._frames = list(self._preroll) + [f]
                self._preroll.clear()
                self._silence_run = 0
            else:
                self._preroll.append(f)
            return []

        self._frames.append(f)
        self._silence_run = 0 if f.speech else self._silence_run + 1
        return self._maybe_cut()

    def flush(self) -> list[Chunk]:
        """End of input: emit whatever is left."""
        out: list[Chunk] = []
        if self._in_chunk and self._frames:
            c = self._emit(self._frames, "end")
            if c:
                out.append(c)
        self._frames = []
        self._in_chunk = False
        self._silence_run = 0
        return out

    # ------------------------------------------------------------ internals
    def _maybe_cut(self) -> list[Chunk]:
        cfg = self.cfg
        dur = len(self._frames) * FRAME_S
        sil_ms = self._silence_run * FRAME_S * 1000

        if dur >= cfg.hard_max_s:
            return self._cut_at_quietest()
        if dur >= cfg.target_max_s and sil_ms >= cfg.short_pause_ms:
            return self._cut_on_pause("short_pause")
        if dur >= cfg.min_chunk_s and sil_ms >= cfg.pause_ms:
            return self._cut_on_pause("pause")
        if sil_ms >= cfg.flush_pause_ms:
            return self._cut_on_pause("flush")
        return []

    def _cut_on_pause(self, reason: str) -> list[Chunk]:
        keep = len(self._frames) - self._silence_run + min(self._silence_run, self.TAIL_KEEP_FRAMES)
        chunk_frames = self._frames[:keep]
        rest = self._frames[keep:]
        self._frames = []
        self._in_chunk = False
        self._silence_run = 0
        self._preroll.extend(rest[-self.PREROLL_FRAMES:])
        c = self._emit(chunk_frames, reason)
        return [c] if c else []

    def _cut_at_quietest(self) -> list[Chunk]:
        frames = self._frames
        n = len(frames)
        min_keep = int(self.cfg.min_chunk_s / FRAME_S)
        search_from = max(min_keep, n - int(2.5 / FRAME_S))
        probs = np.array([fr.prob for fr in frames])
        smooth = np.convolve(probs, np.ones(3) / 3, mode="same")
        idx = search_from + int(np.argmin(smooth[search_from:n - 1])) + 1 if search_from < n - 1 else n
        chunk_frames, rest = frames[:idx], frames[idx:]
        self._frames = rest
        self._in_chunk = bool(rest)
        self._silence_run = 0
        for fr in reversed(rest):
            if fr.speech:
                break
            self._silence_run += 1
        c = self._emit(chunk_frames, "hard_cap")
        return [c] if c else []

    def _emit(self, frames: list[_Frame], reason: str) -> Chunk | None:
        if not frames:
            return None
        voiced = [fr for fr in frames if fr.speech]
        voiced_s = len(voiced) * FRAME_S
        if voiced_s < self.cfg.min_voiced_s:
            return None
        audio = np.concatenate([fr.audio for fr in frames]).astype(np.float32)
        self.seq += 1
        return Chunk(
            seq=self.seq,
            audio=audio,
            start_s=frames[0].pos,
            end_s=frames[-1].pos + FRAME_S,
            t_speech_start=voiced[0].t - FRAME_S,
            t_speech_end=voiced[-1].t,
            t_cut=time.monotonic(),
            voiced_s=voiced_s,
            reason=reason,
        )
