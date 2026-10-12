"""Real-time mixer: plays queued TTS clips back to back, silence in between, in 100 ms frames.

It is the single clock of the audio stream. Its queue length *is* the backlog: how many seconds
of French audio are waiting to be heard.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

log = logging.getLogger("mixer")

TICK_S = 0.1


@dataclass
class Clip:
    seq: int
    pcm: np.ndarray                 # int16 mono
    meta: dict = field(default_factory=dict)

    def duration(self, sr: int) -> float:
        return len(self.pcm) / sr


def normalize_peak(pcm: np.ndarray, peak: float = 0.7) -> np.ndarray:
    """Same loudness for every voice/engine (peak ≈ -3 dBFS), leaving headroom for the volume control."""
    m = int(np.abs(pcm.astype(np.int32)).max()) if len(pcm) else 0
    if m < 100:
        return pcm
    return np.clip(pcm.astype(np.float32) * (peak * 32767 / m), -32768, 32767).astype(np.int16)


def trim_silence(pcm: np.ndarray, sr: int, threshold: int = 300, pad_s: float = 0.03) -> np.ndarray:
    loud = np.flatnonzero(np.abs(pcm) > threshold)
    if len(loud) == 0:
        return pcm[:0]
    pad = int(pad_s * sr)
    return pcm[max(0, loud[0] - pad): min(len(pcm), loud[-1] + pad)]


class Mixer:
    def __init__(self, sr: int, gap_s: float = 0.18,
                 on_frame: Callable[[np.ndarray], None] | None = None,
                 on_air: Callable[[Clip, float], None] | None = None):
        self.sr = sr
        self.frame = int(sr * TICK_S)
        self.gap = np.zeros(int(sr * gap_s), dtype=np.int16)
        self.on_frame = on_frame
        self.on_air = on_air
        self.volume = 1.0
        self.queue: deque[Clip] = deque()
        # Hard ceiling: never start a clip whose speech ended more than max_age_s ago
        # (clip.meta["t_ref"] = monotonic time the speaker finished it). 0 = off.
        self.max_age_s = 0.0
        self.on_skip: Callable[[Clip, float], None] | None = None
        self._current: Clip | None = None
        self._pos = 0
        self._task: asyncio.Task | None = None
        self.underruns = 0

    # ------------------------------------------------------------ queue
    def enqueue(self, clip: Clip, drop_above_s: float | None = None, drop_to_s: float | None = None) -> list[Clip]:
        clip.pcm = np.concatenate([normalize_peak(trim_silence(clip.pcm, self.sr)), self.gap])
        self.queue.append(clip)
        dropped: list[Clip] = []
        if drop_above_s is not None and self.backlog_s > drop_above_s:
            target = drop_to_s if drop_to_s is not None else drop_above_s * 0.6
            # Drop the oldest waiting clips (never the one playing, never the new one).
            while len(self.queue) > 1 and self.backlog_s > target:
                dropped.append(self.queue.popleft())
        return dropped

    @property
    def backlog_s(self) -> float:
        n = sum(len(c.pcm) for c in self.queue)
        if self._current is not None:
            n += len(self._current.pcm) - self._pos
        return n / self.sr

    @property
    def playing(self) -> int | None:
        return self._current.seq if self._current else None

    def clear(self) -> None:
        self.queue.clear()
        self._current = None
        self._pos = 0

    # ------------------------------------------------------------ clock
    def _next_frame(self) -> np.ndarray:
        out = np.zeros(self.frame, dtype=np.int16)
        filled = 0
        while filled < self.frame:
            if self._current is None:
                if not self.queue:
                    break
                clip = self.queue.popleft()
                t_ref = clip.meta.get("t_ref")
                if self.max_age_s and t_ref and time.monotonic() - t_ref > self.max_age_s:
                    if self.on_skip:
                        try:
                            self.on_skip(clip, time.monotonic() - t_ref)
                        except Exception:  # pragma: no cover
                            log.exception("on_skip callback failed")
                    continue
                self._current = clip
                self._pos = 0
                if self.on_air:
                    try:
                        self.on_air(self._current, time.monotonic() + filled / self.sr)
                    except Exception:  # pragma: no cover
                        log.exception("on_air callback failed")
            take = min(self.frame - filled, len(self._current.pcm) - self._pos)
            out[filled:filled + take] = self._current.pcm[self._pos:self._pos + take]
            filled += take
            self._pos += take
            if self._pos >= len(self._current.pcm):
                self._current = None
        if self.volume != 1.0:
            out = np.clip(out.astype(np.float32) * self.volume, -32768, 32767).astype(np.int16)
        return out

    async def run(self) -> None:
        next_t = time.monotonic()
        while True:
            now = time.monotonic()
            behind = now - next_t
            if behind > 1.0:  # event loop stalled badly; resync instead of bursting
                self.underruns += 1
                next_t = now
            while next_t <= time.monotonic():
                frame = self._next_frame()
                if self.on_frame:
                    self.on_frame(frame)
                next_t += TICK_S
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())
