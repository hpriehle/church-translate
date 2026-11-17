"""Build the Windows sidecar: dist/hearchurch-engine/hearchurch-engine.exe (PyInstaller onedir).

    uv sync --frozen --extra linux && uv pip install "pyinstaller>=6.10,<7"
    uv run --no-sync python windows/build.py

The config.toml it ships is the repo's with the Windows defaults, like the Intel Mac build (mac/build.sh,
HC_ARCH=intel): the multilingual CPU model "small" (steps down to "base" on a slow PC), and French as the default
translation (no Wolof voice by default: see docs/windows-engine.md).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STAGE = REPO / "build" / "windows-stage"


def windows_config(text: str) -> str:
    text, n1 = re.subn(r"(?m)^faster_whisper_model = .*$", 'faster_whisper_model = "small"', text)
    text, n2 = re.subn(r"(?m)^languages = \[.*\]$", 'languages = ["fr"]', text, count=1)
    if (n1, n2) != (1, 1):
        sys.exit("windows/build.py: config.toml no longer has faster_whisper_model / languages lines to set")
    return text


def main() -> int:
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)
    (STAGE / "config.toml").write_text(windows_config((REPO / "config.toml").read_text(encoding="utf-8")),
                                       encoding="utf-8")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
           "--distpath", str(REPO / "dist"), "--workpath", str(REPO / "build" / "pyinstaller"),
           str(REPO / "windows" / "hearchurch-engine.spec")]
    print("==", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=REPO, env={**__import__("os").environ, "HC_WIN_STAGE": str(STAGE)})


if __name__ == "__main__":
    sys.exit(main())
