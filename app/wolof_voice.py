"""Where the Wolof voice (SpeechT5, MIT) runs on onnxruntime instead of torch: Windows (docs/windows-engine.md).

The Apple Silicon Mac keeps its torch voice (Hugging Face weights + the downloaded torch library pack). Windows
downloads the same model exported to ONNX (scripts/models/export_speecht5_onnx.py) from our own download host:
wolof_voice.json names the archive and its hash (written by scripts/models/package_model.py --runtime voice), and
it is installed like Wolof speech recognition (wolof_asr.install: checked, unpacked all at once).

Elsewhere (Linux, an Intel Mac) HEARCHURCH_WOLOF_VOICE=onnx picks the ONNX voice too, for tests and trials.
The non-commercial MMS Wolof voice is never used where this is on (config.tts_for, tts.engines.make_tts).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import wolof_asr

MANIFEST = Path(__file__).with_name("wolof_voice.json")


def onnx_voice() -> bool:
    """Whether this computer's Wolof voice is the ONNX one."""
    return sys.platform == "win32" or os.environ.get("HEARCHURCH_WOLOF_VOICE", "") == "onnx"


def manifest() -> dict[str, Any] | None:
    """The published ONNX voice {"id", "url", "sha256", "size", "name", ...}, or None before it is."""
    try:
        entry = json.loads(MANIFEST.read_text(encoding="utf-8")).get("onnx")
    except FileNotFoundError:
        return None
    return entry if entry and entry.get("sha256") else None


def model_dir() -> Path | None:
    """The installed ONNX voice's folder, or None if it isn't installed."""
    entry = manifest()
    return wolof_asr.model_dir(entry) if wolof_asr.installed(entry) else None
