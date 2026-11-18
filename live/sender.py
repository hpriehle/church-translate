"""Live English sender: capture the room audio on the office computer and stream it to phones.

    python -m live.sender --list-devices
    python -m live.sender --device "Line In"          # the receiver's aux out
    python -m live.sender --tone                       # test tone, no audio hardware needed
    python -m live.sender --file sermon.mp3            # play a recording as if it were live

Audio: 48 kHz mono, Opus 5 ms frames in low-delay mode (64 kbps), each packet also carrying the
previous frame. Sent by UDP to every listener that has subscribed in the last 5 seconds.
"""
from __future__ import annotations

import argparse
import logging
import math
import queue
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import protocol as P
from .opus import Encoder

log = logging.getLogger("live.sender")

MAX_LISTENERS = 250


# ---------------------------------------------------------------- audio sources
class Source:
    """Produces 240-sample int16 frames into `self.frames` in real time."""

    latency_ms = 0.0

    def __init__(self):
        self.frames: queue.Queue[tuple[np.ndarray, float]] = queue.Queue(maxsize=200)
        self.dropped = 0  # frames the encoder fell behind on
        self.overflows = 0  # driver-reported input overflows

    def _put(self, pcm: np.ndarray) -> None:
        try:
            self.frames.put_nowait((pcm, time.perf_counter()))
        except queue.Full:
            self.dropped += 1

    def start(self) -> None: ...
    def stop(self) -> None: ...


