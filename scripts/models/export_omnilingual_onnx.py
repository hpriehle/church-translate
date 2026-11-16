"""Export Meta Omnilingual ASR CTC (default omniASR_CTC_300M_v2) to ONNX, for Wolof speech recognition on computers
without Apple Silicon (Intel Macs, Windows, Linux): the app runs it with onnxruntime, no torch or fairseq2.

Needs a Python with omnilingual-asr (fairseq2) and onnx, e.g. the bench venv:

  uv venv /tmp/omni && uv pip install --python /tmp/omni omnilingual-asr onnx onnxruntime
  /tmp/omni/bin/python -I scripts/models/export_omnilingual_onnx.py --out /tmp/wo-models/omni300m
  /tmp/omni/bin/python -I scripts/models/export_omnilingual_onnx.py --out /tmp/wo-models/omni300m --check clip.wav

Writes into --out:
  model.onnx     float32 waveform [1, samples] -> logits [1, frames, vocab]; the waveform is normalised inside
                 (layer norm over the whole clip, as the omnilingual pipeline does)
  model.int8.onnx  the same with int8 weights (onnxruntime dynamic quantisation), with --int8
  tokens.json    {"pieces": [...SentencePiece pieces by id...], "skip": [special ids]} for greedy CTC decoding
  config.json    {"kind": "omnilingual-ctc", "model_card": ..., "sample_rate": 16000, "file": "model.onnx"}
  LICENSE, NOTICE  Apache-2.0 notice for the weights

Then package it with scripts/models/package_model.py. --check compares the ONNX transcript of a 16 kHz mono wav
with the omnilingual pipeline's own (they should be identical) and prints both runtimes' speed.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

NOTICE = """Wolof speech recognition (computers without Apple Silicon):
Meta Omnilingual ASR, {card}, converted to ONNX for Hear Church.
Copyright (c) Meta Platforms, Inc. and affiliates. Licensed under the Apache License, Version 2.0
https://github.com/facebookresearch/omnilingual-asr
"""


def load(card: str):
    import omnilingual_asr.models.inference.pipeline  # noqa: F401  (registers the model cards)
    import torch
    from fairseq2.data.tokenizers.hub import load_tokenizer
    from fairseq2.models.hub import load_model

    model = load_model(card, device=torch.device("cpu"), dtype=torch.float32).eval()
    return model, load_tokenizer(card)


class Wrapped:
    """waveform [1, T] -> logits [1, F, V], with the pipeline's normalisation inside."""

    def __new__(cls, model):
        import torch
        from fairseq2.nn.batch_layout import BatchLayout

        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.m = model

            def forward(self, wave):
                # = layer_norm(wave, wave.shape) without affine (eps 1e-5), written out so the length stays dynamic
                mean = wave.mean(dim=-1, keepdim=True)
                var = ((wave - mean) ** 2).mean(dim=-1, keepdim=True)
                wave = (wave - mean) / torch.sqrt(var + 1e-5)
                layout = BatchLayout(wave.shape, seq_lens=None, device=wave.device)
                logits, _ = self.m(wave, layout)
                return logits

        return M().eval()


def ctc_decode(ids, pieces: list[str], skip: set[int]) -> str:
    out, prev = [], -1
    for i in ids:
        i = int(i)
        if i != prev and i not in skip:
            out.append(pieces[i])
        prev = i
    return "".join(out).replace("▁", " ").strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", default="omniASR_CTC_300M_v2")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--int8", action="store_true", help="also write model.int8.onnx")
    ap.add_argument("--check", type=Path, help="16 kHz mono wav to compare the two runtimes on")
    a = ap.parse_args()
    import torch

    a.out.mkdir(parents=True, exist_ok=True)
    model, tok = load(a.card)
    sp = tok._model
    pieces = [sp.index_to_token(i) for i in range(sp.vocabulary_size)]
    vi = tok.vocab_info
    skip = sorted({i for i in (vi.bos_idx, vi.pad_idx, vi.eos_idx, vi.unk_idx) if i is not None})
    (a.out / "tokens.json").write_text(json.dumps({"pieces": pieces, "skip": skip}, ensure_ascii=False))

    w = Wrapped(model)
    dummy = torch.randn(1, 16000 * 4)
    onnx_path = a.out / "model.onnx"
    t0 = time.time()
    torch.onnx.export(w, (dummy,), str(onnx_path), input_names=["waveform"], output_names=["logits"],
                      dynamic_axes={"waveform": {1: "samples"}, "logits": {1: "frames"}}, opset_version=17,
                      dynamo=False, external_data=False)
    print(f"exported {onnx_path} ({onnx_path.stat().st_size / 1e6:.0f} MB) in {time.time() - t0:.0f} s")
    files = {"file": "model.onnx"}
    if a.int8:
        from onnxruntime.quantization import QuantType, quantize_dynamic

        q = a.out / "model.int8.onnx"
        quantize_dynamic(str(onnx_path), str(q), weight_type=QuantType.QInt8)
        print(f"int8: {q} ({q.stat().st_size / 1e6:.0f} MB)")
    (a.out / "config.json").write_text(json.dumps({"kind": "omnilingual-ctc", "model_card": a.card,
                                                   "sample_rate": 16000, **files}, indent=1))
    (a.out / "NOTICE").write_text(NOTICE.format(card=a.card))
    (a.out / "LICENSE").write_text((Path(__file__).resolve().parents[2] / "licenses" / "Apache-2.0.txt").read_text())

    if a.check:
        import onnxruntime as ort
        import soundfile as sf

        audio, sr = sf.read(str(a.check), dtype="float32")
        assert sr == 16000 and audio.ndim == 1, "need 16 kHz mono"
        audio = audio[: 16000 * 30]
        with torch.inference_mode():
            t = time.time()
            ref = w(torch.from_numpy(audio)[None]).argmax(-1)[0].numpy()
            t_torch = time.time() - t
        for name in ["model.onnx"] + (["model.int8.onnx"] if a.int8 else []):
            sess = ort.InferenceSession(str(a.out / name), providers=["CPUExecutionProvider"])
            t = time.time()
            got = sess.run(None, {"waveform": audio[None]})[0].argmax(-1)[0]
            dt = time.time() - t
            print(f"{name}: {dt:.1f} s for {len(audio) / 16000:.0f} s of audio (torch {t_torch:.1f} s); "
                  f"same ids as torch: {np.array_equal(got, ref)}")
            print("  onnx :", ctc_decode(got, pieces, set(skip)))
        print("  torch:", ctc_decode(ref, pieces, set(skip)))


if __name__ == "__main__":
    main()
