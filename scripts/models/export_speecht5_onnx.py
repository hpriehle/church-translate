"""Export the Wolof SpeechT5 voice (bilalfaye/speecht5_tts-wolof + microsoft/speecht5_hifigan, both MIT) to ONNX, for
Windows and other CPU-only computers: the app runs it with onnxruntime (app/tts/speecht5_onnx.py), no torch.

Needs a Python with torch, transformers, onnx and onnxruntime, e.g.

  uv venv /tmp/st5 -p 3.12 && uv pip install --python /tmp/st5 torch --index-url https://download.pytorch.org/whl/cpu
  uv pip install --python /tmp/st5 transformers sentencepiece onnx onnxscript onnxruntime
  /tmp/st5/bin/python -I scripts/models/export_speecht5_onnx.py --out /tmp/wo-models/speecht5 --int8
  /tmp/st5/bin/python -I scripts/models/export_speecht5_onnx.py --out /tmp/wo-models/speecht5 --check

Writes into --out (see app/tts/speecht5_onnx.py for what each graph does):
  encoder.onnx, decoder.onnx, vocoder.onnx   float32
  encoder.int8.onnx, decoder.int8.onnx      with --int8: int8 weights (onnxruntime dynamic quantisation, per
                                            channel) in the transformer layers only (INT8 below). Quantising the
                                            whole decoder (its prenet, mel and stop projections) ruins the speech:
                                            a Wolof recogniser understood almost none of it (docs/windows-engine.md).
                                            The vocoder stays float32.
  tokens.json, config.json, LICENSE, NOTICE

config.json names the files the app loads ("encoder", "decoder", "vocoder"); --int8 points it at the int8 ones.
Package it with scripts/models/package_model.py --runtime speecht5-onnx.

--check speaks the test sentences with both runtimes using the same dropout masks and prints, per sentence, the
mel and waveform similarity to the torch model, then the ONNX real-time factor; --wavs DIR also writes both.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
MODEL = "bilalfaye/speecht5_tts-wolof"
VOCODER = "microsoft/speecht5_hifigan"

NOTICE = """Wolof voice (computers without Apple Silicon):
SpeechT5 TTS fine-tuned for Wolof by Bilal Faye (bilalfaye/speecht5_tts-wolof), MIT licence, and
Microsoft's SpeechT5 HiFi-GAN vocoder (microsoft/speecht5_hifigan), MIT licence,
converted to ONNX for Hear Church.
"""

MIT = """MIT License

Copyright (c) Microsoft Corporation; Copyright (c) Bilal Faye

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

# Sermon-style Wolof: what the voice speaks in a meeting (Bible verses, greetings, numbers, French loans).
SENTENCES = [
    "Yàlla mooy sunu Boroom, ñu ngi koy sant.",
    "Ndaxte Yàlla dafa bëgg àddina bi lool, ba joxe Doomam ji mu am moom kepp, ngir ku ko gëm du sànku, waaye dina am dund gu dul jeex.",
    "Dalal ak jàmm ci kër Yàlla gi.",
    "Tey, dinanu jàng téere Yowaana, saar bu ñetteel, aaya fukk ak juróom benn.",
    "Yeesu Krist moo di yoon wi, dëgg gi ak dund gi.",
    "Nanu ñaan ak xol bu sell, te sant Yàlla ci li mu nu defal.",
    "Xel mu Sell mi dina leen won yoon wi ci bépp mbir.",
    "Mbokk yi, bëgg-bëggu Yàlla amul dayo, te dootul jeex mukk.",
    "Ku gëm dina am kóllëre ak Yàlla, te dina dund ci jàmm.",
    "Jërëjëf ci seen ñëw, Yàlla na leen barkeel.",
]


# The weights stored as int8 (MatMul node names): the transformer layers' feed-forward and attention projections.
# The rest stays float32: the text embedding and relative positions, the decoder's prenet, mel and stop projections
# (int8 there made the speech unintelligible), and the vocoder.
LAYERS = ("intermediate_dense", "output_dense", "k_proj", "v_proj", "q_proj", "out_proj")
INT8 = {"encoder": LAYERS, "decoder": LAYERS}


