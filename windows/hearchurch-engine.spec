# PyInstaller spec for the Windows sidecar: dist/hearchurch-engine/hearchurch-engine.exe (onedir).
# Run through windows/build.py, which stages the Windows config.toml first (build/windows-stage).
# Models are not bundled: speech models and voices download at first run, like the Mac app.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

REPO = Path(SPECPATH).parent
STAGE = Path(os.environ.get("HC_WIN_STAGE", REPO / "build" / "windows-stage"))

datas = [
    (str(STAGE / "config.toml"), "."),
    (str(REPO / "web"), "web"),
    (str(REPO / "models" / "silero_vad.onnx"), "models"),
    (str(REPO / "models" / "wolof"), "models/wolof"),
    (str(REPO / "licenses"), "licenses"),
    (str(REPO / "LICENSE"), "."),
    (str(REPO / "THIRD_PARTY_NOTICES.md"), "."),
    (str(REPO / "app" / "wolof_asr.json"), "app"),
    (str(REPO / "app" / "tts" / "g2pw_data"), "app/tts/g2pw_data"),
]
datas += [(str(p), ".") for p in sorted(REPO.glob("glossary*.json"))]
binaries = []
hiddenimports = collect_submodules("app") + collect_submodules("uvicorn")

# Packages with native libraries or data files PyInstaller's own hooks may miss.
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime", "piper", "sounddevice", "_sounddevice_data", "soxr",
            "lameenc", "av", "unicode_rbnf", "sentence_stream", "segno", "certifi", "tokenizers", "huggingface_hub",
            "anthropic", "stripe", "websockets", "httptools", "watchfiles", "hf_xet", "tzdata"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(REPO / "windows" / "engine_entry.py")],
    pathex=[str(REPO)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    # Only the Wolof voice needs these (downloaded with it later, as on the Mac); mlx is Apple Silicon only.
    excludes=["torch", "transformers", "mlx", "mlx_whisper", "tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    # Python's UTF-8 mode: the engine reads and writes its JSON/TOML files without naming an encoding, which on
    # Windows would otherwise be the ANSI code page (cp1252) and break on "Frères" or "Ñ".
    [("X utf8", None, "OPTION")],
    exclude_binaries=True,
    name="hearchurch-engine",
    console=True,   # the Windows app starts it hidden (CREATE_NO_WINDOW) and reads its stdout
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="hearchurch-engine")
