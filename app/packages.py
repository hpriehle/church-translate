"""Models a meeting needs on this computer, and downloading the missing ones (the Mac app's preflight).

Speech recognition and the Wolof voice come from the Hugging Face cache (~/.cache/huggingface); the French
Piper voices go to PIPER_DIR. Phase 4 adds sizes, hashes, more languages and our own download host.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .config import PIPER_DIR, Settings

log = logging.getLogger("packages")

PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/{v}/medium/fr_FR-{v}-medium.{ext}"
FRENCH_VOICES = ("tom", "siwis")
WOLOF_REPOS = ("bilalfaye/speecht5_tts-wolof", "microsoft/speecht5_hifigan")


@dataclass
class Package:
    id: str
    name: str
    hf_repos: tuple[str, ...] = ()
    piper_voices: tuple[str, ...] = ()
    state: str = "unknown"       # "ready" | "missing" | "downloading" | "failed"
    error: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "state": self.state, "error": self.error}


def _hf_cached(repo: str) -> bool:
    from huggingface_hub import snapshot_download

    try:
        snapshot_download(repo, local_files_only=True)
        return True
    except Exception:
        return False


def _piper_files(voice: str) -> list[tuple[str, Any]]:
    return [(PIPER_URL.format(v=voice, ext=ext), PIPER_DIR / f"fr_FR-{voice}-medium.{ext}") for ext in ("onnx", "onnx.json")]


class Packages:
    def __init__(self) -> None:
        self.items: dict[str, Package] = {}
        self.on_ready: Callable[[], None] | None = None   # e.g. load the voices that were missing

    def wanted(self, settings: Settings) -> list[Package]:
        """The packages for the languages switched on (speech recognition is always needed)."""
        want = [Package("speech", "Speech recognition", hf_repos=(settings.stt.model,))]
        if "fr" in settings.languages:
            want.append(Package("fr", "French voices", piper_voices=FRENCH_VOICES))
        if "wo" in settings.languages:
            want.append(Package("wo", "Wolof voice", hf_repos=WOLOF_REPOS))
        out = []
        for p in want:
            known = self.items.get(p.id)
            if known is None or known.hf_repos != p.hf_repos:
                self.items[p.id] = known = p
            out.append(known)
        return out

    async def status(self, settings: Settings) -> list[dict[str, Any]]:
        for p in self.wanted(settings):
            if p.state != "downloading":
                p.state = "ready" if await asyncio.to_thread(self._present, p) else ("failed" if p.error else "missing")
        return [p.public() for p in self.wanted(settings)]

    @staticmethod
    def _present(p: Package) -> bool:
        if not all(_hf_cached(r) for r in p.hf_repos):
            return False
        return all(dest.exists() for v in p.piper_voices for _, dest in _piper_files(v))

    def download_missing(self, settings: Settings) -> None:
        for p in self.wanted(settings):
            if p.state in ("missing", "failed") and (p.task is None or p.task.done()):
                p.state, p.error = "downloading", ""
                p.task = asyncio.create_task(self._download(p))

    async def _download(self, p: Package) -> None:
        from huggingface_hub import snapshot_download

        try:
            for repo in p.hf_repos:
                await asyncio.to_thread(snapshot_download, repo)
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


async def _fetch(url: str, dest) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 16):
                    f.write(chunk)
    part.replace(dest)


PACKAGES = Packages()
