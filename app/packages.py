"""Models a meeting needs on this computer, and downloading the missing ones (the Mac app).

Speech recognition and the Wolof voice come from the Hugging Face cache (~/.cache/huggingface); the Piper
voices (French, Spanish, Russian) go to PIPER_DIR. Each package reports its size and, while downloading, the bytes on disk so far.
Phase 4 adds hashes, more languages and our own download host.

The Wolof voice also needs torch and transformers (about 800 MB), which the Mac app leaves out and fetches
from hearchurch.com with that voice: wolof_libs.json in the app names the signed archive and its hash
(mac/wolof_libs.sh builds it). From a checkout there is no such file: the libraries are in the virtualenv.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import os
import shutil
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import HOME, PIPER_DIR, ROOT

log = logging.getLogger("packages")

PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/{family}/{region}/{name}/{quality}/{voice}.{ext}"
# Only voices whose licence allows a paid service: not ru_RU-irina (unknown) or ru_RU-ruslan (non-commercial).
PIPER_VOICES = {
    "fr": ("French voices", ("fr_FR-tom-medium", "fr_FR-siwis-medium")),
    "es": ("Spanish voices", ("es_MX-ald-medium", "es_MX-claude-high")),
    "ru": ("Russian voices", ("ru_RU-denis-medium", "ru_RU-dmitri-medium")),
    "pt": ("Portuguese voices", ("pt_BR-cadu-medium", "pt_BR-faber-medium")),
    "uk": ("Ukrainian voices", ("uk_UA-tetiana-high", "uk_UA-oleksa-high")),
    "fa": ("Persian voices", ("fa_IR-amir-medium", "fa_IR-ganji-medium")),
    "ne": ("Nepali voice", ("ne_NP-chitwan-medium",)),
    "vi": ("Vietnamese voice", ("vi_VN-vais1000-medium",)),
    "zh": ("Chinese voice", ("zh_CN-chaowen-medium",)),
    "en": ("English voices", ("en_US-joe-medium", "en_US-kristin-medium")),  # when the speaker isn't speaking English
}
WOLOF_REPOS = ("bilalfaye/speecht5_tts-wolof", "microsoft/speecht5_hifigan")
# What Piper's Chinese voice needs beyond the voice itself. Piper would fetch these on the first sentence, into the
# working directory (inside the signed app), so they come with the voice. Pinned because Piper runs the config.py inside.
G2PW_URL = ("https://huggingface.co/datasets/rhasspy/piper-checkpoints/resolve/"
            "8d418f0288a50aed603547ee751193fa7910bfe3/zh/zh_CN/_resources/g2pw.tar.gz")
G2PW_SHA256 = "252c6f561c11014fbb243d70aec993a816d82109887b91edcda0261ab9967060"
G2PW_BYTES = 112_991_734
G2PW_DIR = PIPER_DIR / "g2pW"   # where PiperVoice.load(download_dir=PIPER_DIR) looks
G2PW_TABLES = Path(__file__).parent / "tts" / "g2pw_data"   # from the g2pW package, which would pull in transformers
BERT_TOKENIZER = "bert-base-chinese"
BERT_TOKENIZER_FILES = ("vocab.txt", "tokenizer_config.json", "config.json", "tokenizer.json")
LIBS_USERS = ("wo", "zh")       # these two share the downloaded libraries (the tokenizer needs transformers)
# Download sizes in MB (measured), shown before a download starts and used for its progress.
# The speech figure is the mlx model; Intel Macs use the smaller CPU model (FASTER_WHISPER_MB).
FASTER_WHISPER_MB = 486
SIZES_MB = {"speech": 1615, "fr": 127, "wo": 634, "es": 127, "ru": 127, "pt": 126, "uk": 228, "fa": 127, "ne": 63, "vi": 63, "zh": 176, "en": 127}
LIBS_MANIFEST = ROOT / "wolof_libs.json"
LIBS_DIR = HOME / "packages"


def _libs() -> dict[str, Any] | None:
    """The Wolof libraries this version of the app downloads, or None if it has them already."""
    try:
        return json.loads(LIBS_MANIFEST.read_text())
    except FileNotFoundError:
        return None


def activate_libs() -> None:
    """Make downloaded libraries importable. They go last: what the app ships always wins."""
    libs = _libs()
    if libs:
        path = str(LIBS_DIR / libs["id"])
        if os.path.isdir(path) and path not in sys.path:
            sys.path.append(path)
            importlib.invalidate_caches()


@dataclass
class Package:
    id: str
    name: str
    size_mb: int
    hf_repos: tuple[str, ...] = ()
    hf_only: tuple[str, ...] = ()   # just these files of hf_repos, when the rest isn't needed
    g2pw: bool = False
    piper_voices: tuple[str, ...] = ()
    libs: dict[str, Any] | None = None
    state: str = "unknown"       # "ready" | "missing" | "downloading" | "failed"
    error: str = ""
    done_bytes: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        total = self.size_mb * 1_000_000
        done = total if self.state == "ready" else min(self.done_bytes, int(total * 0.99))
        return {"id": self.id, "name": self.name, "state": self.state, "error": self.error,
                "size_mb": self.size_mb, "done_mb": round(done / 1e6, 1)}


def _hf_cached(repo: str, only: tuple[str, ...] = ()) -> bool:
    from huggingface_hub import snapshot_download

    try:
        snapshot_download(repo, local_files_only=True, allow_patterns=list(only) or None)
        return True
    except Exception:
        return False


def _hf_dir(repo: str) -> Path:
    from huggingface_hub.constants import HF_HUB_CACHE

    return Path(HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))


def _bytes_in(path: Path) -> int:
    """Bytes of the real files under a folder (links not followed, so nothing counts twice)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            p = os.path.join(root, f)
            if not os.path.islink(p):
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
    return total


