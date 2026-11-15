"""Streaming speech-recognition benchmark for a CPU server (faster-whisper), using the engine's own code.

The audio is cut by the engine's Chunker (Silero VAD, config.toml [vad] defaults) and each chunk goes through
Transcriber.transcribe with the same prompt the pipeline builds (tail of the previous chunk). One worker per
meeting, chunks in order, like the engine.

Two clocks:
  --clock virtual  a chunk is "available" at its cut position in the audio; the worker starts it at
                   max(available, previous done). Exact for one meeting alone (an idle worker uses no CPU),
                   and runs faster than real time when the model keeps up.
  --clock real     the worker sleeps until the chunk would have been cut in a live meeting. Use this when
                   several meetings run at once, so they compete for the CPU the way live ones would.

Lag = when the text is ready minus when the chunk was cut (what the listener waits on top of the chunk length).

  python scripts/bench/stt_stream.py --audio ru.wav --lang ru --model large-v3-turbo --threads 8 --out r.json
Audio: 16 kHz mono wav. Run from the project root (imports app.*).
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import stt as stt_mod  # noqa: E402
from app.chunker import FRAME, SR, Chunker  # noqa: E402
from app.config import SttCfg, VadCfg  # noqa: E402


def read_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        assert w.getframerate() == SR and w.getnchannels() == 1, "need 16 kHz mono"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def cut(audio: np.ndarray) -> list[tuple[float, np.ndarray]]:
    """(cut position in seconds, chunk audio), as the live chunker would emit them."""
    ch = Chunker(VadCfg())
    out = []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        pos = (i + FRAME) / SR
        for c in ch.feed(audio[i:i + FRAME], pos):
            out.append((pos, c.audio))
    out += [(len(audio) / SR, c.audio) for c in ch.flush()]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--lang", default="ru")
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--clock", choices=["virtual", "real"], default="virtual")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    # Same loader as the engine, with the thread count pinned; no step-down (we measure one model at a time).
    from faster_whisper import WhisperModel, download_model

    stt_mod.STEP_DOWN_RTF = 1e9
    stt_mod.Transcriber._fw_load = staticmethod(lambda name, local_only: WhisperModel(
        download_model(name, local_files_only=local_only), device="cpu", compute_type="int8",
        cpu_threads=a.threads))
    tr = stt_mod.Transcriber(SttCfg(engine="faster-whisper", faster_whisper_model=a.model, language=a.lang,
                                    languages=[a.lang]))
    tr.load()

    audio = read_wav(a.audio)
    chunks = cut(audio)
    r0 = resource.getrusage(resource.RUSAGE_SELF)
    w0 = time.monotonic()
    done_v = 0.0
    prev = ""
    rows = []
    for pos, ca in chunks:
        if a.clock == "real":
            wait = w0 + pos - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            start = time.monotonic() - w0
        else:
            start = max(pos, done_v)
        t = time.monotonic()
        res = tr.transcribe(ca, stt_mod.build_prompt([], prev, a.lang), a.lang)
        work = time.monotonic() - t
        done = (time.monotonic() - w0) if a.clock == "real" else start + work
        done_v = done
        if res.text:
            prev = res.text
        rows.append({"cut_s": round(pos, 2), "len_s": round(len(ca) / SR, 2), "work_s": round(work, 3),
                     "lag_s": round(done - pos, 2), "text": res.text, "dropped": res.dropped_reason})
        print(f"{pos:7.1f}s len {len(ca) / SR:4.1f} work {work:5.2f} lag {done - pos:5.1f} | {res.text[:90]}",
              flush=True)
    wall = time.monotonic() - w0
    r1 = resource.getrusage(resource.RUSAGE_SELF)
    cpu = (r1.ru_utime - r0.ru_utime) + (r1.ru_stime - r0.ru_stime)
    audio_s = len(audio) / SR
    work = sum(r["work_s"] for r in rows)
    lags = [r["lag_s"] for r in rows]
    last_min = [r["lag_s"] for r in rows if r["cut_s"] >= audio_s - 60] or lags[-1:]
    summary = {
        "model": a.model, "threads": a.threads, "lang": a.lang, "clock": a.clock, "audio_s": round(audio_s, 1),
        "chunks": len(rows), "mean_chunk_s": round(float(np.mean([r["len_s"] for r in rows])), 2),
        "rtf": round(work / audio_s, 3), "lag_p50": round(float(np.median(lags)), 2),
        "lag_p90": round(float(np.percentile(lags, 90)), 2), "lag_max": round(max(lags), 2),
        "lag_last_min": round(float(np.mean(last_min)), 2),
        # CPU while working, in cores (100% = one core); and averaged over the meeting
        "cpu_cores_busy": round(cpu / work, 2) if work else 0,
        "cpu_cores_avg": round(cpu / max(wall if a.clock == "real" else audio_s, 1e-9), 2),
        "peak_rss_mb": round(r1.ru_maxrss / 1024), "load_s": round(tr.load_s, 1),
    }
    print(json.dumps(summary))
    Path(a.out).write_text(json.dumps({"summary": summary, "chunks": rows}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
