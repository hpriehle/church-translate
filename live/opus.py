"""Opus encoder/decoder (libopus via PyAV, whose wheels bundle it on Windows, Mac and Linux)."""
from __future__ import annotations

from fractions import Fraction

import av
import numpy as np

from .protocol import FRAME_SAMPLES, SAMPLE_RATE


class Encoder:
    """5 ms frames in libopus's restricted low-delay mode (CELT only, 2.5 ms lookahead)."""

    def __init__(self, bitrate: int = 64000):
        c = av.CodecContext.create("libopus", "w")
        c.sample_rate = SAMPLE_RATE
        c.layout = "mono"
        c.format = "s16"
        c.bit_rate = bitrate
        c.time_base = Fraction(1, SAMPLE_RATE)
        c.options = {"application": "lowdelay", "frame_duration": "5", "vbr": "off"}
        c.open()
        if c.frame_size != FRAME_SAMPLES:
            raise RuntimeError(f"Opus frame size {c.frame_size}, expected {FRAME_SAMPLES}")
        self._c = c
        self._pts = 0

    def encode(self, pcm: np.ndarray) -> list[bytes]:
        """One 240-sample int16 mono frame in; normally exactly one Opus packet out."""
        f = av.AudioFrame.from_ndarray(np.ascontiguousarray(pcm, dtype=np.int16).reshape(1, -1),
                                       format="s16", layout="mono")
        f.sample_rate = SAMPLE_RATE
        f.pts = self._pts
        self._pts += FRAME_SAMPLES
        return [bytes(p) for p in self._c.encode(f)]


class Decoder:
    def __init__(self):
        c = av.CodecContext.create("libopus", "r")
        c.sample_rate = SAMPLE_RATE
        c.layout = "mono"
        c.open()
        self._c = c

    def decode(self, data: bytes) -> np.ndarray:
        """Opus packet -> int16 mono samples (240 for our frames)."""
        out = [fr.to_ndarray().reshape(-1) for fr in self._c.decode(av.Packet(data))]
        if not out:
            return np.zeros(FRAME_SAMPLES, np.int16)
        pcm = np.concatenate(out)
        if pcm.dtype != np.int16:  # float planar from some builds
            pcm = (np.clip(pcm, -1, 1) * 32767).astype(np.int16)
        return pcm