def _piper_files(voice: str) -> list[tuple[str, Path]]:
    region, name, quality = voice.split("-")   # fr_FR-tom-medium
    return [(PIPER_URL.format(family=region[:2], region=region, name=name, quality=quality, voice=voice, ext=ext),
             PIPER_DIR / f"{voice}.{ext}") for ext in ("onnx", "onnx.json")]


def package_for(code: str, stt_model: str) -> Package | None:
    if code == "speech":
        size = FASTER_WHISPER_MB if "faster-whisper" in stt_model else SIZES_MB["speech"]
        return Package("speech", "Speech recognition", size, hf_repos=(stt_model,))
    if code == "zh":
        name, voices = PIPER_VOICES[code]
        libs = _libs()
        size = SIZES_MB[code] + (round(libs["size"] / 1e6) if libs else 0)
        return Package(code, name, size, piper_voices=voices, hf_repos=(BERT_TOKENIZER,),
                       hf_only=BERT_TOKENIZER_FILES, g2pw=True, libs=libs)
    if code in PIPER_VOICES:
        name, voices = PIPER_VOICES[code]
        return Package(code, name, SIZES_MB[code], piper_voices=voices)
    if code == "wo":
        libs = _libs()
        size = SIZES_MB["wo"] + (round(libs["size"] / 1e6) if libs else 0)
        return Package("wo", "Wolof voice", size, hf_repos=WOLOF_REPOS, libs=libs)
    return None


