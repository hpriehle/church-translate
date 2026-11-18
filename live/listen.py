"""Test listener for the Live English sender: plays the stream and measures the network.

    python -m live.listen 192.168.1.20               # play through this computer's output
    python -m live.listen 192.168.1.20 --no-play     # measure only (loss, jitter, delay)
    python -m live.listen 192.168.1.20 --buffer-ms 20

Run it on a laptop on the church Wi-Fi, sitting in the chapel, to see what the iPhone app will
face: whether packets get through at all, how much they wobble (which sets the phone's buffer),
and an estimate of the delay from microphone to ear.
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading
import time
from collections import deque

import numpy as np

from . import protocol as P

FRAME_S = P.FRAME_SAMPLES / P.SAMPLE_RATE


class JitterBuffer:
    """Reorders packets by sequence number and releases one frame per pull, `target` frames behind.

    pull() returns (opus bytes or None to conceal, kind) where kind is one of
    "ok", "recovered" (from the next packet's redundant copy), "lost", "wait" (still filling).
    """

    def __init__(self, target_frames: int, max_frames: int = 40):
        self.target = max(1, target_frames)
        self.max = max_frames
        self.frames: dict[int, tuple[bytes, bool]] = {}
        self.next: int | None = None
        self.newest: int | None = None
        self.playing = False
        self.misses = 0
        self.late = 0
        self.skipped = 0
        self._over = 0

    def depth(self) -> int:
        if self.next is None or self.newest is None:
            return 0
        return max(0, P.seq_diff(self.newest, self.next) + 1)

    def push(self, seq: int, primary: bytes, redundant: bytes | None) -> None:
        if self.next is not None and P.seq_diff(seq, self.next) < 0:
            self.late += 1
            return
        if self.newest is None or P.seq_diff(seq, self.newest) > 0:
            self.newest = seq
        if self.next is None:
            self.next = seq
        self.frames.setdefault(seq, (primary, False))
        prev = (seq - 1) & 0xFFFFFFFF
        if redundant and prev not in self.frames and P.seq_diff(prev, self.next) >= 0:
            self.frames[prev] = (redundant, True)
        if self.depth() > self.max:  # far behind (stall): jump to the newest audio
            self.next = (self.newest - self.target + 1) & 0xFFFFFFFF
            self.frames = {s: f for s, f in self.frames.items() if P.seq_diff(s, self.next) >= 0}

    def pull(self) -> tuple[bytes | None, str]:
        if self.next is None:
            return None, "wait"
        if not self.playing:
            if self.depth() < self.target:
                return None, "wait"
            self.playing = True
        # clock drift / burst: if we sit well above target for a second, drop one frame
        if self.depth() > self.target + 4:
            self._over += 1
            if self._over > 200:
                self.frames.pop(self.next, None)
                self.next = (self.next + 1) & 0xFFFFFFFF
                self.skipped += 1
                self._over = 0
        else:
            self._over = 0
        item = self.frames.pop(self.next, None)
        self.next = (self.next + 1) & 0xFFFFFFFF
        if item is None:
            self.misses += 1
            if self.misses >= 20 and not self.frames:  # 100 ms of nothing: refill before playing
                self.playing = False
                self.next = None
                self.newest = None
                self.misses = 0
            return None, "lost"
        self.misses = 0
        data, recovered = item
        return data, "recovered" if recovered else "ok"


class Listener:
    def __init__(self, host: str, port: int, buffer_ms: float):
        self.addr = (socket.gethostbyname(host), port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.settimeout(0.5)
        self.jb = JitterBuffer(round(buffer_ms / P.FRAME_MS))
        self.lock = threading.Lock()
        self.hello: P.Hello | None = None
        self.rtts: deque[float] = deque(maxlen=10)
        self.transit: deque[tuple[float, float]] = deque()  # (arrival, arrival - media time)
        self.base: tuple[float, int] | None = None  # (arrival, seq) of the first packet
        self.counts = {"received": 0, "ok": 0, "recovered": 0, "lost": 0}
        self.last_packet = 0.0
        self._stop = threading.Event()
        from .opus import Decoder
        self.decoder = Decoder()
        self.last_pcm = np.zeros(P.FRAME_SAMPLES, np.int16)

    def _send(self, t: P.PacketType, payload: bytes = b"") -> None:
        try:
            self.sock.sendto(P.pack(P.Packet(t, payload=payload)), self.addr)
        except OSError:
            pass

    def _keepalive(self) -> None:
        while not self._stop.is_set():
            self._send(P.PacketType.SUBSCRIBE)
            self._send(P.PacketType.PING, struct.pack("!d", time.perf_counter()))
            self._stop.wait(P.SUBSCRIBE_EVERY_S)

    def _recv(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(2048)
            except (socket.timeout, ConnectionResetError):
                continue
            except OSError:
                return
            now = time.perf_counter()
            p = P.unpack(data)
            if p is None:
                continue
            if p.type == P.PacketType.AUDIO:
                if self.base is None or now - self.last_packet > 2:  # first packet or stream restart
                    self.base = (now, p.seq)
                    self.transit.clear()
                self.last_packet = now
                self.counts["received"] += 1
                media = self.base[0] + P.seq_diff(p.seq, self.base[1]) * FRAME_S
                self.transit.append((now, now - media))
                while self.transit and self.transit[0][0] < now - 10:
                    self.transit.popleft()
                primary, redundant = P.split_audio(p)
                with self.lock:
                    self.jb.push(p.seq, primary, redundant)
            elif p.type == P.PacketType.HELLO:
                self.hello = P.parse_hello(p.payload)
            elif p.type == P.PacketType.PONG and len(p.payload) >= 8:
                (sent,) = struct.unpack("!d", p.payload[:8])
                if self.hello is not None:  # skip the first ping, sent before the stream warmed up
                    self.rtts.append((now - sent) * 1000)

    def next_pcm(self) -> np.ndarray:
        with self.lock:
            data, kind = self.jb.pull()
        if kind == "wait":
            return np.zeros(P.FRAME_SAMPLES, np.int16)
        self.counts[kind] += 1
        if data is None:  # conceal: fade the last frame out rather than click to silence
            self.last_pcm = (self.last_pcm * np.linspace(0.5, 0, P.FRAME_SAMPLES)).astype(np.int16)
            return self.last_pcm
        pcm = self.decoder.decode(data)[:P.FRAME_SAMPLES]
        if len(pcm) < P.FRAME_SAMPLES:
            pcm = np.pad(pcm, (0, P.FRAME_SAMPLES - len(pcm)))
        self.last_pcm = pcm
        return pcm

    def jitter_ms(self) -> tuple[float, float, float]:
        """Spread of packet arrival around the earliest one, last 10 s: p50, p95, p99 in ms."""
        if len(self.transit) < 20:
            return 0.0, 0.0, 0.0
        d = np.array([t for _, t in self.transit])
        d = (d - d.min()) * 1000
        return tuple(float(np.percentile(d, q)) for q in (50, 95, 99))

    def start(self) -> None:
        for fn in (self._keepalive, self._recv):
            threading.Thread(target=fn, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self._send(P.PacketType.UNSUBSCRIBE)
        self.sock.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m live.listen", description=__doc__.split("\n\n")[0])
    ap.add_argument("host", help="the sender's IP address")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--buffer-ms", type=float, default=30, help="playout buffer (default 30)")
    ap.add_argument("--no-play", action="store_true", help="measure only, no sound")
    ap.add_argument("--device", help="output device index or name")
    a = ap.parse_args(argv)

    L = Listener(a.host, a.port, a.buffer_ms)
    L.start()
    out_latency_ms = 0.0
    stream = None
    pacer_stop = threading.Event()
    if a.no_play:
        def pace():
            nxt = time.perf_counter()
            while not pacer_stop.is_set():
                L.next_pcm()
                nxt += FRAME_S
                time.sleep(max(0, nxt - time.perf_counter()))
        threading.Thread(target=pace, daemon=True).start()
    else:
        import sounddevice as sd
        dev = int(a.device) if a.device and a.device.isdigit() else a.device

        def cb(outdata, frames, t, status):
            outdata[:, 0] = L.next_pcm()[:frames]
        stream = sd.OutputStream(device=dev, samplerate=P.SAMPLE_RATE, channels=1, dtype="int16",
                                 blocksize=P.FRAME_SAMPLES, latency="low", callback=cb)
        stream.start()
        out_latency_ms = float(stream.latency) * 1000

    print(f"Listening to {L.addr[0]}:{L.addr[1]}, buffer {a.buffer_ms:.0f} ms. Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
            c = L.counts
            if not c["received"]:
                sys.stdout.write("\rNo audio yet: is the sender running, and can this device reach it?   ")
                sys.stdout.flush()
                continue
            if time.perf_counter() - L.last_packet > 2:
                sys.stdout.write("\rStream stopped: no packets for 2 s.                                   ")
                sys.stdout.flush()
                continue
            played = c["ok"] + c["recovered"] + c["lost"]
            loss = 100 * c["lost"] / played if played else 0
            p50, p95, p99 = L.jitter_ms()
            rtt = float(np.median(L.rtts)) if L.rtts else 0.0
            cap = L.hello.capture_latency_ms if L.hello else 0.0
            buf = L.jb.depth() * P.FRAME_MS
            est = cap + P.FRAME_MS + 2.5 + rtt / 2 + buf + out_latency_ms
            sys.stdout.write(
                f"\rrtt {rtt:5.1f} ms | jitter p95 {p95:5.1f} p99 {p99:5.1f} ms | lost {loss:4.1f}%"
                f" (recovered {c['recovered']}, late {L.jb.late}) | buffer {buf:4.0f} ms"
                f" | mic-to-ear ~{est:3.0f} ms   ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        print()
        p50, p95, p99 = L.jitter_ms()
        print(f"Suggested phone buffer for this spot: about {max(10, p99 + 5):.0f} ms "
              f"(covers 99% of packet timing wobble).")
    finally:
        pacer_stop.set()
        if stream:
            stream.stop()
            stream.close()
        L.stop()


if __name__ == "__main__":
    main()
