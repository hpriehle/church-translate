"""TTS engines: macOS `say` (default) and Piper (optional). Both return int16 mono PCM at the stream rate."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from ..config import TtsCfg

log = logging.getLogger("tts")


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return data, sr


def _resample(pcm: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return pcm
    import soxr

    return soxr.resample(pcm, sr_in, sr_out).astype(np.int16)


# ------------------------------------------------------------------ macOS say

_VOICE_LINE = re.compile(r"^(?P<name>.+?)\s+(?P<lang>[a-z]{2,3}_[A-Z0-9]{2,3})\s+#")


def list_say_voices() -> list[dict]:
    if not shutil.which("say"):
        return []
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    voices = []
    for line in out.splitlines():
        m = _VOICE_LINE.match(line)
        if m:
            voices.append({"name": m.group("name").strip(), "lang": m.group("lang")})
    return voices


def french_voices() -> list[dict]:
    def score(v: dict) -> tuple:
        name = v["name"]
        quality = 3 if "Premium" in name else 2 if "Enhanced" in name or "améliorée" in name.lower() else 1
        region = 2 if v["lang"] == "fr_FR" else 1
        return (quality, region, "Thomas" in name)

    fr = [v for v in list_say_voices() if v["lang"].startswith("fr")]
    return sorted(fr, key=score, reverse=True)


class SayTts:
    name = "say"

    def __init__(self, cfg: TtsCfg, out_sr: int):
        if sys.platform != "darwin" or not shutil.which("say"):
            raise RuntimeError("macOS `say` is not available on this machine")
        self.cfg = cfg
        self.out_sr = out_sr
        voices = french_voices()
        names = {v["name"] for v in voices}
        if cfg.voice and cfg.voice in names:
            self.voice = cfg.voice
        elif voices:
            if cfg.voice:
                log.warning("voice %r not installed; using %s", cfg.voice, voices[0]["name"])
            self.voice = voices[0]["name"]
        else:
            raise RuntimeError("no French voice installed (System Settings → Accessibility → Spoken Content)")

    def synth(self, text: str, speed: float = 1.0) -> np.ndarray:
        rate = int(round(self.cfg.rate * speed))
        with tempfile.TemporaryDirectory() as td:
            txt = Path(td) / "t.txt"
            wav = Path(td) / "o.wav"
            txt.write_text(text, encoding="utf-8")
            subprocess.run(["say", "-v", self.voice, "-r", str(rate), "-f", str(txt), "-o", str(wav),
                            "--file-format=WAVE", f"--data-format=LEI16@{self.out_sr}"],
                           check=True, capture_output=True, timeout=30)
            pcm, sr = _read_wav(wav)
        return _resample(pcm, sr, self.out_sr)


# ------------------------------------------------------------------ Piper

class PiperTts:
    name = "piper"

    def __init__(self, cfg: TtsCfg, out_sr: int):
        self.cfg = cfg
        self.out_sr = out_sr
        self.binary = shutil.which("piper") or str(Path("~/.local/bin/piper").expanduser())
        self.model = Path(cfg.piper_model).expanduser()
        if not Path(self.binary).exists():
            raise RuntimeError("piper is not installed (pip install piper-tts)")
        if not self.model.exists():
            raise RuntimeError(f"Piper voice not found: {self.model}")
        self.voice = self.model.stem

    def synth(self, text: str, speed: float = 1.0) -> np.ndarray:
        # Piper's length_scale: <1 is faster. cfg.rate is relative to ~175 wpm.
        length = 1.0 / max(0.5, speed * self.cfg.rate / 175.0)
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "o.wav"
            subprocess.run([self.binary, "--model", str(self.model), "--output_file", str(wav),
                            "--length_scale", f"{length:.3f}", "--sentence_silence", "0.15"],
                           input=text.encode("utf-8"), check=True, capture_output=True, timeout=60)
            pcm, sr = _read_wav(wav)
        return _resample(pcm, sr, self.out_sr)


def make_tts(cfg: TtsCfg, out_sr: int):
    if cfg.engine == "piper":
        return PiperTts(cfg, out_sr)
    return SayTts(cfg, out_sr)


class TtsRunner:
    """Runs the (blocking) engine in a worker thread and reports timing."""

    def __init__(self, cfg: TtsCfg, out_sr: int):
        self.cfg = cfg
        self.out_sr = out_sr
        self.engine = None
        self.error = ""
        self.reload(cfg)

    def reload(self, cfg: TtsCfg) -> None:
        self.cfg = cfg
        try:
            self.engine = make_tts(cfg, self.out_sr)
            self.error = ""
            log.info("TTS: %s voice=%s rate=%s", self.engine.name, self.engine.voice, cfg.rate)
        except Exception as e:
            # Keep the previous engine if there was one.
            self.error = str(e)
            log.warning("TTS unavailable: %s", e)

    @property
    def voice(self) -> str:
        return getattr(self.engine, "voice", "")

    async def synth(self, text: str, speed: float = 1.0) -> tuple[np.ndarray, float]:
        if self.engine is None:
            raise RuntimeError(self.error or "no TTS engine")
        t0 = time.monotonic()
        pcm = await asyncio.to_thread(self.engine.synth, text, speed)
        return pcm, (time.monotonic() - t0) * 1000
