"""Wolof speech recognition (docs/wolof-speaker.md).

Whisper has no Wolof, so a Wolof speaker language gets its own recogniser, downloaded when Wolof is first picked:
  - Apple Silicon: Kiriku-Wolof-ASR (AI Hub Senegal, a Whisper large-v2 fine-tune) converted to mlx
    (scripts/models/convert_kiriku_mlx.py), run with mlx-whisper's decoder;
  - everything else (Intel Macs, Linux, Windows): Meta Omnilingual ASR CTC 300M exported to ONNX
    (scripts/models/export_omnilingual_onnx.py), run with onnxruntime. Kiriku is far too slow on a CPU.
Both come from our own download host (wolof_asr.json names the archive and its hash), never from Hugging Face.

Whisper's language detector can't hear Wolof either (it guesses Javanese, French...), so when Wolof is a speaker
language each chunk is routed by `route`: to another speaker language only when the detector is sure of it, else
to Wolof. Wolof is never added to the speaker languages automatically, and nothing else is while Wolof is one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tarfile
import threading
import zlib
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .config import HOME

log = logging.getLogger("wolof_asr")

WOLOF = "wo"
# Speaker languages Whisper's detector can't hear: never added automatically, routed by `route`.
UNDETECTABLE = {WOLOF}

MANIFEST = Path(__file__).with_name("wolof_asr.json")
MODELS_DIR = HOME / "models"
PACKAGE_ID = "speech-wo"

# ------------------------------------------------------------------ which recogniser hears a chunk

# Another speaker language is recognised by Whisper only when the detector is at least this sure of it...
OTHER_SURE = 0.5
# ... French more so: Wolof preaching is full of French words, and the detector's guess for Wolof is often French.
# (Kiriku writes French words as French anyway, so a mixed chunk loses little going to Wolof.)
FRENCH_SURE = 0.7
STICKY = 0.1          # still speaking the language of the chunk before: this much less is enough
# A language outside the speaker set is recognised as itself (never added) only when this sure, and never one of
# the detector's usual guesses for Wolof.
OUTSIDE_SURE = 0.8
LOOKALIKES = {"jw", "fr"}


def route(probs: dict[str, float], speakers: list[str], prev: str = "") -> tuple[str, str]:
    """(the language a chunk is recognised as, the detector's top language) for a meeting with Wolof among its
    speaker languages. Wolof unless another language is clearly heard."""
    others = [c for c in speakers if c not in UNDETECTABLE]
    if not others or not probs:
        return WOLOF, ""
    top = max(probs, key=lambda c: probs[c])
    p = float(probs[top])
    if top in others:
        bar = (FRENCH_SURE if top == "fr" else OTHER_SURE) - (STICKY if top == prev else 0.0)
        if p >= bar:
            return top, top
    elif top not in LOOKALIKES and p >= OUTSIDE_SURE:
        return top, top
    return WOLOF, top


# ------------------------------------------------------------------ the models

def runtime(engine: str) -> str:
    """"mlx" (Kiriku) where speech recognition runs on mlx (Apple Silicon), else "onnx" (Omnilingual)."""
    return "mlx" if engine == "mlx" else "onnx"


def manifest(rt: str) -> dict[str, Any] | None:
    """The published model for a runtime: {"id", "url", "sha256", "size", "name", ...}, or None before it is."""
    try:
        entry = json.loads(MANIFEST.read_text()).get(rt)
    except FileNotFoundError:
        return None
    return entry if entry and entry.get("sha256") else None


def model_dir(entry: dict[str, Any]) -> Path:
    return MODELS_DIR / entry["id"]


def installed(entry: dict[str, Any] | None) -> bool:
    return bool(entry) and (model_dir(entry) / "config.json").exists()


def install(archive: Path, entry: dict[str, Any]) -> None:
    """Check a downloaded archive against the hash the app carries, unpack it (all at once: a half-unpacked model
    never counts as installed) and drop older Wolof models of the same runtime."""
    digest = hashlib.sha256()
    with open(archive, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != entry["sha256"]:
        archive.unlink(missing_ok=True)
        raise RuntimeError("the Wolof speech model download was damaged")
    dest = model_dir(entry)
    work = dest.with_name(dest.name + ".unpacking")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    shutil.rmtree(dest, ignore_errors=True)
    (work / entry["id"]).rename(dest)
    shutil.rmtree(work, ignore_errors=True)
    archive.unlink(missing_ok=True)
    prefix = entry["id"].rsplit("-", 1)[0]          # wolof-asr-kiriku-mlx-q8-<hash>: same model, other versions
    for old in MODELS_DIR.glob(prefix + "-*"):
        if old.is_dir() and old.name != entry["id"] and not old.name.endswith(".unpacking"):
            shutil.rmtree(old, ignore_errors=True)


def remove(entry: dict[str, Any]) -> None:
    shutil.rmtree(model_dir(entry), ignore_errors=True)
    for f in MODELS_DIR.glob(entry["id"] + ".tar.gz*"):
        f.unlink(missing_ok=True)


def compression_ratio(text: str) -> float:
    b = text.encode("utf-8")
    return len(b) / len(zlib.compress(b)) if b else 0.0


def decode_tokens(ids: list[int], decode: Callable[[list[int]], str], extra: dict[int, str], eot: int) -> str:
    """Kiriku's tokens as text: the letters it added after Whisper's timestamp tokens (ŋ ɗ ɓ ƴ) as those letters
    (mlx-whisper would read them as timestamps and drop them), Whisper's own text tokens through `decode`, and
    anything else (special and timestamp tokens) left out."""
    out: list[str] = []
    run: list[int] = []
    for t in ids:
        if t in extra:
            if run:
                out.append(decode(run))
                run = []
            out.append(extra[t])
        elif t < eot:
            run.append(t)
    if run:
        out.append(decode(run))
    return "".join(out)


class KirikuMlx:
    """Kiriku (Whisper large-v2 fine-tune) on mlx. Its own model object: mlx-whisper's ModelHolder holds one
    model, the meeting's Whisper, and swapping would reload gigabytes on every change of language."""

    TEMPERATURES = (0.0, 0.2, 0.4)

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.model = None
        self.tokens: dict[str, Any] = {}

    def load(self) -> None:
        import mlx.core as mx
        from mlx_whisper.load_models import load_model
        from mlx_whisper.tokenizer import get_tokenizer

        self.tokens = json.loads((self.folder / "tokens.json").read_text())
        model = load_model(str(self.folder), dtype=mx.float16)
        # mlx-whisper counts the languages from the vocabulary size: 103 for Kiriku's bigger vocabulary, which
        # would shift every special token (start, language, no-timestamps) by one. Large-v2 has 99.
        n = int(self.tokens.get("num_languages", 99))
        model.__class__ = type("KirikuWhisper", (model.__class__,), {"num_languages": n})
        self.model = model
        self.tokenizer = get_tokenizer(True, num_languages=n, language=self.tokens.get("language", "fr"),
                                       task="transcribe")
        self.extra = {int(k): v for k, v in (self.tokens.get("extra") or {}).items()}
        # Never sample a timestamp (no-timestamp decoding), nor the tokens mlx-whisper suppresses by default.
        first_extra = min(self.extra, default=self.model.dims.n_vocab)
        self.suppress = [-1, *range(self.tokenizer.timestamp_begin, first_extra)]
        self.transcribe(np.zeros(16000, dtype=np.float32))   # compiles the graphs before the first real chunk

    def transcribe(self, audio: np.ndarray) -> tuple[str, float, float, float]:
        """(text, no-speech probability, mean log probability, compression ratio) for one chunk (< 30 s)."""
        import mlx.core as mx
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
        from mlx_whisper.decoding import DecodingOptions, decode

        mel = log_mel_spectrogram(audio, n_mels=self.model.dims.n_mels, padding=N_SAMPLES)
        mel = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
        r = None
        for t in self.TEMPERATURES:
            r = decode(self.model, mel, DecodingOptions(
                language=self.tokens.get("language", "fr"), task="transcribe", temperature=t,
                without_timestamps=True, suppress_tokens=list(self.suppress), fp16=True))
            text = decode_tokens(r.tokens, self.tokenizer.encoding.decode, self.extra, self.tokenizer.eot).strip()
            if compression_ratio(text) <= 2.4 and r.avg_logprob >= -1.0:
                break
        return text, float(r.no_speech_prob), float(r.avg_logprob), compression_ratio(text)


def ctc_decode(ids, pieces: list[str], skip: set[int]) -> str:
    """Greedy CTC: repeats collapsed, blank and special ids dropped, SentencePiece pieces joined."""
    out, prev = [], -1
    for i in ids:
        i = int(i)
        if i != prev and i not in skip:
            out.append(pieces[i])
        prev = i
    return "".join(out).replace("▁", " ").strip()


class OmniOnnx:
    """Meta Omnilingual ASR CTC on onnxruntime (CPU): one encoder pass per chunk, no decoding loop."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.session = None

    def load(self) -> None:
        import onnxruntime as ort

        cfg = json.loads((self.folder / "config.json").read_text())
        tok = json.loads((self.folder / "tokens.json").read_text())
        self.pieces, self.skip = tok["pieces"], set(tok["skip"])
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(str(self.folder / cfg.get("file", "model.onnx")), opts,
                                            providers=["CPUExecutionProvider"])
        self.transcribe(np.zeros(16000, dtype=np.float32))

    def transcribe(self, audio: np.ndarray) -> tuple[str, float, float, float]:
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if len(audio) < 1600:
            return "", 1.0, -2.0, 0.0
        logits = self.session.run(None, {"waveform": audio[None]})[0][0]
        ids = logits.argmax(-1)
        # Mean log probability of the chosen frames that aren't blank: the CTC stand-in for Whisper's avg_logprob.
        z = logits - logits.max(-1, keepdims=True)
        logp = -np.log(np.exp(z).sum(-1))
        speech = np.array([int(i) not in self.skip for i in ids])
        alp = float(logp[speech].mean()) if speech.any() else -2.0
        text = ctc_decode(ids, self.pieces, self.skip)
        return text, 0.0 if text else 1.0, alp, compression_ratio(text)


class WolofRecogniser:
    """The Wolof model for this computer: loaded when Wolof is a speaker language and its download is there."""

    def __init__(self, engine: str):
        self.runtime = runtime(engine)
        self.model: KirikuMlx | OmniOnnx | None = None
        self.error = ""
        self._lock = threading.Lock()

    @property
    def entry(self) -> dict[str, Any] | None:
        return manifest(self.runtime)

    @property
    def ready(self) -> bool:
        return self.model is not None

    def available(self) -> bool:
        """Downloaded (or a model folder given to a dev box: HC_WOLOF_ASR_DIR)."""
        return self._folder() is not None

    def _folder(self) -> Path | None:
        import os

        if os.environ.get("HC_WOLOF_ASR_DIR"):
            return Path(os.environ["HC_WOLOF_ASR_DIR"])
        e = self.entry
        return model_dir(e) if installed(e) else None

    def load(self) -> bool:
        """Load the model if it's downloaded and not loaded yet (speech-recognition thread). False if it isn't there."""
        with self._lock:
            if self.model is not None:
                return True
            folder = self._folder()
            if folder is None:
                return False
            m = KirikuMlx(folder) if self.runtime == "mlx" else OmniOnnx(folder)
            try:
                m.load()
            except Exception as e:
                self.error = f"Wolof speech recognition failed to load: {e}"
                log.exception("Wolof model load failed")
                return False
            self.model, self.error = m, ""
            log.info("Wolof speech recognition ready (%s)", folder.name)
            return True

    def unload(self) -> None:
        """Wolof is no longer spoken: give its memory back (Kiriku is about 1.7 GB)."""
        with self._lock:
            self.model = None

    def transcribe(self, audio: np.ndarray) -> tuple[str, float, float, float]:
        if self.model is None and not self.load():
            raise ModelMissing("Wolof speech recognition isn't downloaded")
        return self.model.transcribe(audio)


class ModelMissing(RuntimeError):
    pass


def cer(ref: str, hyp: str) -> float:
    """Character error rate after lower-casing and dropping punctuation (scripts' quality checks)."""
    import re
    import unicodedata

    def norm(s: str) -> str:
        s = unicodedata.normalize("NFC", s.lower())
        s = "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in s)
        return re.sub(r"\s+", " ", s).strip()

    r, h = norm(ref), norm(hyp)
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / max(1, len(r))
