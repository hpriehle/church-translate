"""Convert AIHubSN/Kiriku-Wolof-ASR (Whisper large-v2 fine-tune, Apache-2.0) to mlx-whisper weights, for Wolof
speech recognition on Apple Silicon. Run on the Mac (it also works on Linux with `mlx[cpu]`, slowly):

  uv venv /tmp/kiriku && uv pip install --python /tmp/kiriku mlx mlx-whisper==0.4.3 safetensors huggingface_hub
  hf download AIHubSN/Kiriku-Wolof-ASR --local-dir /tmp/kiriku-hf        # gated: accept on huggingface.co, hf auth login
  /tmp/kiriku/bin/python -I scripts/models/convert_kiriku_mlx.py --hf /tmp/kiriku-hf --out /tmp/wo-models/kiriku-q8 --bits 8
  /tmp/kiriku/bin/python -I scripts/models/convert_kiriku_mlx.py --check /tmp/wo-models/kiriku-q8 clip.wav [ref.txt]

What it does (docs/wolof-speaker.md):
  - reads both safetensors shards (the repo is sharded; mlx-examples' convert.py expects one file) and renames the
    Hugging Face weights to mlx-whisper's names, as mlx-examples' whisper/convert.py does;
  - float16, or 4/8-bit (--bits, mlx group quantisation of the Linear and Embedding layers);
  - writes config.json (mlx-whisper's ModelDimensions + "quantization") and weights.safetensors, which
    mlx_whisper.load_models.load_model reads;
  - writes tokens.json: the letters Kiriku added after Whisper's timestamp tokens (ŋ ɗ ɓ ƴ at 51865–51868), the real
    number of languages (99: large-v2), and the language token to force ("fr", the warm start; auto-detect is useless
    for Wolof). The app's decoder (app/wolof_asr.py) needs all three: mlx-whisper would count 103 languages from the
    bigger vocabulary (shifting every special token by one) and would read the added letters as timestamps.
  - NOTICE with the credits the licences ask for, and LICENSE (Apache-2.0).

--check transcribes a 16 kHz mono wav with the app's own Kiriku decoder and prints the time it took (and the CER
against ref.txt if given). Package the folder with scripts/models/package_model.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

LANGUAGE = "fr"        # the token Kiriku's embeddings were warm-started from
NUM_LANGUAGES = 99     # Whisper large-v2

NOTICE = """Wolof speech recognition (Apple Silicon):
Kiriku-Wolof-ASR by AI Hub Senegal (AIHubSN), https://huggingface.co/AIHubSN/Kiriku-Wolof-ASR
Licensed under the Apache License, Version 2.0. Converted to mlx (float16{q}) for Hear Church.
Fine-tuned from Whisper large-v2, Copyright (c) 2022 OpenAI, MIT License.
Trained on galsenai/wolof-audio-data (Apache-2.0), which includes the Kallaama corpus (CC BY 4.0,
https://github.com/gauthelo/kallaama-speech-dataset) and Google FLEURS (CC BY 4.0).
"""


def remap(k: str) -> str:
    """A Hugging Face Whisper weight name -> mlx-whisper's (mlx-examples whisper/convert.py hf_to_pt + convert)."""
    for a, b in ((".layers", ".blocks"), (".self_attn_layer_norm", ".attn_ln"), (".self_attn", ".attn"),
                 (".encoder_attn_layer_norm", ".cross_attn_ln"), (".encoder_attn.", ".cross_attn."),
                 (".final_layer_norm", ".mlp_ln"), (".q_proj", ".query"), (".k_proj", ".key"),
                 (".v_proj", ".value"), (".out_proj", ".out"), (".fc1", ".mlp1"), (".fc2", ".mlp2"),
                 ("embed_positions.weight", "positional_embedding"), ("decoder.embed_tokens", "decoder.token_embedding"),
                 ("encoder.layer_norm", "encoder.ln_post"), ("decoder.layer_norm", "decoder.ln")):
        k = k.replace(a, b)
    return k.removeprefix("model.")


def dims(cfg: dict) -> dict:
    return {"n_mels": cfg["num_mel_bins"], "n_audio_ctx": cfg["max_source_positions"], "n_audio_state": cfg["d_model"],
            "n_audio_head": cfg["encoder_attention_heads"], "n_audio_layer": cfg["encoder_layers"],
            "n_vocab": cfg["vocab_size"], "n_text_ctx": cfg["max_target_positions"], "n_text_state": cfg["d_model"],
            "n_text_head": cfg["decoder_attention_heads"], "n_text_layer": cfg["decoder_layers"]}


def added_letters(hf: Path, first: int) -> dict[str, str]:
    """Tokens the fine-tune added after Whisper's own (ids >= first): {"51865": "ŋ", ...}."""
    added = json.loads((hf / "added_tokens.json").read_text())
    return {str(i): t for t, i in sorted(added.items(), key=lambda kv: kv[1]) if i >= first and not t.startswith("<|")}


def convert(hf: Path, out: Path, bits: int, group_size: int) -> None:
    import mlx.core as mx
    import mlx.nn as nn
    from mlx.utils import tree_flatten, tree_unflatten
    from mlx_whisper.whisper import ModelDimensions, Whisper
    from safetensors import safe_open

    cfg = json.loads((hf / "config.json").read_text())
    d = dims(cfg)
    shards = sorted(hf.glob("model*.safetensors"))
    if not shards:
        raise SystemExit(f"no model*.safetensors in {hf}")
    weights = {}
    for shard in shards:   # both shards into one dict: the "merge"
        with safe_open(str(shard), framework="numpy") as f:
            for k in f.keys():
                name = remap(k)
                if name in ("proj_out.weight", "encoder.positional_embedding"):
                    continue   # output projection = token embedding; the encoder's positions are computed
                v = f.get_tensor(k)
                if "conv" in name and v.ndim == 3:
                    v = v.swapaxes(1, 2)
                weights[name] = mx.array(v).astype(mx.float16)
        print(f"read {shard.name}", flush=True)
    model = Whisper(ModelDimensions(**d), mx.float16)
    model.update(tree_unflatten(list(weights.items())))
    missing = {k for k, _ in tree_flatten(model.parameters())} - set(weights) - {"alignment_heads"}
    if missing:
        raise SystemExit(f"weights missing after the rename: {sorted(missing)[:5]}")
    del weights
    config = {"model_type": "whisper", **d}
    if bits:
        nn.quantize(model, group_size=group_size, bits=bits)
        config["quantization"] = {"group_size": group_size, "bits": bits}
    out.mkdir(parents=True, exist_ok=True)
    mx.save_safetensors(str(out / "weights.safetensors"), dict(tree_flatten(model.parameters())))
    (out / "config.json").write_text(json.dumps(config, indent=1))
    extra = added_letters(hf, 51865)   # large-v2's own vocabulary ends at 51864 (<|30.00|>)
    (out / "tokens.json").write_text(json.dumps({"kind": "kiriku-mlx", "language": LANGUAGE,
                                                 "num_languages": NUM_LANGUAGES, "extra": extra},
                                                ensure_ascii=False, indent=1))
    (out / "NOTICE").write_text(NOTICE.format(q=f", {bits}-bit weights" if bits else ""))
    (out / "LICENSE").write_text((Path(__file__).resolve().parents[2] / "licenses" / "Apache-2.0.txt").read_text())
    size = sum(p.stat().st_size for p in out.iterdir())
    print(f"wrote {out} ({size / 1e9:.2f} GB); added letters: {extra}")


def check(folder: Path, wav: Path, ref: Path | None) -> None:
    import soundfile as sf

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from app.wolof_asr import KirikuMlx, cer

    audio, sr = sf.read(str(wav), dtype="float32")
    assert sr == 16000 and audio.ndim == 1, "need 16 kHz mono"
    t = time.time()
    m = KirikuMlx(folder)
    m.load()
    print(f"loaded in {time.time() - t:.1f} s")
    texts, work = [], 0.0
    for i in range(0, len(audio), 16000 * 10):   # 10 s pieces, like the app's longest chunks
        piece = audio[i:i + 16000 * 10]
        if len(piece) < 8000:
            continue
        t = time.time()
        text, nsp, alp, _cr = m.transcribe(piece)
        work += time.time() - t
        texts.append(text)
        print(f"[{i // 16000:4d}s] {text}  (no speech {nsp:.2f}, logprob {alp:.2f}, {time.time() - t:.1f} s)", flush=True)
    print(f"RTF {work / (len(audio) / 16000):.2f} (seconds of work per second of speech)")
    if ref:
        print(f"CER {cer(ref.read_text(), ' '.join(texts)):.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hf", type=Path, help="local copy of AIHubSN/Kiriku-Wolof-ASR")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--bits", type=int, default=8, choices=(0, 4, 8), help="0 = float16")
    ap.add_argument("--group-size", type=int, default=64)
    ap.add_argument("--check", nargs="+", metavar=("FOLDER", "WAV"), help="FOLDER WAV [REF.txt]")
    a = ap.parse_args()
    if a.check:
        check(Path(a.check[0]), Path(a.check[1]), Path(a.check[2]) if len(a.check) > 2 else None)
    elif a.hf and a.out:
        convert(a.hf, a.out, a.bits, a.group_size)
    else:
        ap.error("give --hf and --out, or --check FOLDER WAV")


if __name__ == "__main__":
    main()
