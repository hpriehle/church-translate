"""Continuous MP3 stream (internet-radio style) fanned out to every connected phone.

One encoder for everyone: the mixer's PCM is encoded once and the same bytes go to all
listeners. A slow/stalled listener is disconnected (its page reconnects) instead of
holding memory or delaying others.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path

import lameenc
import numpy as np

log = logging.getLogger("stream")

_BITRATES_V1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]


def find_frame_sync(buf: bytes) -> int:
    """Offset of the first plausible MPEG-1/2 Layer III frame header, or 0."""
    for i in range(len(buf) - 4):
        if buf[i] == 0xFF and (buf[i + 1] & 0xE6) == 0xE2:  # sync + layer III
            b2 = buf[i + 2]
            if (b2 >> 4) not in (0, 15) and ((b2 >> 2) & 3) != 3:
                return i
    return 0


def encode_clip(pcm: np.ndarray, sr: int, kbps: int) -> bytes:
    """Standalone MP3 for one clip (used by the clip-by-clip fallback mode)."""
    enc = lameenc.Encoder()
    enc.set_bit_rate(kbps)
    enc.set_in_sample_rate(sr)
    enc.set_channels(1)
    enc.set_quality(5)
    return bytes(enc.encode(pcm.astype(np.int16).tobytes()) + enc.flush())


class Subscriber:
    def __init__(self, maxsize: int):
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=maxsize)
        self.kicked = False


class Mp3Broadcaster:
    def __init__(self, sr: int, kbps: int, burst_s: float):
        self.sr = sr
        self.kbps = kbps
        self.encoder = lameenc.Encoder()
        self.encoder.set_bit_rate(kbps)
        self.encoder.set_in_sample_rate(sr)
        self.encoder.set_channels(1)
        self.encoder.set_quality(5)
        self.burst: deque[bytes] = deque(maxlen=max(1, int(burst_s / 0.1)))
        self.subs: set[Subscriber] = set()
        self.kicked_total = 0
        self.bytes_out = 0
        self._recorders: list = []

    def feed(self, pcm: np.ndarray) -> None:
        data = bytes(self.encoder.encode(pcm.tobytes()))
        if not data:
            return
        self.burst.append(data)
        for rec in self._recorders:
            rec.write(data)
        for sub in list(self.subs):
            try:
                sub.queue.put_nowait(data)
                self.bytes_out += len(data)
            except asyncio.QueueFull:
                self._kick(sub)

    def _kick(self, sub: Subscriber) -> None:
        sub.kicked = True
        self.subs.discard(sub)
        self.kicked_total += 1
        try:
            while True:
                sub.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        sub.queue.put_nowait(None)

    def subscribe(self) -> tuple[Subscriber, bytes]:
        sub = Subscriber(maxsize=150)  # 15 s of audio before we give up on a phone
        self.subs.add(sub)
        burst = b"".join(self.burst)
        return sub, burst[find_frame_sync(burst):]

    def unsubscribe(self, sub: Subscriber) -> None:
        self.subs.discard(sub)

    @property
    def listeners(self) -> int:
        return len(self.subs)

    # --------------------------------------------------------- recording
    def start_recording(self, path: Path):
        f = open(path, "wb")
        self._recorders.append(f)
        return f

    def stop_recording(self, f) -> None:
        if f in self._recorders:
            self._recorders.remove(f)
        f.close()
