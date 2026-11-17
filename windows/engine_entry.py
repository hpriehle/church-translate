"""hearchurch-engine.exe: the Hear Church engine (python -m app) frozen by PyInstaller (windows/build.py).

    hearchurch-engine.exe --app --port 8000      what the Windows app runs (same flags as `python -m app`)
    hearchurch-engine.exe --self-check           imports everything the engine needs, prints JSON, exits 0/1
"""
import multiprocessing
import sys


def self_check() -> int:
    import importlib
    import json

    out: dict = {"utf8_mode": sys.flags.utf8_mode, "frozen": bool(getattr(sys, "frozen", False))}
    failed = []
    for mod in ("app.main", "faster_whisper", "ctranslate2", "onnxruntime", "piper", "sounddevice", "soxr",
                "lameenc", "av", "anthropic", "huggingface_hub", "uvicorn", "segno", "psutil", "stripe"):
        try:
            importlib.import_module(mod)
        except Exception as e:   # noqa: BLE001  (report every missing piece, not the first)
            failed.append(f"{mod}: {type(e).__name__}: {e}")
    from app import config

    out["root"], out["home"] = str(config.ROOT), str(config.HOME)
    out["shipped"] = {p: (config.ROOT / p).exists() for p in (
        "config.toml", "glossary.json", "web/operator", "models/silero_vad.onnx", "app/wolof_asr.json",
        "licenses", "THIRD_PARTY_NOTICES.md")}
    try:
        import ctranslate2
        import onnxruntime
        import sounddevice

        out["cuda_devices"] = ctranslate2.get_cuda_device_count()
        out["onnx_providers"] = onnxruntime.get_available_providers()
        out["input_devices"] = sum(1 for d in sounddevice.query_devices() if d["max_input_channels"] > 0)
        out["host_apis"] = [a["name"] for a in sounddevice.query_hostapis()]
        from app.stt import resolve_engine

        out["stt_engine"] = resolve_engine("auto")
        out["stt_model"] = config.load_settings().stt.faster_whisper_model
    except Exception as e:   # noqa: BLE001
        failed.append(f"probe: {type(e).__name__}: {e}")
    out["failed"] = failed
    ok = not failed and out["utf8_mode"] == 1 and all(out["shipped"].values())
    out["ok"] = ok
    print(json.dumps(out, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if "--self-check" in sys.argv:
        sys.exit(self_check())
    from app.__main__ import main

    main()
