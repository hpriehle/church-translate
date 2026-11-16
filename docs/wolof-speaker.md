# Wolof as a speaker language

Status: 2026-11-16. Built on `chat/hc-v3-mac`; the models are converted but **not uploaded** (the desk does that,
with Harrison's yes). Research behind the model choice: `docs/wolof-asr-research.md`.

## What the operator sees

- Wolof is in the **speaker** grid on every Mac (it used to be translation-only). It's always offered there, even
  before anything is downloaded: picking it downloads **Wolof speech recognition**, with the same progress bar as a
  voice. Settings → Languages lists it under "Speech recognition" with Download / Remove.
- The Wolof *voice* (`wo` package) is still only for a Wolof *translation*, and still Apple Silicon only. A Wolof
  speaker doesn't need it.
- Wolof chunks are translated into the meeting's translation languages through the paid path and metered like any
  other speaker language. Listeners who chose Wolof hear a Wolof speaker in the original (pass-through, not billed).
- Until the model is downloaded, Wolof speech is skipped and the operator's log says so once.

## The models

| Computer | Model | Runtime | Download |
|---|---|---|---|
| Apple Silicon | Kiriku-Wolof-ASR (AI Hub Senegal, Whisper large-v2 fine-tune, Apache-2.0), 8-bit | mlx-whisper's decoder (`app/wolof_asr.py: KirikuMlx`) | ~1.6 GB |
| Intel Macs (and Linux/Windows dev boxes) | Meta Omnilingual ASR CTC 300M v2 (Apache-2.0), int8 or fp32 ONNX | onnxruntime, already in both builds (`OmniOnnx`) | 0.33 GB int8 / 1.3 GB fp32 |

Why two: Kiriku reads much better (research doc), but it's a 1.55B autoregressive model, ~20x slower than real
time on a CPU. Omnilingual is one encoder pass per chunk. torch has no Intel Mac builds, so the Intel model is
exported to ONNX (`onnxruntime` ships in the Intel app already) instead of running fairseq2.

Kiriku needs three fixes that `scripts/models/convert_kiriku_mlx.py` and `KirikuMlx` make:

1. **Shards**: the repo has two safetensors shards; the converter reads both (mlx-examples' convert.py expects one).
2. **Language count**: mlx-whisper derives it from the vocabulary size (51869 - 51765 - 1 = 103), which would add
   `<|yue|>` and shift every special token (start, language, no-timestamps) by one. Large-v2 has 99; the converter
   writes `num_languages: 99` to `tokens.json` and the loader overrides the model's count.
3. **Added letters**: Kiriku added `ŋ ɗ ɓ ƴ` at ids 51865–51868 (in that order, from `added_tokens.json`), above
   Whisper's timestamp range; mlx-whisper would read them as timestamps and drop them. `KirikuMlx` decodes without
   timestamps, suppresses the real timestamp tokens, and `decode_tokens` turns 51865–51868 back into letters.

The language token is forced to `fr` (Kiriku's warm start); auto-detect is useless for Wolof (it picks `jw`). No
prompt is given (Kiriku was trained without one). Temperature fallback 0 / 0.2 / 0.4 as for Whisper; the output gets
Whisper's checks (empty, non-speech, repetition) through `Transcriber.transcribe(raw=...)`.

Kiriku is its own model object, not mlx-whisper's `ModelHolder` (which holds one model: swapping it with the
meeting's Whisper would reload gigabytes whenever the language changes). It is loaded while Wolof is a speaker
language and freed when it no longer is (`Pipeline.sync_wolof`). Memory on Apple Silicon: Whisper turbo (~1.6 GB)
plus Kiriku 8-bit (~1.7 GB).

## Routing: Whisper's detector can't hear Wolof

Whisper has no Wolof; its detector calls Wolof `jw`, `fr` or something else, with low confidence. So:

- **Wolof the only speaker language**: every chunk goes to the Wolof model; the detector isn't run.
- **Wolof and others** (`wolof_asr.route`): the detector runs; a chunk goes to another speaker language only when
  the detector is sure of it: p ≥ 0.5 (`OTHER_SURE`), **French p ≥ 0.7** (`FRENCH_SURE`: Wolof preaching is full
  of French words, and Kiriku writes them as French anyway), 0.1 less when the chunk before was in that language
  (`STICKY`). Everything else is Wolof: low confidence, `jw`, French-ish.
- A language **outside** the speaker set is recognised as itself only at p ≥ 0.8 and never `jw` or `fr`
  (`LOOKALIKES`); it is **never added**.
- **No automatic additions while Wolof is a speaker language** (the `hc-autolang` auto-add is skipped): two chunks
  of Wolof the detector takes for French would otherwise add French and turn the meeting's Wolof into Whisper's
  French. The operator adds languages by hand. Wolof itself is never auto-added (`UNDETECTABLE`, and
  `main.auto_add_speaker` refuses it).

The thresholds are a first guess, not measured on a mixed Wolof/French recording (none with a clear licence was
found). The health reports' `asr.langs` (detected vs recognised) will show how they do.

The interpreter is told when Wolof is a source that the Wolof text is a rough transcript (`prompts.WOLOF_INPUT`).

## Hosting

Same as the Wolof libraries: one archive per model at the root of the R2 bucket, served by the download Worker
(`/download/<name>.tar.gz` is already allowed: no Worker change).

```
https://hearchurch.com/download/wolof-asr-kiriku-mlx-q8-<hash12>.tar.gz        Apple Silicon
https://hearchurch.com/download/wolof-asr-omni300m-onnx-int8-<hash12>.tar.gz   Intel
```

`<hash12>` comes from the files' hashes, so a new conversion gets a new name and old apps keep theirs. Each archive
holds one folder `<id>/` with the model files, `config.json`, `tokens.json`, `NOTICE` and `LICENSE`.
`app/wolof_asr.json` (in the app) names each runtime's archive, its sha256 and size; the app checks the hash
before unpacking into `~/Library/Application Support/Hear Church/models/<id>/` and deletes older versions of
the same model. An entry left `null` means "not published yet": the app offers Wolof but the download says it
can't be downloaded yet.

## What the desk runs (on the Mac), with Harrison's yes for the upload

```
# 1. Kiriku → mlx (gated: accept the licence on huggingface.co once, `hf auth login`)
uv venv /tmp/kiriku && uv pip install --python /tmp/kiriku mlx mlx-whisper==0.4.3 safetensors huggingface_hub soundfile pydantic
hf download AIHubSN/Kiriku-Wolof-ASR --local-dir /tmp/kiriku-hf
/tmp/kiriku/bin/python -I scripts/models/convert_kiriku_mlx.py --hf /tmp/kiriku-hf --out /tmp/wo-models/kiriku-q8 --bits 8
# quality and speed: a FLEURS utterance and the sermon clip (compare 4-bit with --bits 4 if 8-bit is slow)
/tmp/kiriku/bin/python -I scripts/models/convert_kiriku_mlx.py --check /tmp/wo-models/kiriku-q8 clip.wav [ref.txt]
python3 -I scripts/models/package_model.py /tmp/wo-models/kiriku-q8 --runtime mlx \
    --prefix wolof-asr-kiriku-mlx-q8 --name "Kiriku-Wolof-ASR, 8-bit" --out mac/models-out

# 2. Omnilingual → ONNX: already exported and checked on the Linux box (see below); to redo it:
uv venv /tmp/omni && uv pip install --python /tmp/omni omnilingual-asr "onnx<1.18" onnxruntime "numpy<2" soundfile
/tmp/omni/bin/python -I scripts/models/export_omnilingual_onnx.py --out /tmp/wo-models/omni300m --int8 --check clip.wav
python3 -I scripts/models/package_model.py /tmp/wo-models/omni300m --runtime onnx --exclude model.onnx \
    --prefix wolof-asr-omni300m-onnx-int8 --name "Omnilingual ASR CTC 300M v2, int8" --out mac/models-out

# 3. upload (publishes), then commit app/wolof_asr.json
rclone copyto mac/models-out/<id>.tar.gz r2:<bucket>/<id>.tar.gz
curl -fsSI https://hearchurch.com/download/<id>.tar.gz | grep -i content-length
```

## Measured on the Linux box (2026-11-16, CPU only, 4 cores, other jobs running: load average 3–14)

Omnilingual CTC 300M v2, ONNX (`export_omnilingual_onnx.py --int8 --check`):

| | FLEURS wo_sn test, first 40 (CER) | RTF on this box |
|---|---|---|
| torch (research doc) | 14.5% | 0.85 |
| ONNX fp32 | 14.4%; **identical CTC ids to torch** on a 20 s sermon window | 1.0 (load ~12) / 0.47 on the 20 s window |
| **ONNX int8 (shipped)** | 16.1% | 0.47 (load ~12) / 0.30 |

int8 costs 1.7 CER points for half the work and a quarter of the download (244 MB archive). Intel Macs are the
slowest machines a church will use, so int8 is the pick; fp32 is one `package_model.py` run away if quality
complaints come in. End to end through the app's code (hash check, unpack, `WolofRecogniser`): load 2 s, a 12 s
FLEURS utterance in 5.7 s, CER 21% on that utterance.

Packaged (not uploaded): `wolof-asr-omni300m-onnx-int8-e42e3ebb0c34.tar.gz`, 243,635,948 bytes, sha256
`bbb54ca87f30fd2bf014cb3a4de99124549840822c19b9f3ef5dc5c87ccc4c92` (deterministic: re-running the export and
`package_model.py` on the same weights gives the same file). The entry goes into `app/wolof_asr.json` under
`"onnx"` once it's uploaded.

Kiriku 8-bit mlx (`convert_kiriku_mlx.py`, run here with `mlx[cpu]`): converts in ~2 minutes to 1.64 GB, and
finds the added letters `{51865: ŋ, 51866: ɗ, 51867: ɓ, 51868: ƴ}`. Tokenizer check against Kiriku's own Hugging
Face tokenizer: with mlx-whisper's default 103 languages `<|transcribe|>`, `<|notimestamps|>` and every
timestamp are off by one; with 99 all special ids match. Stock mlx decode turns `impresiyoŋ … guŋŋewoon` into
`impresiyo … guewoon`; `decode_tokens` round-trips the sentence exactly. **Speed and quality of Kiriku on mlx
must be measured on the Mac** (the CPU build of mlx is far too slow for a meaningful number here).
