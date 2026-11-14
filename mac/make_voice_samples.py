#!/usr/bin/env python3
"""Records every voice saying its language's test sentence, for the Listen buttons in Settings → Languages.

The recordings are built into the app so a voice can be heard before it is downloaded. Run this after adding a
voice to app/tts/engines.py, from the repo root, naming the languages to record (all of them if none):

    .venv/bin/python mac/make_voice_samples.py [folder with the Piper voices] [en lv ...]

On a Mac it encodes with afconvert; elsewhere with ffmpeg. tests/test_languages.py fails while a voice has no recording.
"""
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import packages as P  # noqa: E402
from app.config import Settings, tts_for  # noqa: E402
from app.languages import LANGUAGES  # noqa: E402
from app.tts import engines as E  # noqa: E402
from app.tts.normalize import speechify  # noqa: E402

OUT = ROOT / "mac" / "HearChurch" / "Resources" / "VoiceSamples"
RATE = 44100


def main() -> None:
    args = sys.argv[1:]
    if args and Path(args[0]).is_dir():
        P.PIPER_DIR = E.PIPER_DIR = Path(args.pop(0))
        P.G2PW_DIR = P.PIPER_DIR / "g2pW"
    OUT.mkdir(parents=True, exist_ok=True)
    catalog = E.voice_catalog()
    settings = Settings()
    for code in args or catalog:
        for option in catalog[code]:
            cfg = tts_for(settings, code).model_copy(update={"engine": option["engine"], "voice": option["voice"]})
            pcm = E.make_tts(cfg, RATE).synth(speechify(code, LANGUAGES[code].test_sentence))
            target = OUT / f"sample-{code}-{option['voice']}.m4a"
            with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
                with wave.open(tmp.name, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(RATE)
                    w.writeframes(pcm.astype("int16").tobytes())
                if shutil.which("afconvert"):
                    cmd = ["afconvert", "-f", "m4af", "-d", "aac", "-b", "64000", tmp.name, str(target)]
                else:
                    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", tmp.name, "-c:a", "aac", "-b:a", "64k", str(target)]
                subprocess.run(cmd, check=True)
            print(f"{option['label']:22s} {code}  {len(pcm) / RATE:4.1f} s  {target.stat().st_size // 1024} KB", flush=True)


if __name__ == "__main__":
    main()
