"""Audio sources (live input device or a recording played back in real time) + the capture worker.

The worker thread turns any source into 512-sample 16 kHz frames with capture timestamps,
runs the VAD chunker on them and hands finished chunks to the asyncio pipeline.
"""
from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

from .chunker import FRAME, SR, Chunk, Chunker

log = logging.getLogger("audio")

try:  # PortAudio may be missing on headless Linux dev boxes
    import sounddevice as sd
except (OSError, ImportError):  # pragma: no cover
    sd = None


_active_streams = 0  # PortAudio may only be re-initialised while no stream is open


def refresh_devices() -> None:
    """PortAudio reads the device list once; re-read it so a mic plugged in later shows up."""
    if sd is None or _active_streams:
        return
    try:
        sd._terminate()
        sd._initialize()
    except Exception as e:  # pragma: no cover
        log.warning("could not refresh audio devices: %s", e)


def list_input_devices() -> list[dict]:
    if sd is None:
        return []
    refresh_devices()
    try:
        devices = sd.query_devices()
        default_in = sd.default.device[0] if sd.default.device else -1
    except Exception as e:  # pragma: no cover
        log.warning("cannot list audio devices: %s", e)
        return []
    out = []
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            out.append({
                "index": i,
                "name": d["name"],
                "channels": d["max_input_channels"],
                "samplerate": int(d.get("default_samplerate") or 48000),
                "default": i == default_in,
            })
    return out


def resolve_device(name: str) -> dict | None:
    devices = list_input_devices()
    if not devices:
        return None
    if name:
        for d in devices:
            if d["name"] == name:
                return d
        for d in devices:
            if name.lower() in d["name"].lower():
                return d
        if name.isdigit():
            for d in devices:
                if d["index"] == int(name):
                    return d
    return next((d for d in devices if d["default"]), devices[0])


class Source:
    label = "source"

    def blocks(self) -> Iterator[tuple[np.ndarray, float]]:
        """Yield (float32 mono 16 kHz samples, monotonic time of the last sample)."""
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class MicSource(Source):
    """A PortAudio input device, chosen ONCE here by name (or the system default at that moment).

    The device is pinned for the whole meeting: we never ask for "the default input" again, so when macOS
    switches its default (AirPods connecting, a monitor's mic appearing) we keep listening to the chosen mic.
    If that mic stops delivering sound, `warning` is set, the meeting keeps running and we keep trying to
    reopen the SAME device by name; we never fall back to another one.
    """

    STALL_S = 4.0     # no audio block for this long = the microphone has stopped
    RETRY_S = 2.0     # how often to try to reopen it

    def __init__(self, device_name: str = "", channel: int = 0):
        if sd is None:
            raise RuntimeError("sounddevice/PortAudio is not available on this machine")
        refresh_devices()
        dev = resolve_device(device_name)
        if dev is None:
            raise RuntimeError("no audio input device found")
        self.device = dev
        self.name = dev["name"]          # pinned: reopening looks this exact name up again
        self.channel = channel
        self.native_sr = dev["samplerate"]
        self.label = f"input: {dev['name']}"
        self.warning: str | None = None  # short sentence for the operator while the mic is not delivering sound
        self._q: queue.Queue = queue.Queue(maxsize=500)
        self._stop = threading.Event()
        self._stream = None
        self._stream_dead = False

    def _callback(self, indata, frames, time_info, status):  # PortAudio thread
        if status:
            log.debug("input status: %s", status)
        if self.channel and self.channel <= indata.shape[1]:
            mono = indata[:, self.channel - 1].copy()
        else:
            mono = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0].copy()
        try:
            self._q.put_nowait((mono.astype(np.float32), time.monotonic()))
        except queue.Full:
            pass

    def _finished(self) -> None:  # PortAudio thread: the stream ended on its own (device unplugged, error)
        self._stream_dead = True

    def _open(self) -> None:
        """Open the pinned device. Raises if it cannot be opened."""
        global _active_streams
        # To read input channel N we must open N channels; otherwise mix down up to 2.
        channels = min(self.device["channels"], self.channel) if self.channel else min(self.device["channels"], 2)
        self._stream_dead = False
        stream = sd.InputStream(device=self.device["index"], channels=channels,
                                samplerate=self.native_sr, dtype="float32",
                                blocksize=int(self.native_sr * 0.05), callback=self._callback,
                                finished_callback=self._finished)
        stream.start()
        self._stream = stream
        _active_streams += 1

    def _close(self) -> None:
        global _active_streams
        stream, self._stream = self._stream, None
        if stream is None:
            return
        _active_streams = max(0, _active_streams - 1)
        for fn in (stream.stop, stream.close):
            try:
                fn()
            except Exception:
                pass

    def _reopen_same(self) -> bool:
        """Find our device again BY NAME in a fresh device list and open it. No fallback to another device."""
        self._close()                         # PortAudio may only re-scan devices while no stream is open
        refresh_devices()
        dev = next((d for d in list_input_devices() if d["name"] == self.name), None)
        if dev is None:
            self.warning = (f"The microphone “{self.name}” isn't connected. Plug it back in — "
                            "translation resumes when it's back.")
            return False
        self.device, self.native_sr = dev, dev["samplerate"]
        try:
            self._open()
        except Exception as e:
            log.info("mic %s: reopen failed: %s", self.name, e)
            self.warning = (f"The microphone “{self.name}” stopped sending sound. Check its cable — "
                            "translation resumes when it's back.")
            return False
        return True

    def blocks(self):
        import soxr

        try:
            self._open()
        except Exception as e:
            hint = ""
            if sys.platform == "darwin":
                hint = (" — on a Mac this usually means the server isn't allowed to use the microphone: System "
                        "Settings → Privacy & Security → Microphone (turn on python3 / uv), or record from the "
                        "speak page (http://localhost:8000/speak) instead.")
            raise RuntimeError(f"cannot open {self.device['name']}: {e}{hint}") from e
        rs = soxr.ResampleStream(self.native_sr, SR, 1, dtype="float32", quality="HQ")
        log.info("capturing from %s @ %d Hz", self.device["name"], self.native_sr)
        last_audio = time.monotonic()
        next_retry = 0.0
        try:
            while not self._stop.is_set():
                try:
                    block, t = self._q.get(timeout=0.2)
                except queue.Empty:
                    now = time.monotonic()
                    if self._stream is not None and (self._stream_dead or now - last_audio > self.STALL_S):
                        log.warning("mic %s stopped sending audio", self.name)
                        self.warning = (f"The microphone “{self.name}” stopped sending sound. Check its cable — "
                                        "translation resumes when it's back.")
                        self._close()
                        next_retry = now + self.RETRY_S
                    elif self._stream is None and now >= next_retry:
                        next_retry = now + self.RETRY_S
                        if self._reopen_same():
                            log.info("mic %s is back", self.name)
                            rs = soxr.ResampleStream(self.native_sr, SR, 1, dtype="float32", quality="HQ")
                            last_audio = now   # warning clears when the first block arrives
                    continue
                last_audio = time.monotonic()
                if self.warning:
                    self.warning = None
                out = rs.resample_chunk(block)
                if len(out):
                    yield out, t
        finally:
            self._close()

    def stop(self):
        self._stop.set()


