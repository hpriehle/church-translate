"""Wolof speech-recognition benchmark: WER/CER on FLEURS wo_sn test + speed, and transcripts of long clips.

Standalone (does not import app.*). Engines:
  ct2   a Whisper fine-tune converted to CTranslate2, run with faster-whisper (what the app uses off Apple Silicon)
  omni  Meta Omnilingual ASR (omnilingual-asr package, fairseq2), CTC or LLM model card

FLEURS: download data/wo_sn/test.tsv + audio/test.tar.gz from huggingface.co/datasets/google/fleurs and unpack,
so that <fleurs>/test.tsv and <fleurs>/test/*.wav exist. Utterances are taken in tsv order; --n limits them.

  python scripts/bench/wolof_asr.py ct2 --model /tmp/whosper-ct2 --fleurs /tmp/fleurs --n 100 --threads 4 --out r.json
  python scripts/bench/wolof_asr.py omni --model omniASR_CTC_1B_v2 --fleurs /tmp/fleurs --n 100 --out r.json
  python scripts/bench/wolof_asr.py ct2 --model /tmp/whosper-ct2 --clip sermon.wav --out s.json   # 16 kHz mono wav

Text normalisation for WER: lower case, punctuation removed (Wolof letters like ë, à, ñ, ŋ kept), spaces collapsed.
The reference is the tsv "raw transcription" column normalised the same way.
"""
from __future__ import annotations

import argparse
import json
import re
import resource
import time
import unicodedata
from pathlib import Path

import numpy as np


def norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s.lower())
    s = "".join(" " if unicodedata.category(c)[0] in "PS" else c for c in s)
    return re.sub(r"\s+", " ", s).strip()


def read_wav(p: Path) -> np.ndarray:
    import soundfile as sf  # FLEURS wavs are 32-bit float
    a, sr = sf.read(str(p), dtype="float32")
    assert sr == 16000 and a.ndim == 1, f"{p}: need 16 kHz mono"
    return a


def fleurs_items(root: Path, n: int | None):
    rows = [l.rstrip("\n").split("\t") for l in (root / "test.tsv").read_text().splitlines() if l.strip()]
    items = [(root / "test" / r[1], r[2]) for r in rows]
    return items[:n] if n else items


class Ct2:
    def __init__(self, a):
        from faster_whisper import WhisperModel
        self.m = WhisperModel(a.model, device="cpu", compute_type=a.compute, cpu_threads=a.threads)
        self.a = a

    def __call__(self, audio: np.ndarray) -> str:
        segs, _ = self.m.transcribe(audio, language=self.a.lang, beam_size=self.a.beam, vad_filter=False,
                                    condition_on_previous_text=False, without_timestamps=True)
        return " ".join(s.text.strip() for s in segs)


class Omni:
    def __init__(self, a):
        import torch
        from omnilingual_asr.models.inference.pipeline import ASRInferencePipeline
        # float32: the default bfloat16 is emulated (very slow) on CPUs without AVX512-BF16
        self.p = ASRInferencePipeline(model_card=a.model, device="cpu", dtype=torch.float32)
        self.llm = "LLM" in a.model

    def __call__(self, audio: np.ndarray) -> str:
        inp = [{"waveform": audio, "sample_rate": 16000}]
        kw = {"lang": ["wol_Latn"]} if self.llm else {}
        return self.p.transcribe(inp, batch_size=1, **kw)[0]


def cut(audio: np.ndarray, sec: float):
    """Fixed windows for long clips (Omni CTC/LLM accept < 40 s; Whisper 30 s). No VAD: crude but equal for all."""
    step = int(sec * 16000)
    return [audio[i:i + step] for i in range(0, len(audio), step) if len(audio[i:i + step]) > 8000]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("engine", choices=["ct2", "omni"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--fleurs", type=Path)
    ap.add_argument("--n", type=int)
    ap.add_argument("--clip", type=Path)
    ap.add_argument("--window", type=float, default=20.0, help="seconds per window for --clip")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--compute", default="int8")
    ap.add_argument("--beam", type=int, default=1)
    ap.add_argument("--lang", default=None, help="Whisper language token the fine-tune was trained with")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    if a.engine == "omni":
        import torch
        torch.set_num_threads(a.threads)
    t0 = time.time()
    eng = Ct2(a) if a.engine == "ct2" else Omni(a)
    load_s = time.time() - t0
    res = {"engine": a.engine, "model": a.model, "threads": a.threads, "compute": a.compute, "beam": a.beam,
           "lang": a.lang, "load_s": round(load_s, 1)}

    if a.clip:
        audio = read_wav(a.clip)
        outs, work = [], 0.0
        for i, w in enumerate(cut(audio, a.window)):
            t = time.time()
            txt = eng(w)
            work += time.time() - t
            outs.append({"t": round(i * a.window), "text": txt})
            print(f"[{i * a.window:5.0f}s] {txt}", flush=True)
        res.update(clip=str(a.clip), audio_s=round(len(audio) / 16000, 1), work_s=round(work, 1),
                   rtf=round(work / (len(audio) / 16000), 3), windows=outs)
    else:
        import jiwer
        refs, hyps, per, audio_s, work = [], [], [], 0.0, 0.0
        for p, ref in fleurs_items(a.fleurs, a.n):
            audio = read_wav(p)
            t = time.time()
            hyp = eng(audio)
            dt = time.time() - t
            work += dt
            audio_s += len(audio) / 16000
            refs.append(norm(ref))
            hyps.append(norm(hyp) or "<empty>")
            per.append({"file": p.name, "ref": ref, "hyp": hyp, "s": round(dt, 2)})
            print(f"{len(per):4d} {dt:5.1f}s | {hyp[:90]}", flush=True)
        res.update(n=len(per), audio_s=round(audio_s, 1), work_s=round(work, 1), rtf=round(work / audio_s, 3),
                   wer=round(jiwer.wer(refs, hyps), 4), cer=round(jiwer.cer(refs, hyps), 4), items=per)
        print(f"WER {res['wer']:.3f}  CER {res['cer']:.3f}  RTF {res['rtf']:.3f}  n={len(per)}")
    res["peak_rss_gb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 2)
    a.out.write_text(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