class Packages:
    def __init__(self, stt_model: str = "mlx-community/whisper-large-v3-turbo") -> None:
        self.stt_model = stt_model
        self.items: dict[str, Package] = {}
        self._libs_lock = asyncio.Lock()   # Wolof and Chinese can start at once and share one archive
        self.on_ready: Callable[[], None] | None = None   # e.g. load the voices that were missing

    def wanted(self, languages: list[str]) -> list[Package]:
        """Speech recognition (always needed) plus a package per language that has one."""
        out = []
        for code in ["speech", *languages]:
            if code not in self.items:
                p = package_for(code, self.stt_model)
                if p is None:
                    continue
                self.items[code] = p
            out.append(self.items[code])
        return out

    async def status(self, languages: list[str]) -> list[dict[str, Any]]:
        for p in self.wanted(languages):
            if p.state == "downloading":
                p.done_bytes = await asyncio.to_thread(self._done_bytes, p)
            else:
                p.state = "ready" if await asyncio.to_thread(self._present, p) else ("failed" if p.error else "missing")
        return [p.public() for p in self.wanted(languages)]

    @staticmethod
    def _present(p: Package) -> bool:
        if not all(_hf_cached(r, p.hf_only) for r in p.hf_repos):
            return False
        if p.libs and not (LIBS_DIR / p.libs["id"]).is_dir():
            return False
        if p.g2pw and not (G2PW_DIR / "g2pw.onnx").exists():
            return False
        return all(dest.exists() for v in p.piper_voices for _, dest in _piper_files(v))

    @staticmethod
    def _done_bytes(p: Package) -> int:
        n = sum(_bytes_in(_hf_dir(r)) for r in p.hf_repos)
        if p.libs:
            archive = LIBS_DIR / f'{p.libs["id"]}.tar.gz'
            part = archive.with_name(archive.name + ".part")
            if part.exists():
                n += part.stat().st_size
            elif archive.exists() or (LIBS_DIR / p.libs["id"]).is_dir():
                n += p.libs["size"]
        if p.g2pw:
            part = PIPER_DIR / "g2pw.tar.gz.part"
            if part.exists():
                n += part.stat().st_size
            elif (PIPER_DIR / "g2pw.tar.gz").exists() or (G2PW_DIR / "g2pw.onnx").exists():
                n += G2PW_BYTES
        for v in p.piper_voices:
            for _, dest in _piper_files(v):
                for f in (dest, dest.with_name(dest.name + ".part")):
                    if f.exists():
                        n += f.stat().st_size
        return n

    async def remove(self, code: str) -> None:
        """Delete one language's downloaded voice. Speech recognition is needed for every language, so it stays."""
        p = self.items.get(code) or package_for(code, self.stt_model)
        if p is None or code == "speech":
            raise ValueError(f"{code} can't be removed")
        if p.state == "downloading":
            raise ValueError(f"{p.name} is still downloading: wait for it to finish, then remove it.")
        self.items[code] = p
        await asyncio.to_thread(self._delete, p)
        p.state, p.error, p.done_bytes = "missing", "", 0
        log.info("removed %s", p.name)

    @staticmethod
    def _delete(p: Package) -> None:
        for repo in p.hf_repos:
            shutil.rmtree(_hf_dir(repo), ignore_errors=True)
        if p.g2pw:
            shutil.rmtree(G2PW_DIR, ignore_errors=True)
            for name in ("g2pw.tar.gz", "g2pw.tar.gz.part"):
                (PIPER_DIR / name).unlink(missing_ok=True)
        if p.libs and not _libs_used_by_another(p):
            shutil.rmtree(LIBS_DIR / p.libs["id"], ignore_errors=True)
            (LIBS_DIR / f'{p.libs["id"]}.tar.gz').unlink(missing_ok=True)
        for v in p.piper_voices:
            for _, dest in _piper_files(v):
                dest.unlink(missing_ok=True)
                dest.with_name(dest.name + ".part").unlink(missing_ok=True)

    def download_missing(self, languages: list[str]) -> None:
        for p in self.wanted(languages):
            if p.state in ("missing", "failed") and (p.task is None or p.task.done()):
                p.state, p.error, p.done_bytes = "downloading", "", 0
                p.task = asyncio.create_task(self._download(p))

    async def _download(self, p: Package) -> None:
        from huggingface_hub import snapshot_download

        try:
            if p.libs:
                async with self._libs_lock:
                    if not (LIBS_DIR / p.libs["id"]).is_dir():
                        archive = LIBS_DIR / f'{p.libs["id"]}.tar.gz'
                        await _fetch(p.libs["url"], archive)
                        await asyncio.to_thread(_unpack_libs, archive, p.libs)
            for repo in p.hf_repos:
                await asyncio.to_thread(snapshot_download, repo, allow_patterns=list(p.hf_only) or None)
            if p.g2pw and not (G2PW_DIR / "g2pw.onnx").exists():
                archive = PIPER_DIR / "g2pw.tar.gz"
                await _fetch(G2PW_URL, archive)
                await asyncio.to_thread(_unpack_g2pw, archive)
            for v in p.piper_voices:
                for url, dest in _piper_files(v):
                    if not dest.exists():
                        await _fetch(url, dest)
            p.state = "ready"
            log.info("downloaded %s", p.name)
            if self.on_ready:
                self.on_ready()
        except Exception as e:  # try again later: the Hugging Face cache keeps what already arrived
            p.state, p.error = "failed", str(e)[:200] or type(e).__name__
            log.warning("download of %s failed: %s", p.name, p.error)


async def _fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):
                    f.write(chunk)
    part.replace(dest)


def _libs_used_by_another(p: Package) -> bool:
    """Whether another language's installed voice still needs the libraries p would delete."""
    others = (package_for(code, "") for code in LIBS_USERS if code != p.id)
    return any(o and Packages._present(o) for o in others)


def _unpack_g2pw(archive: Path) -> None:
    """Check the archive against the pinned hash and unpack it, so a half-finished unpack never looks installed."""
    digest = hashlib.sha256()
    with open(archive, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != G2PW_SHA256:
        archive.unlink(missing_ok=True)
        raise RuntimeError("the Chinese pronunciation model didn't match its checksum")
    tmp = G2PW_DIR.with_name("g2pW.part")
    shutil.rmtree(tmp, ignore_errors=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(tmp, filter="data")
    for table in G2PW_TABLES.iterdir():
        if table.name != "LICENCE":
            shutil.copy(table, tmp / table.name)
    shutil.rmtree(G2PW_DIR, ignore_errors=True)
    tmp.replace(G2PW_DIR)
    archive.unlink(missing_ok=True)


def _unpack_libs(archive: Path, libs: dict[str, Any]) -> None:
    """Check the archive against the hash the signed app carries, unpack it, and drop any older version."""
    digest = hashlib.sha256()
    with open(archive, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    if digest.hexdigest() != libs["sha256"]:
        archive.unlink()
        raise RuntimeError("the Wolof libraries download was damaged")
    work = LIBS_DIR / f'{libs["id"]}.unpacking'
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    (work / libs["id"]).rename(LIBS_DIR / libs["id"])   # all at once: a partial folder never counts as installed
    shutil.rmtree(work)
    archive.unlink()
    for old in LIBS_DIR.glob("wolof-libs-*"):
        if old.is_dir() and old.name != libs["id"]:
            shutil.rmtree(old, ignore_errors=True)
    activate_libs()


PACKAGES = Packages()