def load():
    import torch
    from transformers import SpeechT5ForTextToSpeech, SpeechT5HifiGan, SpeechT5Processor

    torch.set_grad_enabled(False)
    model = SpeechT5ForTextToSpeech.from_pretrained(MODEL).eval().requires_grad_(False)
    vocoder = SpeechT5HifiGan.from_pretrained(VOCODER).eval().requires_grad_(False)
    return SpeechT5Processor.from_pretrained(MODEL), model, vocoder


def modules(model, vocoder):
    import torch
    from torch import nn

    dec = model.speecht5.decoder
    layers = dec.wrapped_decoder.layers
    prenet = dec.prenet
    post = model.speech_decoder_postnet
    cfg = model.config

    def heads(x, n, d):   # [1, T, n*d] -> [n, T, d]
        return x.view(x.size(0), -1, n, d).transpose(1, 2)[0]

    class Wrapper(nn.Module):
        def __init__(self):   # registered, so the weights export as initializers (int8 needs them so)
            super().__init__()
            self.model, self.vocoder = model, vocoder

    class Encoder(Wrapper):
        def forward(self, input_ids):
            h = model.speecht5.encoder(input_values=input_ids, return_dict=True).last_hidden_state
            ks = [heads(l.encoder_attn.k_proj(h), cfg.decoder_attention_heads, H) for l in layers]
            vs = [heads(l.encoder_attn.v_proj(h), cfg.decoder_attention_heads, H) for l in layers]
            return torch.stack(ks), torch.stack(vs)

    H = cfg.hidden_size // cfg.decoder_attention_heads

    def attend(attn, x, k, v):   # x [1, 1, D]; k, v [heads, S, H]
        q = heads(attn.q_proj(x) * attn.scaling, attn.num_heads, H)          # [heads, 1, H]
        w = torch.softmax(q @ k.transpose(1, 2), dim=-1)
        o = (w @ v).transpose(0, 1).reshape(1, 1, -1)
        return attn.out_proj(o)

    class Step(Wrapper):
        def forward(self, prev_mel, dropout_masks, position, speaker, cross_k, cross_v, past_k, past_v):
            x = prev_mel
            for i, layer in enumerate(prenet.layers):
                x = nn.functional.relu(layer(x)) * dropout_masks[i]
            x = prenet.final_layer(x)
            pe = prenet.encode_positions
            x = x + pe.alpha * pe.pe[:, position]
            spk = nn.functional.normalize(speaker).unsqueeze(1)
            x = nn.functional.relu(prenet.speaker_embeds_layer(torch.cat([x, spk], dim=-1)))
            new_k, new_v = [], []
            for i, l in enumerate(layers):
                sa = l.self_attn
                k = torch.cat([past_k[i], heads(sa.k_proj(x), sa.num_heads, H)], dim=1)
                v = torch.cat([past_v[i], heads(sa.v_proj(x), sa.num_heads, H)], dim=1)
                new_k.append(k)
                new_v.append(v)
                x = l.self_attn_layer_norm(x + attend(sa, x, k, v))
                x = l.encoder_attn_layer_norm(x + attend(l.encoder_attn, x, cross_k[i], cross_v[i]))
                x = l.final_layer_norm(x + l.feed_forward(x))
            h = x[:, 0]
            spec = post.feat_out(h).view(1, cfg.reduction_factor, cfg.num_mel_bins)
            prob = torch.sigmoid(post.prob_out(h))
            return spec, prob, torch.stack(new_k), torch.stack(new_v)

    class Vocoder(Wrapper):
        def forward(self, mel):
            return vocoder(post.postnet(mel))

    return Encoder().eval(), Step().eval(), Vocoder().eval(), H


