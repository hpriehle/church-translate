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
import unicodedata
import wave
from pathlib import Path

import numpy as np

from ..config import MODELS_DIR, PIPER_DIR, TtsCfg

log = logging.getLogger("tts")


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return data, sr


def _tempo(pcm: np.ndarray, sr: int, factor: float) -> np.ndarray:
    """Speak faster/slower without changing the pitch (ffmpeg atempo), for voices with no speed control."""
    if abs(factor - 1.0) < 0.02 or not len(pcm) or not shutil.which("ffmpeg"):
        return pcm
    factor = min(2.0, max(0.5, factor))
    r = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "s16le", "-ar", str(sr), "-ac", "1",
                        "-i", "pipe:0", "-filter:a", f"atempo={factor:.3f}", "-f", "s16le", "pipe:1"],
                       input=pcm.astype(np.int16).tobytes(), capture_output=True, timeout=30)
    return np.frombuffer(r.stdout, dtype=np.int16) if r.returncode == 0 and r.stdout else pcm


def _resample(pcm: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return pcm
    import soxr

    return soxr.resample(pcm, sr_in, sr_out).astype(np.int16)


# ------------------------------------------------------------------ Piper

DEFAULT_PIPER_VOICES = {"fr_FR-tom-medium", "es_MX-ald-medium", "ru_RU-denis-medium", "pt_BR-cadu-medium",
                        "uk_UA-tetiana-high", "fa_IR-amir-medium", "ne_NP-chitwan-medium", "vi_VN-vais1000-medium",
                        "zh_CN-chaowen-medium", "en_US-joe-medium", "lv_LV-aivars-medium"}
# What the operator sees for each voice: a common first name for that language, never a model or engine name.
_VOICE_PEOPLE = {
    "fr": [("fr_FR-tom-medium", "Male", "Julien"), ("fr_FR-siwis-medium", "Female", "Camille")],
    "wo": [("male", "Male", "Moussa"), ("female", "Female", "Fatou")],
    "es": [("es_MX-ald-medium", "Male", "Carlos"), ("es_MX-claude-high", "Female", "Lucía")],
    "ru": [("ru_RU-denis-medium", "Male", "Ivan"), ("ru_RU-dmitri-medium", "Male", "Alexei")],
    "pt": [("pt_BR-cadu-medium", "Male", "João"), ("pt_BR-faber-medium", "Male", "Pedro")],
    "uk": [("uk_UA-tetiana-high", "Female", "Olena"), ("uk_UA-oleksa-high", "Male", "Andriy")],
    "fa": [("fa_IR-amir-medium", "Male", "Ali"), ("fa_IR-ganji-medium", "Male", "Reza")],
    "ne": [("ne_NP-chitwan-medium", "Male", "Ram")],
    "vi": [("vi_VN-vais1000-medium", "Female", "Lan")],
    "zh": [("zh_CN-chaowen-medium", "Female", "Mei")],
    "en": [("en_US-joe-medium", "Male", "James"), ("en_US-kristin-medium", "Female", "Mary")],
    "lv": [("lv_LV-aivars-medium", "Male", "Jānis")],
}
_PIPER_LOADED: dict[str, object] = {}


def voice_label(lang: str, voice: str) -> str:
    """e.g. "Male 2 - Alexei": numbered within its language, the same whichever voices are downloaded."""
    seen: dict[str, int] = {}
    for v, kind, name in _VOICE_PEOPLE.get(lang, ()):
        seen[kind] = seen.get(kind, 0) + 1
        if v == voice:
            return f"{kind} {seen[kind]} - {name}"
    return "Voice"


def voice_catalog() -> dict[str, list[dict]]:
    """Every voice each language offers, downloaded or not, so the operator can hear them before downloading.
    The Mac app plays each from a recording built into it: mac/make_voice_samples.py makes them."""
    return {lang: [{"id": f"{'speecht5' if lang == 'wo' else 'piper'}:{v}", "engine": "speecht5" if lang == "wo" else "piper",
                    "voice": v, "label": voice_label(lang, v), "lang": lang} for v, _, _ in people]
            for lang, people in _VOICE_PEOPLE.items()}


def list_piper_voices(lang: str = "fr") -> list[dict]:
    out = []
    for p in sorted(PIPER_DIR.glob("*.onnx")):
        if not Path(str(p) + ".json").exists():
            continue
        parts = p.stem.split("-")  # fr_FR-tom-medium
        if not parts[0].startswith(lang):
            continue
        out.append({"id": f"piper:{p.stem}", "engine": "piper", "voice": p.stem, "label": voice_label(lang, p.stem), "lang": parts[0]})
    # Default first.
    return sorted(out, key=lambda v: v["voice"] not in DEFAULT_PIPER_VOICES)


def list_voices(lang: str = "fr") -> list[dict]:
    """Every voice for one language the operator can pick, across engines."""
    return list_piper_voices(lang)


class PiperTts:
    """Piper running in-process: the voice model is loaded once, a sentence takes ~0.1–0.2 s."""

    name = "piper"

    def __init__(self, cfg: TtsCfg, out_sr: int):
        from piper import PiperVoice

        self.cfg = cfg
        self.out_sr = out_sr
        voice = cfg.voice if (PIPER_DIR / f"{cfg.voice}.onnx").exists() else ""
        if not voice:
            available = [v["voice"] for v in list_piper_voices(cfg.lang)]
            if not available:
                raise RuntimeError("no Piper voice in models/piper — run ./start.sh --setup")
            voice = available[0]
            if cfg.voice:
                log.warning("Piper voice %r not found; using %s", cfg.voice, voice)
        self.voice = voice
        path = str(PIPER_DIR / f"{voice}.onnx")
        if path not in _PIPER_LOADED:
            if voice.startswith("zh_"):   # its tokenizer needs transformers, which the Mac app downloads
                from ..packages import activate_libs

                activate_libs()
            _PIPER_LOADED[path] = PiperVoice.load(path, download_dir=PIPER_DIR)
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


# Meta's MMS Wolof voice (CC BY-NC 4.0): one voice, about 4x faster than SpeechT5 on a plain CPU.
MMS_WOLOF_MODEL = "rnjema-unima/mms-tts-wol-baseline"
_MMS_LOADED: dict = {}


def _torch_available() -> bool:
    from ..packages import activate_libs

    activate_libs()   # the Mac app downloads these with the Wolof voice
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


def speecht5_available() -> bool:
    return _torch_available() and any(WOLOF_DIR.glob("speaker-*.npy"))


def list_wolof_voices() -> list[dict]:
    if not _torch_available():
        return []
    # The MMS voice is non-commercial (CC BY-NC), so the paid app never offers it.
    return [{"id": f"speecht5:{p.stem[8:]}", "engine": "speecht5", "voice": p.stem[8:],
             "label": voice_label("wo", p.stem[8:]), "lang": "wo"}
            for p in sorted(WOLOF_DIR.glob("speaker-*.npy"))]


def mms_wolof_text(text: str) -> str:
    """The MMS voice learned Wolof written without accents, x or q (x as "kh"): spell it the same way."""
    t = text.lower().replace("x", "kh").replace("q", "k")
    return "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))


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
        return _resample(_tempo(pcm, self.SR, self.cfg.speed * speed), self.SR, self.out_sr)


class MmsTts:
    """Wolof voice from Meta's MMS project (one voice). Fast enough for a server without a GPU."""

    name = "mms"

    def __init__(self, cfg: TtsCfg, out_sr: int):
        import torch
        from transformers import AutoTokenizer, VitsModel

        self.cfg = cfg
        self.out_sr = out_sr
        self.voice = "default"
        if "model" not in _MMS_LOADED:
            torch.set_num_threads(4)
            _MMS_LOADED["tokenizer"] = AutoTokenizer.from_pretrained(MMS_WOLOF_MODEL)
            _MMS_LOADED["model"] = VitsModel.from_pretrained(MMS_WOLOF_MODEL).eval()
        self._tok = _MMS_LOADED["tokenizer"]
        self._model = _MMS_LOADED["model"]
        self._chars = {k for k in self._tok.get_vocab() if len(k) == 1}
        self.SR = self._model.config.sampling_rate
        self._lock = threading.Lock()

    def synth(self, text: str, speed: float = 1.0) -> np.ndarray:
        import torch

        # The voice ignores punctuation, so pause between sentences and clauses ourselves.
        parts = ["".join(c for c in mms_wolof_text(p) if c in self._chars).strip()
                 for p in re.split(r"(?<=[.!?;:,])\s+", text)]
        gap = np.zeros(int(0.12 * self.SR), dtype=np.float32)
        out = []
        with self._lock, torch.inference_mode():
            for p in parts:
                if p:
                    ids = self._tok(p, return_tensors="pt")
                    out += [self._model(**ids).waveform[0].float().numpy(), gap]
        audio = np.concatenate(out[:-1]) if out else np.zeros(0, np.float32)
        pcm = (np.clip(audio / max(1e-6, float(np.abs(audio).max())) * 0.9, -1, 1) * 32767).astype(np.int16)
        # The model's own speaking_rate falls short (each sound has a minimum length), so time-stretch.
        return _resample(_tempo(pcm, self.SR, self.cfg.speed * speed), self.SR, self.out_sr)


def list_voices_for(code: str) -> list[dict]:
    if code == "wo":
        return list_wolof_voices()
    return list_voices(code)


def make_tts(cfg: TtsCfg, out_sr: int):
    if cfg.engine == "piper":
        return PiperTts(cfg, out_sr)
    if cfg.engine == "speecht5":
        return SpeechT5Tts(cfg, out_sr)
    if cfg.engine == "mms":
        return MmsTts(cfg, out_sr)
    raise ValueError(f"unknown voice engine {cfg.engine!r}")


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

    def unload(self) -> None:
        """The operator removed this language's voice: free it, and load from nothing if it comes back."""
        self.engine = None
        self.error = ""
        self.ready.clear()
        for path in [p for p in _PIPER_LOADED if Path(p).name.startswith(self.cfg.lang)]:
            del _PIPER_LOADED[path]

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
