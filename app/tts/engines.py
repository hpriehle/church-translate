"""TTS engines: macOS `say` (default) and Piper (optional). Both return int16 mono PCM at the stream rate."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from ..config import MODELS_DIR, TtsCfg

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

PIPER_DIR = MODELS_DIR / "piper"
DEFAULT_PIPER_VOICE = "fr_FR-tom-medium"
_PIPER_NAMES = {"tom": "Tom (male)", "siwis": "Siwis (female)", "upmc": "UPMC (female)",
                "gilles": "Gilles (male)", "mls": "MLS"}
_PIPER_LOADED: dict[str, object] = {}


def list_piper_voices() -> list[dict]:
    out = []
    for p in sorted(PIPER_DIR.glob("*.onnx")):
        if not Path(str(p) + ".json").exists():
            continue
        parts = p.stem.split("-")  # fr_FR-tom-medium
        if not parts[0].startswith("fr"):
            continue
        nice = _PIPER_NAMES.get(parts[1], parts[1].capitalize()) if len(parts) > 1 else p.stem
        out.append({"id": f"piper:{p.stem}", "engine": "piper", "voice": p.stem, "label": f"Piper — {nice}", "lang": parts[0]})
    # Default first.
    return sorted(out, key=lambda v: v["voice"] != DEFAULT_PIPER_VOICE)


def list_voices() -> list[dict]:
    """Every French voice the operator can pick, across engines."""
    voices = list_piper_voices()
    for v in french_voices():
        voices.append({"id": f"say:{v['name']}", "engine": "say", "voice": v["name"],
                       "label": f"{v['name']} (macOS) · {v['lang']}", "lang": v["lang"]})
    return voices


class PiperTts:
    """Piper running in-process: the voice model is loaded once, a sentence takes ~0.1–0.2 s."""

    name = "piper"

    def __init__(self, cfg: TtsCfg, out_sr: int):
        from piper import PiperVoice

        self.cfg = cfg
        self.out_sr = out_sr
        voice = cfg.voice if (PIPER_DIR / f"{cfg.voice}.onnx").exists() else ""
        if not voice:
            available = [v["voice"] for v in list_piper_voices()]
            if not available:
                raise RuntimeError("no Piper voice in models/piper — run ./start.sh --setup")
            voice = available[0]
            if cfg.voice:
                log.warning("Piper voice %r not found; using %s", cfg.voice, voice)
        self.voice = voice
        path = str(PIPER_DIR / f"{voice}.onnx")
        if path not in _PIPER_LOADED:
            _PIPER_LOADED[path] = PiperVoice.load(path)
        self._pv = _PIPER_LOADED[path]
        self._lock = threading.Lock()

    def synth(self, text: str, speed: float = 1.0) -> np.ndarray:
        from piper import SynthesisConfig

        # length_scale < 1 speaks faster. cfg.rate is in words per minute, relative to ~175.
        length = 1.0 / max(0.5, speed * self.cfg.rate / 175.0)
        with self._lock:
            chunks = list(self._pv.synthesize(text, syn_config=SynthesisConfig(length_scale=length)))
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        sr = chunks[0].sample_rate
        gap = np.zeros(int(0.12 * sr), dtype=np.int16)  # Piper yields one chunk per sentence
        parts = []
        for c in chunks:
            parts += [c.audio_int16_array.reshape(-1), gap]
        return _resample(np.concatenate(parts[:-1]), sr, self.out_sr)


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