def export(out: Path, int8: bool) -> None:
    import torch

    proc, model, vocoder = load()
    enc, step, voc, H = modules(model, vocoder)
    cfg = model.config
    L, NH = cfg.decoder_layers, cfg.decoder_attention_heads
    out.mkdir(parents=True, exist_ok=True)
    ids = torch.tensor([proc.tokenizer(SENTENCES[0])["input_ids"]])
    T = ids.shape[1]

    def onnx_export(m, args, path, names, outs, axes):
        torch.onnx.export(m, args, str(path), input_names=names, output_names=outs, dynamic_axes=axes,
                          opset_version=17, dynamo=False)

    onnx_export(enc, (ids,), out / "encoder.onnx", ["input_ids"], ["cross_k", "cross_v"],
                {"input_ids": {1: "T"}, "cross_k": {2: "T"}, "cross_v": {2: "T"}})
    ck, cv = enc(ids)
    S = 3
    args = (torch.zeros(1, 1, cfg.num_mel_bins), torch.ones(2, cfg.speech_decoder_prenet_units),
            torch.tensor([S], dtype=torch.long), torch.randn(1, cfg.speaker_embedding_dim), ck, cv,
            torch.zeros(L, NH, S, H), torch.zeros(L, NH, S, H))
    onnx_export(step, args, out / "decoder.onnx",
                ["prev_mel", "dropout_masks", "position", "speaker", "cross_k", "cross_v", "past_k", "past_v"],
                ["spec", "prob", "present_k", "present_v"],
                {"cross_k": {2: "T"}, "cross_v": {2: "T"}, "past_k": {2: "S"}, "past_v": {2: "S"},
                 "present_k": {2: "S1"}, "present_v": {2: "S1"}})
    onnx_export(voc, (torch.randn(1, 40, cfg.num_mel_bins),), out / "vocoder.onnx", ["mel"], ["waveform"],
                {"mel": {1: "N"}, "waveform": {1: "M"}})

    vocab = proc.tokenizer.get_vocab()
    pieces = [""] * (max(vocab.values()) + 1)
    for p, i in vocab.items():
        pieces[i] = p
    (out / "tokens.json").write_text(json.dumps({"pieces": pieces, "unk": proc.tokenizer.unk_token_id,
                                                 "eos": proc.tokenizer.eos_token_id}, ensure_ascii=False), encoding="utf-8")
    files = {"encoder": "encoder.onnx", "decoder": "decoder.onnx", "vocoder": "vocoder.onnx"}
    if int8:
        import onnx
        from onnxruntime.quantization import QuantType, quantize_dynamic

        for part in ("encoder", "decoder"):
            graph = onnx.load(str(out / f"{part}.onnx"), load_external_data=False).graph
            nodes = [n.name for n in graph.node if n.op_type == "MatMul" and any(k in n.name for k in INT8[part])]
            quantize_dynamic(str(out / f"{part}.onnx"), str(out / f"{part}.int8.onnx"), weight_type=QuantType.QInt8,
                             per_channel=True, nodes_to_quantize=nodes)
            files[part] = f"{part}.int8.onnx"
    conf = {"kind": "speecht5-onnx", "model": MODEL, "vocoder_model": VOCODER, **files,
            "layers": L, "heads": NH, "head_dim": H, "mels": cfg.num_mel_bins,
            "prenet_units": cfg.speech_decoder_prenet_units, "prenet_dropout": cfg.speech_decoder_prenet_dropout,
            "reduction": cfg.reduction_factor, "max_positions": cfg.max_speech_positions, "sample_rate": 16000}
    (out / "config.json").write_text(json.dumps(conf, indent=1), encoding="utf-8")
    (out / "NOTICE").write_text(NOTICE, encoding="utf-8")
    (out / "LICENSE").write_text(MIT, encoding="utf-8")
    for p in sorted(out.iterdir()):
        print(f"{p.name:24} {p.stat().st_size / 1e6:8.1f} MB")