class PacedSource(Source):
    """Base for synthetic sources: emits a frame every 5 ms from a thread."""

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def next_frame(self) -> np.ndarray | None:
        raise NotImplementedError

    def start(self) -> None:
        self._t = threading.Thread(target=self._run, name="paced-source", daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        if self._t:
            self._t.join(1)

    def _run(self) -> None:
        period = P.FRAME_SAMPLES / P.SAMPLE_RATE
        nxt = time.perf_counter()
        while not self._stop.is_set():
            pcm = self.next_frame()
            if pcm is None:
                return
            self._put(pcm)
            nxt += period
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.1:  # stalled (laptop asleep, debugger): don't burst to catch up
                nxt = time.perf_counter()


class ToneSource(PacedSource):
    """A 440 Hz tone with a short beep pattern, so dropouts are easy to hear."""

    def __init__(self, freq: float = 440.0, level_db: float = -18.0):
        super().__init__()
        self._n = 0
        self._amp = 32767 * 10 ** (level_db / 20)
        self._freq = freq

    def next_frame(self) -> np.ndarray:
        idx = np.arange(self._n, self._n + P.FRAME_SAMPLES)
        self._n += P.FRAME_SAMPLES
        on = (idx % P.SAMPLE_RATE) < P.SAMPLE_RATE * 0.6  # 0.6 s on, 0.4 s off
        x = np.sin(2 * math.pi * self._freq * idx / P.SAMPLE_RATE) * self._amp * on
        return x.astype(np.int16)


class FileSource(PacedSource):
    """Any audio/video file decoded by PyAV, played in real time, looping."""

    def __init__(self, path: str, loop: bool = True):
        super().__init__()
        self._path, self._loop = path, loop
        self._buf = np.zeros(0, np.int16)
        self._it = self._decode()

    def _decode(self):
        import av
        while True:
            with av.open(self._path) as f:
                rs = av.AudioResampler(format="s16", layout="mono", rate=P.SAMPLE_RATE)
                for frame in f.decode(audio=0):
                    for r in rs.resample(frame):
                        yield r.to_ndarray().reshape(-1)
            if not self._loop:
                return

    def next_frame(self) -> np.ndarray | None:
        while len(self._buf) < P.FRAME_SAMPLES:
            chunk = next(self._it, None)
            if chunk is None:
                return None
            self._buf = np.concatenate([self._buf, chunk])
        out, self._buf = self._buf[:P.FRAME_SAMPLES], self._buf[P.FRAME_SAMPLES:]
        return out


class DeviceSource(Source):
    """A live input device via PortAudio. On Windows this uses WASAPI."""

    def __init__(self, device: int | str | None, channel: str = "mix", exclusive: bool = False):
        super().__init__()
        import sounddevice as sd
        self._sd = sd
        self.device = resolve_device(device)
        info = sd.query_devices(self.device)
        self.device_name = info["name"]
        hostapi = sd.query_hostapis(info["hostapi"])["name"]
        self.hostapi = hostapi
        self._channels = min(2, int(info["max_input_channels"]))
        self._channel = channel
        extra = None
        if "WASAPI" in hostapi:
            try:
                extra = sd.WasapiSettings(exclusive=exclusive, auto_convert=not exclusive)
            except TypeError:  # older sounddevice without auto_convert
                extra = sd.WasapiSettings(exclusive=exclusive)
        self._buf = np.zeros(0, np.int16)
        self._stream = sd.InputStream(
            device=self.device, samplerate=P.SAMPLE_RATE, channels=self._channels,
            dtype="int16", blocksize=P.FRAME_SAMPLES, latency="low",
            extra_settings=extra, callback=self._callback)
        self.latency_ms = float(self._stream.latency) * 1000

    def _callback(self, indata, frames, time_info, status):  # PortAudio thread: keep it short
        if status.input_overflow:
            self.overflows += 1
        x = indata
        if x.shape[1] == 1:
            mono = x[:, 0]
        elif self._channel == "left":
            mono = x[:, 0]
        elif self._channel == "right":
            mono = x[:, 1]
        else:
            mono = ((x[:, 0].astype(np.int32) + x[:, 1]) // 2).astype(np.int16)
        if frames == P.FRAME_SAMPLES and not len(self._buf):
            self._put(mono.copy())
            return
        self._buf = np.concatenate([self._buf, mono])
        while len(self._buf) >= P.FRAME_SAMPLES:
            self._put(self._buf[:P.FRAME_SAMPLES].copy())
            self._buf = self._buf[P.FRAME_SAMPLES:]

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        self._stream.stop()
        self._stream.close()


def list_devices() -> list[tuple[int, str, str, int]]:
    import sounddevice as sd
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append((i, d["name"], sd.query_hostapis(d["hostapi"])["name"], d["max_input_channels"]))
    return out


def resolve_device(device: int | str | None) -> int | None:
    """Index, or the first input whose name contains `device`, preferring WASAPI on Windows."""
    if device is None or isinstance(device, int):
        return device
    if device.isdigit():
        return int(device)
    matches = [d for d in list_devices() if device.lower() in d[1].lower()]
    if not matches:
        raise SystemExit(f"No input device matching {device!r}. Run with --list-devices.")
    matches.sort(key=lambda d: "WASAPI" not in d[2])
    return matches[0][0]


# ---------------------------------------------------------------- sender
@dataclass
class Stats:
    frames: int = 0
    packets_sent: int = 0
    send_errors: int = 0
    peak: int = 0
    sum_sq: float = 0.0
    n: int = 0
    started: float = field(default_factory=time.monotonic)

    def level_db(self) -> tuple[float, float]:
        rms = math.sqrt(self.sum_sq / self.n) if self.n else 0
        db = lambda v: 20 * math.log10(v / 32768) if v > 0 else -99.0
        return db(rms), db(self.peak)


class LiveSender:
    def __init__(self, source: Source, port: int = P.DEFAULT_PORT, bind: str = "0.0.0.0",
                 bitrate: int = 64000, redundancy: bool = True, channel: int = 0,
                 clock=time.monotonic):
        self.source = source
        self.channel = channel
        self.redundancy = redundancy
        self.encoder = Encoder(bitrate)
        self.clock = clock
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self.sock.bind((bind, port))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.5)
        self.subscribers: dict[tuple[str, int], float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.stats = Stats()
        self._seq = 0
        self._prev: bytes | None = None
        self._ts = 0

    # -- control traffic
    def handle(self, data: bytes, addr: tuple[str, int]) -> None:
        p = P.unpack(data)
        if p is None:
            return
        now = self.clock()
        if p.type == P.PacketType.SUBSCRIBE:
            with self._lock:
                if addr not in self.subscribers and len(self.subscribers) >= MAX_LISTENERS:
                    return
                if addr not in self.subscribers:
                    log.info("listener joined %s:%d", *addr)
                self.subscribers[addr] = now
                n = len(self.subscribers)
            self._send(P.Packet(P.PacketType.HELLO, self.channel,
                                payload=P.hello_payload(n, self.source.latency_ms)), addr)
        elif p.type == P.PacketType.UNSUBSCRIBE:
            with self._lock:
                if self.subscribers.pop(addr, None) is not None:
                    log.info("listener left %s:%d", *addr)
        elif p.type == P.PacketType.PING:
            self._send(P.Packet(P.PacketType.PONG, self.channel, payload=p.payload[:64]), addr)

    def prune(self) -> None:
        cutoff = self.clock() - P.SUBSCRIBER_TIMEOUT_S
        with self._lock:
            for addr in [a for a, t in self.subscribers.items() if t < cutoff]:
                del self.subscribers[addr]
                log.info("listener timed out %s:%d", *addr)

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                self.prune()
                continue
            except ConnectionResetError:  # Windows: ICMP port unreachable from a gone listener
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            self.handle(data, addr)

    # -- audio
    def _send(self, p: P.Packet, addr) -> bool:
        try:
            self.sock.sendto(P.pack(p), addr)
            return True
        except OSError:
            self.stats.send_errors += 1
            return False

    def send_frame(self, pcm: np.ndarray) -> None:
        s = self.stats
        a = np.abs(pcm.astype(np.int32))
        s.peak = max(s.peak, int(a.max()))
        s.sum_sq += float(np.dot(a, a))
        s.n += len(a)
        for opus_frame in self.encoder.encode(pcm):
            flags, payload = P.audio_payload(opus_frame, self._prev if self.redundancy else None)
            pkt = P.pack(P.Packet(P.PacketType.AUDIO, self.channel, flags, self._seq, self._ts, payload))
            with self._lock:
                targets = list(self.subscribers)
            for addr in targets:
                try:
                    self.sock.sendto(pkt, addr)
                    s.packets_sent += 1
                except OSError:
                    s.send_errors += 1
            self._prev = opus_frame
            self._seq = (self._seq + 1) & 0xFFFFFFFF
        self._ts = (self._ts + P.FRAME_SAMPLES) & 0xFFFFFFFF
        s.frames += 1

    def _audio_loop(self) -> None:
        while not self._stop.is_set():
            try:
                pcm, _ = self.source.frames.get(timeout=0.5)
            except queue.Empty:
                continue
            self.send_frame(pcm)

    # -- lifecycle
    def start(self) -> None:
        for fn, name in ((self._recv_loop, "recv"), (self._audio_loop, "audio")):
            t = threading.Thread(target=fn, name=f"live-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        self.source.start()

    def stop(self) -> None:
        self._stop.set()
        self.source.stop()
        for t in self._threads:
            t.join(1)
        self.sock.close()

    def listener_count(self) -> int:
        with self._lock:
            return len(self.subscribers)


# ---------------------------------------------------------------- discovery
def local_ip() -> str:
    """The address phones should use: the one on the interface with the default route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def advertise(name: str, ip: str, port: int):
    """Advertise over mDNS/Bonjour if zeroconf is installed. Returns a closer or None."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        log.info("zeroconf not installed: phones need the IP address (%s:%d)", ip, port)
        return None
    info = ServiceInfo(P.MDNS_TYPE, f"{name}.{P.MDNS_TYPE}", addresses=[socket.inet_aton(ip)],
                       port=port, properties={"v": str(P.VERSION), "ch": "en"})
    zc = Zeroconf()
    try:
        zc.register_service(info)
    except Exception as e:
        log.warning("mDNS advertise failed: %s", e)
        zc.close()
        return None

    def close():
        zc.unregister_service(info)
        zc.close()
    return close


# ---------------------------------------------------------------- CLI
def meter(db: float, width: int = 20) -> str:
    filled = max(0, min(width, int((db + 60) / 60 * width)))
    return "#" * filled + "-" * (width - filled)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m live.sender", description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--device", help="input device index or part of its name (default: system default)")
    src.add_argument("--tone", action="store_true", help="send a beeping test tone")
    src.add_argument("--file", help="send an audio file in real time (loops)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--channel", choices=["mix", "left", "right"], default="mix",
                    help="which input channel to use on a stereo input")
    ap.add_argument("--exclusive", action="store_true", help="WASAPI exclusive mode (lowest delay)")
    ap.add_argument("--port", type=int, default=P.DEFAULT_PORT)
    ap.add_argument("--bitrate", type=int, default=64000)
    ap.add_argument("--no-redundancy", action="store_true")
    ap.add_argument("--name", default=socket.gethostname(), help="name shown to phones (mDNS)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if a.list_devices:
        for i, name, api, ch in list_devices():
            print(f"{i:3d}  {name}  [{api}, {ch} ch]")
        return

    if a.tone:
        source: Source = ToneSource()
        label = "test tone"
    elif a.file:
        source = FileSource(a.file)
        label = a.file
    else:
        try:
            source = DeviceSource(a.device, a.channel, a.exclusive)
        except Exception as e:
            raise SystemExit(f"Could not open the audio input: {e}\n"
                             "Try --list-devices, a different --device, or drop --exclusive.")
        label = f"{source.device_name} [{source.hostapi}{', exclusive' if a.exclusive else ''}]"

    sender = LiveSender(source, a.port, bitrate=a.bitrate, redundancy=not a.no_redundancy)
    ip = local_ip()
    close_mdns = advertise(a.name, ip, sender.port)
    sender.start()
    print(f"Live English sender: {label}")
    print(f"Listeners connect to {ip}:{sender.port}  (capture latency {source.latency_ms:.1f} ms)")
    print("Ctrl+C to stop.\n")

    def _terminate(*_):
        raise KeyboardInterrupt
    for sig in ("SIGTERM", "SIGBREAK"):  # service stop / console close on Windows
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _terminate)

    try:
        last_frames = 0
        while True:
            time.sleep(1)
            s = sender.stats
            rms, peak = sender.stats.level_db()
            fps = s.frames - last_frames
            last_frames = s.frames
            warn = ""
            if peak > -1:
                warn = "  CLIPPING: turn the input down"
            elif rms < -55 and s.n:
                warn = "  (silent)"
            sys.stdout.write(
                f"\rlevel {meter(rms)} {rms:6.1f} dB  peak {peak:6.1f}  | listeners {sender.listener_count():3d}"
                f"  | {fps:3d} frames/s  drops {source.dropped + source.overflows}{warn}      ")
            sys.stdout.flush()
            s.peak, s.sum_sq, s.n = 0, 0.0, 0
    except KeyboardInterrupt:
        print()
    finally:
        sender.stop()
        if close_mdns:
            close_mdns()


if __name__ == "__main__":
    main()