class BrowserSource(Source):
    """Microphone audio streamed from a web page (/speak) as 16-bit PCM at the browser's rate."""

    def __init__(self, sample_rate: int, label: str = "browser microphone"):
        self.sample_rate = int(sample_rate)
        self.label = label
        self._q: queue.Queue = queue.Queue(maxsize=600)
        self._stop = threading.Event()

    def push(self, data: bytes) -> None:
        pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        try:
            self._q.put_nowait((pcm, time.monotonic()))
        except queue.Full:
            pass

    def blocks(self):
        import soxr

        rs = soxr.ResampleStream(self.sample_rate, SR, 1, dtype="float32", quality="HQ")
        while not self._stop.is_set():
            try:
                pcm, t = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            out = rs.resample_chunk(pcm)
            if len(out):
                yield out, t

    def stop(self):
        self._stop.set()


class FileSource(Source):
    """Plays a recording into the pipeline at real-time speed, as if it were live."""

    def __init__(self, path: str | Path, speed: float = 1.0, start_s: float = 0.0, max_s: float | None = None):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is required for test files (brew install ffmpeg)")
        self.speed = max(0.1, float(speed))
        self.start_s = start_s
        self.max_s = max_s
        self.label = f"test file: {self.path.name}"
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None

    def blocks(self):
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error"]
        if self.start_s:
            cmd += ["-ss", str(self.start_s)]
        cmd += ["-i", str(self.path)]
        if self.max_s:
            cmd += ["-t", str(self.max_s)]
        cmd += ["-f", "f32le", "-ac", "1", "-ar", str(SR), "pipe:1"]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        block = 1600  # 100 ms
        t0 = time.monotonic()
        done = 0
        try:
            while not self._stop.is_set():
                raw = self._proc.stdout.read(block * 4)
                if not raw:
                    break
                samples = np.frombuffer(raw, dtype=np.float32)
                done += len(samples)
                due = t0 + done / SR / self.speed
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                yield samples, due
        finally:
            if self._proc.poll() is None:
                self._proc.kill()
            self._proc.wait()

    def stop(self):
        self._stop.set()


class CaptureWorker(threading.Thread):
    """Reads a Source, runs the chunker, reports levels, and calls back with chunks."""

    def __init__(self, source: Source, chunker: Chunker, gain: float,
                 on_chunk: Callable[[Chunk], None], on_level: Callable[[float], None],
                 on_end: Callable[[str | None], None]):
        super().__init__(daemon=True, name="capture")
        self.source = source
        self.chunker = chunker
        self.gain = gain
        self.on_chunk = on_chunk
        self.on_level = on_level
        self.on_end = on_end

    def run(self):
        err = None
        buf = np.zeros(0, dtype=np.float32)
        level_acc: list[float] = []
        try:
            for samples, t_last in self.source.blocks():
                if self.gain != 1.0:
                    samples = np.clip(samples * self.gain, -1.0, 1.0)
                buf = np.concatenate([buf, samples])
                n_frames = len(buf) // FRAME
                remaining_after = len(buf) - n_frames * FRAME
                for i in range(n_frames):
                    frame = buf[i * FRAME:(i + 1) * FRAME]
                    t_frame = t_last - ((n_frames - 1 - i) * FRAME + remaining_after) / SR
                    level_acc.append(float(np.sqrt(np.mean(frame ** 2)) + 1e-9))
                    if len(level_acc) >= 3:  # ~100 ms
                        rms = max(level_acc)
                        level_acc.clear()
                        self.on_level(20 * np.log10(rms))
                    for chunk in self.chunker.feed(frame, t_frame):
                        self.on_chunk(chunk)
                buf = buf[n_frames * FRAME:]
            for chunk in self.chunker.flush():
                self.on_chunk(chunk)
        except Exception as e:  # pragma: no cover - surfaced to the operator page
            log.exception("capture failed")
            err = str(e)
        finally:
            self.on_end(err)

    def stop(self):
        self.source.stop()