def check(out: Path, wavs: Path | None, sentences: list[str], speaker: str) -> None:
    import torch

    sys.path.insert(0, str(ROOT))
    from app.tts.speecht5_onnx import SpeechT5Onnx

    proc, model, vocoder = load()
    onnx = SpeechT5Onnx(out)
    spk = np.load(ROOT / "models" / "wolof" / f"speaker-{speaker}.npy").astype(np.float32).reshape(1, -1)
    prenet = model.speecht5.decoder.prenet
    if wavs:
        wavs.mkdir(parents=True, exist_ok=True)
        import soundfile as sf

    tok_ok = all(onnx.tokenize(s) == proc.tokenizer(s)["input_ids"] for s in sentences + ["  Ndax ŋ 12 \"Ço\" ﬁ é"])
    print(f"tokenizer identical to transformers': {tok_ok}")
    total_audio = total_onnx = total_torch = 0.0
    rows = []
    for n, text in enumerate(sentences):
        rng = np.random.default_rng(n)
        drawn: list[np.ndarray] = []

        def masks(step):   # the same masks for both runtimes, in order
            while len(drawn) < step:
                drawn.append(onnx.masks(rng))
            return drawn[step - 1]

        calls = {"n": 0}

        def torch_dropout(x, p):   # the prenet runs on the whole sequence; only its last frame is used
            step, layer = divmod(calls["n"], 2)
            calls["n"] += 1
            m = torch.ones_like(x[0]) / (1 - p)
            m[-1] = torch.from_numpy(masks(step + 1)[layer])
            return x * m

        prenet._consistent_dropout = torch_dropout
        ids = proc(text=text, return_tensors="pt")["input_ids"]
        t0 = time.perf_counter()
        mel_t = model.generate_speech(ids, torch.from_numpy(spk), threshold=0.5, maxlenratio=6.0).numpy()
        wav_t = vocoder(torch.from_numpy(mel_t)).numpy()
        t1 = time.perf_counter()
        mel_o = onnx.mel(text, spk, threshold=0.5, maxlenratio=6.0, masks=masks)
        t2 = time.perf_counter()
        mel_o_post = model.speech_decoder_postnet.postnet(torch.from_numpy(mel_o)[None])[0].numpy()
        wav_o = onnx.vocode(mel_o)
        t3 = time.perf_counter()
        frames = min(len(mel_t), len(mel_o_post))
        a, b = mel_t[:frames].ravel(), mel_o_post[:frames].ravel()
        mel_cos = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
        mel_mae = float(np.abs(a - b).mean())
        m = min(len(wav_t), len(wav_o))
        wav_corr = float(np.corrcoef(wav_t[:m], wav_o[:m])[0, 1])
        secs = len(wav_o) / onnx.sr
        rows.append((n, len(mel_t), len(mel_o), mel_cos, mel_mae, wav_corr, secs, (t3 - t1) / secs, (t1 - t0) / secs))
        total_audio += secs
        total_onnx += t3 - t1
        total_torch += t1 - t0
        if wavs:
            sf.write(wavs / f"{n:02d}-torch.wav", wav_t, 16000)
            sf.write(wavs / f"{n:02d}-onnx.wav", wav_o, 16000)
    print("sent  frames(t/o)  mel-cos  mel-MAE  wav-corr  audio s  RTF onnx  RTF torch")
    for r in rows:
        print(f"{r[0]:4}  {r[1]:5}/{r[2]:<5}  {r[3]:.4f}   {r[4]:.4f}   {r[5]:.4f}  {r[6]:7.2f}  {r[7]:8.3f}  {r[8]:9.3f}")
    print(f"total audio {total_audio:.1f} s; RTF onnx {total_onnx / total_audio:.3f}, torch {total_torch / total_audio:.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--int8", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--wavs", type=Path)
    ap.add_argument("--speaker", default="male")
    a = ap.parse_args()
    if a.check:
        check(a.out, a.wavs, SENTENCES, a.speaker)
    else:
        export(a.out, a.int8)


if __name__ == "__main__":
    main()
