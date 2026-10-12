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


# ------------------------------------------------------------------ SpeechT5 (Wolof)

WOLOF_DIR = MODELS_DIR / "wolof"
SPEECHT5_MODEL = "bilalfaye/speecht5_tts-wolof"   # fine-tuned on ~40k sentences by native Wolof speakers (v1: steadier than v0.2)
SPEECHT5_VOCODER = "microsoft/speecht5_hifigan"
_SPEECHT5_LOADED: dict[str, object] = {}


def speecht5_available() -> bool:
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return any(WOLOF_DIR.glob("speaker-*.npy"))


def list_wolof_voices() -> list[dict]:
    if not speecht5_available():
        return []
    names = {"male": "male", "female": "female"}
    return [{"id": f"speecht5:{p.stem[8:]}", "engine": "speecht5", "voice": p.stem[8:],
             "label": f"Wolof — {names.get(p.stem[8:], p.stem[8:])}", "lang": "wo"}
            for p in sorted(WOLOF_DIR.glob("speaker-*.npy"))]


class SpeechT5Tts:
    """Wolof voice. The model takes a speaker profile (x-vector) that sets the voice; runs on CPU
    (faster than the Mac GPU for this model). Speaks sentence by sentence, with a length cap so a
    sentence can never turn into a long babble."""

    name = "speecht5"
    SR = 16000

    def __init__(self, cfg: TtsCfg, out_sr: int):
        import torch
        from transformers import SpeechT5ForTextToSpeech, SpeechT5HifiGan, SpeechT5Processor

        self.cfg = cfg
        self.out_sr = out_sr
        voice = cfg.voice or "female"
        path = WOLOF_DIR / f"speaker-{voice}.npy"
        if not path.exists():
            raise RuntimeError(f"Wolof voice profile {path.name} missing (models/wolof)")
        self.voice = voice
        if "model" not in _SPEECHT5_LOADED:
            torch.set_num_threads(4)
            _SPEECHT5_LOADED["processor"] = SpeechT5Processor.from_pretrained(SPEECHT5_MODEL)
            _SPEECHT5_LOADED["model"] = SpeechT5ForTextToSpeech.from_pretrained(SPEECHT5_MODEL).eval()
            _SPEECHT5_LOADED["vocoder"] = SpeechT5HifiGan.from_pretrained(SPEECHT5_VOCODER).eval()
        self._proc = _SPEECHT5_LOADED["processor"]
        self._model = _SPEECHT5_LOADED["model"]
        self._voc = _SPEECHT5_LOADED["vocoder"]
        self._spk = torch.tensor(np.load(path), dtype=torch.float32).reshape(1, -1)
        self._lock = threading.Lock()

    def _sentence(self, text: str) -> np.ndarray:
        import torch

        ids = self._proc(text=text, return_tensors="pt")["input_ids"]
        with torch.inference_mode():
            # maxlenratio caps the output at ~6 frames per input token: a normal sentence is ~2-3.
            wav = self._model.generate_speech(ids, self._spk, vocoder=self._voc, threshold=0.4,
                                              minlenratio=0.0, maxlenratio=6.0)
        return wav.float().numpy()

    def synth(self, text: str, speed: float = 1.0) -> np.ndarray:
        parts = [p.strip() for p in re.split(r"(?<=[.!?;:])\s+", text) if p.strip()]
        gap = np.zeros(int(0.15 * self.SR), dtype=np.float32)
        out = []
        with self._lock:
            for p in parts or [text]:
                out += [self._sentence(p), gap]
        audio = np.concatenate(out[:-1]) if out else np.zeros(0, np.float32)
        pcm = (np.clip(audio / max(1e-6, float(np.abs(audio).max())) * 0.9, -1, 1) * 32767).astype(np.int16)
        return _resample(pcm, self.SR, self.out_sr)


def list_voices_for(code: str) -> list[dict]:
    if code == "wo":
        return list_wolof_voices()
    if code == "fr":
        return list_voices()
    return []


def make_tts(cfg: TtsCfg, out_sr: int):
    if cfg.engine == "piper":
        return PiperTts(cfg, out_sr)
    if cfg.engine == "speecht5":
        return SpeechT5Tts(cfg, out_sr)
    return SayTts(cfg, out_sr)


class TtsRunner:
    """Runs the (blocking) engine in a worker thread and reports timing.

    With lazy=True the engine is loaded by `await load()` in the background (the Wolof model takes
    a few seconds), and synth() waits for it instead of failing.
    """

    def __init__(self, cfg: TtsCfg, out_sr: int, lazy: bool = False):
        self.cfg = cfg
        self.out_sr = out_sr
        self.engine = None
        self.error = ""
        self.ready = asyncio.Event()
        if not lazy:
            self.reload(cfg)
            self.ready.set()

    async def load(self) -> None:
        await asyncio.to_thread(self.reload, self.cfg)
        self.ready.set()  # on the event loop, not from the worker thread

    def reload(self, cfg: TtsCfg) -> None:
        self.cfg = cfg
        try:
            self.engine = make_tts(cfg, self.out_sr)
            self.error = ""
            log.info("TTS: %s voice=%s rate=%s", self.engine.name, self.engine.voice, cfg.rate)
        except Exception as e:
            # Keep the previous engine if there was one.
            self.error = str(e)
            log.warning("TTS unavailable (%s %s): %s", cfg.engine, cfg.voice, e)

    @property
    def voice(self) -> str:
        return getattr(self.engine, "voice", "")

    async def synth(self, text: str, speed: float = 1.0) -> tuple[np.ndarray, float]:
        if not self.ready.is_set():
            await asyncio.wait_for(self.ready.wait(), 120)
        if self.engine is None:
            raise RuntimeError(self.error or "no TTS engine")
        t0 = time.monotonic()
        pcm = await asyncio.to_thread(self.engine.synth, text, speed)
        return pcm, (time.monotonic() - t0) * 1000
