"""How fast the ONNX Wolof voice speaks on this machine (docs/windows-engine.md, "The Wolof voice").

  uv run python scripts/wolof_voice_bench.py <the voice's folder> [--passes 5] [--out bench.json]

Speaks the Wolof test sentences (scripts/models/export_speecht5_onnx.py) as the engine does, a caption of two
sentences at a time (app/tts/engines.py SpeechT5OnnxTts: stop at 0.4, at most 6 frames per character, the vocoder
on one sentence while the decoder writes the next), --passes times over (5: about four minutes of speech), with
both voices. Prints the real-time factor (seconds of work per second of speech; under 1 keeps up, the target is
under 0.5) and the time per caption.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "models"))

from app.tts.speecht5_onnx import SpeechT5Onnx  # noqa: E402
from export_speecht5_onnx import SENTENCES  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--passes", type=int, default=5)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()

    t0 = time.perf_counter()
    voice = SpeechT5Onnx(a.folder)
    load_s = time.perf_counter() - t0
    speakers = {v: np.load(ROOT / "models" / "wolof" / f"speaker-{v}.npy") for v in ("male", "female")}
    captions = [SENTENCES[i:i + 2] for i in range(0, len(SENTENCES), 2)]
    voice.speak_all(captions[0][:1], speakers["male"], threshold=0.4, maxlenratio=6.0)   # warm up
    took, audio, per_caption = 0.0, 0.0, []
    rng = np.random.default_rng(0)
    for n in range(a.passes):
        spk = speakers["male" if n % 2 == 0 else "female"]
        for cap in captions:
            t = time.perf_counter()
            waves = voice.speak_all(cap, spk, threshold=0.4, maxlenratio=6.0, rng=rng)
            dt = time.perf_counter() - t
            secs = sum(len(w) for w in waves) / voice.sr
            took += dt
            audio += secs
            per_caption.append((dt, secs))
    rtfs = sorted(dt / secs for dt, secs in per_caption)
    times = sorted(dt for dt, _ in per_caption)
    result = {
        "machine": {"os": sys.platform, "cpus": os.cpu_count(), "cpu": platform.processor()},
        "model": json.loads((a.folder / "config.json").read_text(encoding="utf-8")).get("decoder"),
        "load_s": round(load_s, 2), "captions": len(per_caption), "speech_s": round(audio, 1),
        "work_s": round(took, 1), "rtf": round(took / audio, 3),
        "rtf_caption_p50": round(rtfs[len(rtfs) // 2], 3), "rtf_caption_max": round(rtfs[-1], 3),
        "caption_s_p50": round(times[len(times) // 2], 2), "caption_s_p95": round(times[int(len(times) * .95)], 2),
    }
    print(json.dumps(result, indent=1))
    print(f"WOLOF VOICE  RTF {result['rtf']} over {result['speech_s']} s of speech "
          f"({result['captions']} captions; per caption p50 {result['caption_s_p50']} s)", flush=True)
    if a.out:
        a.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
