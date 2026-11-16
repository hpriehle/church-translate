# Wolof speech recognition for Hear Church: which open model can ship

Status: 2026-11-16. Research + benchmark only, no app changes. Bench script: `scripts/bench/wolof_asr.py`.

## Why

Wolof is a listener-only language today (`app/languages.py`: Whisper has no `<|wo|>` token and can't recognise
Wolof). To let a church *speak* Wolof, the app needs a Wolof speech-to-text model that runs locally
(mlx-whisper / faster-whisper today), with a licence that allows a paid, closed-source app.

## Short answer

- **Pick (updated 2026-11-16, evening): `AIHubSN/Kiriku-Wolof-ASR`** (Whisper large-v2 full fine-tune,
  Apache-2.0) **for Apple Silicon**, if a speed check on a Mac confirms real time. On the sermon clip it is clearly
  the most readable model: coherent Wolof sentences, French words written as French. On the same FLEURS
  utterances it beats Omnilingual 300M (n=4 only: CER 7.8% vs 11.5%, WER 27.8% vs 31.6%), though it trained on
  FLEURS. It drops into the existing Whisper pipeline (faster-whisper works as is; mlx-whisper needs a shard merge
  and a small tokenizer patch). It's far too slow for CPU-only machines (see "Kiriku" below).
- **CPU-only machines (Windows/Intel): Meta Omnilingual ASR, CTC 300M v2 (`omniASR_CTC_300M_v2`)**,
  Apache-2.0 code and weights, Wolof (`wol_Latn`) trained on ~50 h. Measured on FLEURS Wolof test (40
  utterances): WER 46%, CER 14.5%. Rougher, but small (1.3 GB fp32) and a plain CTC encoder, so it's fast
  and easy to port (no autoregressive decoding, no hallucination loops). This was the pick before Kiriku was
  measured.
- **If accuracy matters more than size: `omniASR_CTC_1B_v2`** (same licence, 3.9 GB): WER 39%, CER 12%. It's
  about 3x slower.
- **Fallback with the existing Whisper pipeline: `M9and2M/whisper-small-wolof` (MIT)**: drops straight into
  faster-whisper/mlx-whisper (prompt with `language=en`), 240 MB int8, but much worse (WER 62%, CER 29%).
- Not usable: **MMS-1B-all** and **MMS-1B** (CC-BY-NC-4.0, non-commercial), and anything fine-tuned from them.
  Google Chirp is hosted and billed per use (Harrison doesn't want per-use cost); mentioned only.

None of these is good enough for verbatim captions. They're good enough to feed the translator (Claude via
the hub) if the translator is told that the input is a noisy Wolof transcript. Whether that's acceptable
is a product decision (see "Harrison decides" at the end).

## Licence and model-card table

Checked on huggingface.co (API tags and model cards) and the Omnilingual ASR GitHub README, 2026-11-16.

| Model | Type / size | Licence (weights) | Training data stated on card | Card's own Wolof score | Commercial use in a paid closed app? |
|---|---|---|---|---|---|
| Meta Omnilingual ASR `omniASR_CTC_300M_v2` | CTC (wav2vec2-style), 300M, 1.3 GB | Apache-2.0 (code + models, README "License") | Meta's own corpus; released part `facebook/omnilingual-asr-corpus` is CC-BY-4.0. Wolof: 50.4 h | (7B LLM-ASR row: CER 11.1 for `wol_Latn`) | **Yes** (keep Apache notice) |
| Meta Omnilingual ASR `omniASR_CTC_1B_v2` | CTC, 1B, 3.9 GB | Apache-2.0 | same | same | **Yes** |
| Meta Omnilingual ASR LLM 300M–7B (incl. "Unlimited" long-audio variants) | encoder + LLM decoder | Apache-2.0 | same | 7B: CER 11.1 | Yes, but bigger/slower; not benchmarked |
| `CAYTU/whosper-large-v2` | LoRA adapter on Whisper large-v2 (1.55B) | Apache-2.0 (Whisper itself MIT) | ALFFA, FLEURS, Bus Urbain, Anta Women TTS, Kallaama | WER 23.5%, CER 11.0% (own test set) | Probably; Kallaama is CC-BY-4.0 (GitHub `gauthelo/kallaama-speech-dataset`, checked 2026-11-16), Anta Women TTS not verified |
| `AIHubSN/Kiriku-Wolof-ASR` | Whisper large-v2 full fine-tune, extra Wolof tokens in the tokenizer | Apache-2.0 (card + API tag). **Gated**: "agree to share your Hugging Face information" (auto-approved) | `galsenai/wolof-audio-data` (Apache-2.0), 88 h; that set is ALFFA + **FLEURS** + Urban Bus + Kallaama (CC-BY-4.0) | "Best WER 20.70%" (no test set named) | Yes (keep Apache notice; credit Kallaama CC-BY). Measured: see "Kiriku" below |
| `M9and2M/whisper-small-wolof` | Whisper small (244M) | MIT | `M9and2M/Wolof_ASR_dataset` (MIT), 57 h, clips < 6 s | WER 17% (own test) | **Yes** |
| `malinali-app/whisper-small-wolof-ggml` | same weights, whisper.cpp q8_0 | MIT | (as above) | — | Yes |
| `dofbi/wolof-asr` | Whisper small | MIT | `galsenai/wolof_tts` (CC-BY-4.0, TTS speech) | "WER 12%" (no test set named) | Yes (credit galsenai) |
| `galsenai/whisper-large-v3-wo` | Whisper large-v3 | **none stated** (empty auto-generated card) | not stated | not stated | **No** until a licence is stated |
| `asr-africa/wav2vec2-xls-r-wolof-kallaama-fleurs-51-hours` | wav2vec2 XLS-R 300M CTC | Apache-2.0 | Kallaama + FLEURS | — | Unclear: depends on Kallaama's licence (not verified) |
| `asr-africa/w2v2-bert-wolof-mixed-75-hours` | w2v-BERT 2.0 (600M) CTC | MIT (base w2v-bert-2.0 MIT) | FLEURS + mixed | — | Probably; not benchmarked |
| `bilalfaye/wav2vec2-large-mms-1b-wolof` | MMS-1B fine-tune | says MIT, but base `facebook/mms-1b` is **CC-BY-NC-4.0** | ALFFA | — | **No** (derived from NC weights) |
| `facebook/mms-1b-all` (adapter `wol`) | wav2vec2 1B + adapters | **CC-BY-NC-4.0** (verified) | FLEURS, MMS data | — | **No** |
| `soynade-research/Wolof-HuBERT-CTC` | HuBERT base CTC | **AGPL-3.0** | — | — | **No** for a closed app |
| Google Chirp (Cloud STT) | hosted | paid per minute | — | — | Not wanted (per-use cost); not tested |

NVIDIA: no Wolof NeMo/Canary/Parakeet model found on the hub.

## Benchmark

Setup: FLEURS `wo_sn` test set (CC-BY-4.0), the first 40 utterances in tsv order (665 s of audio). CPU only
on this Linux box (4 cores, 4 threads per run). WER/CER after lower-casing and removing punctuation
(`norm()` in the script). Omnilingual runs in float32 (bfloat16 is emulated and very slow on this CPU);
Whisper models via faster-whisper (CTranslate2) int8, greedy, prompted with `language=en`.

**Caveat on speed:** the box was heavily shared during the runs (load average 13–21 on 4 cores from other
jobs), so the real-time factors below are several times worse than an idle machine. Treat them as an
upper bound and as a *relative* ranking.

| Model | WER | CER | RTF (work s / audio s), loaded box | Load time | Peak RAM |
|---|---|---|---|---|---|
| Omnilingual CTC 300M v2 (fp32) | 45.7% | 14.5% | 0.85 | 7 s | 3.3 GB |
| Omnilingual CTC 1B v2 (fp32) | 39.2% | 12.2% | 2.53 | 165 s (first load, cold disk) | 5.5 GB |
| whisper-small-wolof (int8) | 62.1% | 29.4% | 0.74 | 2 s | 0.7 GB |
| Kiriku (large-v2 fine-tune, int8, `fr`) | n=4 only: 27.8% (O300 on the same 4: 31.6%) | 7.8% (O300: 11.5%) | 25 (load avg 16–23) | | |
| Whosper-large-v2 (merged, int8) | not finished | | ~36–150 s per 15 s utterance (RTF ~2.5–10) | | 2 GB |

Whosper (the adapter merged into large-v2, converted to CTranslate2 int8) was stopped after 22 of 40
utterances: on the loaded box it took 36–150 s per utterance (a 1.55B autoregressive model is the slowest
option by far). Its text looked close to Omnilingual 1B's, but it
wasn't a top pick anyway: FLEURS is in its training data (Kallaama turned out to be CC-BY-4.0, so that's fine).

Why WER looks high: FLEURS references write numbers as digits (`1990`) while every model spells them out in
Wolof, and Wolof spelling varies (French loanwords: `pyramid`/`piramit`, `lumiyeer`/`limier`). CER is the
fairer number. Example (FLEURS test #1):

```
REF   Misigu pyramid ak lumiyeer bu nuy wone mooy mbir yu gëna am solo ci diwaan gi ngir xale yi.
O300  misiku piramit ak limier bu ñuy wone mooy mbir yu gën am solo ci diiwaan gi ngir xale yi
O1B   misiku pyramide ak limieer buñuy wone mooy mbir yu gëna am solo ci diiwaan gi ngir xale yi
SMALL misiku piramid ak limiir bu ñuy woowe bu ñuy wone mooy mbir yu gën am soloo ci diiwaan ngir xale yi
```

Note: Whosper lists FLEURS among its training data, so its FLEURS score may be flattering.

### Apple Silicon estimate

Not measured (no Mac here). A CTC model is one encoder pass per window, no decoding loop. Omnilingual
CTC 300M is about the size of Whisper-small's encoder plus a bit; on an M1/M2 with PyTorch MPS (or a port
to MLX/Core ML) expect roughly RTF 0.02–0.05, i.e. well inside real time with room for the translator.
The 1B model should be ~3x that, still real time. Shipping it means adding PyTorch + fairseq2 (heavy, and
fairseq2 has no Windows wheels) or exporting the encoder (ONNX / Core ML / MLX); the CTC head and the
SentencePiece tokenizer are simple to reimplement. That port is the real engineering cost of this pick.

## Kiriku-Wolof-ASR (Whisper large-v2 fine-tune): measured (partial), best quality, slow on CPU

Measured 2026-11-16 21:36–22:32 UTC after Harrison accepted the gate and saved a read token. The 60-minute
time box ran out before the full subset finished, so **the FLEURS numbers are n=4, not n=30** (same
utterances for every model, the first 4 of the tsv), and **3 of the 9 sermon windows** (the first 60 s).
The partial per-utterance results are kept in `/tmp/wo-bench/res/kiriku-n30.partial.json` and
`kiriku-sermon.log`. The runs were stopped at the end of the time box because the box is shared.

Licence (re-checked 2026-11-16 via the HF API with the token, and the model card):

- Model: **Apache-2.0** (card metadata + API tag `license:apache-2.0`; gate is `auto`, no extra terms shown
  after accepting). Card base model `openai/whisper-large-v2` (MIT).
- Training data `galsenai/wolof-audio-data`: **Apache-2.0**, not gated. Sources per its card: ALFFA,
  **FLEURS** (via `vonewman/fleurs-wolof-dataset`), Urban Bus, Kallaama (CC-BY-4.0). The card doesn't say
  which FLEURS splits were used, so assume FLEURS test may be in training: the FLEURS score is flattering
  and the sermon clip is the fairer test.

How it was run:

- Download `hf download AIHubSN/Kiriku-Wolof-ASR` (2 safetensors shards, 6.2 GB fp32), converted with
  `ct2-transformers-converter --quantization int8` (1.5 GB `model.bin`). The repo has no `tokenizer.json`
  (faster-whisper needs one, or it silently falls back to stock Whisper's tokenizer), so it was written from
  the HF files with `WhisperTokenizerFast.from_pretrained(...).backend_tokenizer.save(...)` (51,869 tokens).
- Language token: the card says it's trained for "free transcription" without a forced language token, and
  the embeddings were warm-started from `<|fr|>`. faster-whisper always inserts a language token; with
  auto-detect it picked `jw` (p=0.19) on utterance 1, so the runs **force `language=fr`** (same text as
  auto on that utterance, apart from one word split, and no detection pass).
- faster-whisper int8, greedy (beam 1), no VAD, 4 threads; same `norm()` as the rest of this doc.

FLEURS `wo_sn` test, first 4 utterances (53 s of audio), all models on the same 4:

| Model | WER | CER |
|---|---|---|
| **Kiriku (int8, `fr`)** | **27.8%** | **7.8%** |
| Omnilingual CTC 300M v2 | 31.6% | 11.5% |
| Omnilingual CTC 1B v2 | 35.4% | 10.4% |
| whisper-small-wolof | 58.2% | 27.8% |

n=4 is too small to trust the size of the gap, and FLEURS may be in Kiriku's training data. The direction
matches the sermon clip, though.

```
REF   Misigu pyramid ak lumiyeer bu nuy wone mooy mbir yu gëna am solo ci diwaan gi ngir xale yi.
KIR   misiku pyramide ak limiyeer bu ñuy wone mooy mbir yu gëna am solo ci diiwaan gi ngir xale yi
O300  misiku piramit ak limier bu ñuy wone mooy mbir yu gën am solo ci diiwaan gi ngir xale yi
```

Speed on this box: **RTF 25 on FLEURS** (1331 s of work for 53 s of audio; 236–505 s per utterance) and
**RTF 19 on the sermon windows** (258–546 s per 20 s window), with the **load average at 16–23 on 4 cores**
the whole time (other jobs, plus a stray Omnilingual bench chain until 22:32). Even on an idle box it would be
several times slower than real time: **CPU-only machines can't run Kiriku live.**

Tokenizer catch (found in the code, applies to both runtimes): Kiriku added 4 tokens at ids 51865–51868
(`ŋ ƴ ɓ ɗ`; `ñ` and `ë` were already encodable). Those ids sit above Whisper's timestamp range (50364–51864).
faster-whisper's `Tokenizer.decode` keeps only ids < 50257, so these letters are **silently dropped**;
mlx-whisper's tiktoken tokenizer doesn't know them and treats ids ≥ 50364 as timestamps. Only `ŋ` matters for
Wolof; it occurs 5 times in the 30-utterance refs, and mapping `ŋ→n` changed Kiriku's CER by 0.3 points here.
Fix when integrating: decode ids 51865–51868 to their letters in the app (a few lines in either runtime).

mlx (Mac): `mlx-examples/whisper/convert.py` converts HF Whisper checkpoints (`hf_to_pt`, reads `n_vocab`
from `config.json`), but it loads a single `model.safetensors`; Kiriku is sharded, so merge the two shards
first (a few lines of Python). Add `-q` for 4/8-bit weights. Plus the tokenizer patch above. Not tried
(no Mac here); call it straightforward but unverified.

Speed on Apple Silicon (estimate, not measured): large-v2 has the same 32-layer encoder as large-v3-turbo but
32 decoder layers instead of 4 (1.55B vs 0.81B parameters). Decoding dominates for short windows, so expect
roughly 3–6x the turbo model's time per window. mlx-whisper large-v2/v3 runs comfortably faster than real
time on M1 Pro and later at fp16 (~3 GB of weights); a base M1 with 8 GB is the tight case once the
translator also runs, and 8-bit weights (~1.6 GB) help there. **Measure on the Mac before committing.**

Pick between the two: **Kiriku for Apple Silicon** (accuracy first: clearly better on the sermon clip and on
the same FLEURS utterances; and it fits the existing mlx-whisper/faster-whisper pipeline). **Omnilingual CTC
300M stays the fallback for CPU-only machines**, where Kiriku can't keep up. If the Mac check shows Kiriku
isn't real time on the target Macs, fall back to Omnilingual 300M everywhere.

Rerun (full n=30 and the whole sermon clip), best done on the Mac or an idle box:

```
hf download AIHubSN/Kiriku-Wolof-ASR --local-dir /tmp/wo-bench/kiriku-hf
ct2-transformers-converter --model /tmp/wo-bench/kiriku-hf --output_dir /tmp/wo-bench/kiriku-ct2 --quantization int8 \
  --copy_files tokenizer_config.json preprocessor_config.json added_tokens.json special_tokens_map.json vocab.json merges.txt normalizer.json
python -c "from transformers import WhisperTokenizerFast as T; T.from_pretrained('/tmp/wo-bench/kiriku-hf').backend_tokenizer.save('/tmp/wo-bench/kiriku-ct2/tokenizer.json')"
python scripts/bench/wolof_asr.py ct2 --model /tmp/wo-bench/kiriku-ct2 --lang fr --fleurs /tmp/wo-data/fleurs --n 30 --out kiriku-n30.json
python scripts/bench/wolof_asr.py ct2 --model /tmp/wo-bench/kiriku-ct2 --lang fr --clip /tmp/wo-data/clips/sermon3m.wav --out kiriku-sermon.json
```

The bench script now also writes `<out>.partial.json` after every FLEURS utterance, so a run stopped by a time
box still leaves per-utterance results.

## Sermon clip (judged by eye)

Clip: the first 3 minutes of `archive.org/download/Sermons_201307/Diamono.mp3`, a Wolof sermon recorded
in a hall (one speaker, room echo, Wolof mixed with French). It's a public archive.org item, likely a Muslim
sermon rather than a church one; no Wolof church recording with a clear licence turned up in the time box.
(A GRN "Jesus Story" Wolof recording, © 2006 Jesus Film Project, was also downloaded but is studio-read, not
preaching, so it wasn't used.) Cut into fixed 20 s windows, no VAD. Audio is not committed.

- **Omnilingual CTC 300M:** produces text for every window. Many words are recognisable Wolof (`yàlla`,
  `xale`, `jigéen`, `góor`, `takk`, `jabar`, `jëkkër`) and the topic (girls, marriage, the age at which
  children are married, `cinq ans`, `problème`) comes through, but word boundaries and spelling are often
  wrong and long stretches are garbled. French code-switching comes out spelled phonetically.
  Example (40 s): `… xale yi xaari ñu kenn ñoom bi ñuy génn ñu góoy ñi … jigéen bu ñu endal … góor bu ñu endal …`
- **whisper-small-wolof:** cleaner spelling where it outputs anything, but it **drops most of the speech**:
  whole 20 s windows came out as 3–5 words (`canna di anna ceegal`, `waccam na amul jëkkër di matas`).
  That's the model trained on clips under 6 s; it doesn't handle long, fast preaching.
- **Kiriku (first 3 windows only, 0–60 s; time box):** clearly the best. Whole windows read as Wolof
  sentences with plausible word boundaries, the topic (God, mothers, girls, pupils, marriage) is easy to
  follow, and French code-switching comes out as French (`côté`, `quoi`, `élèves`, `complété`, `comme`). It
  repeats a phrase once (`dafa bëri dafa bëri dafa bëri`) but that matches the speaker's own repetition in
  the Omnilingual output too. Same 40 s window as above:
  `foofu nga xale ba fa tool yi nga di ko maye … comme xale yi xaarul kenn ñoom dañuy génn ñu góor ñi ña koy
  uti jigéen bi ñu àndal ñi jigéen ñi ñoom ituti góor gu ñu àndal léeg-léeg …`
  Window 0 for comparison: Kiriku `yàlla dafay jur ndey bi nga ko jox côté bàqaar te ne yàlla dina yore ab
  ndey …` vs Omnilingual 300M `yàalo dafay dub mbéle nade gax ko te bàkkar nene njaloo di la warel ab mbéw …`.

Verdict: Kiriku is the only one close to a usable transcript (still not verbatim captions, but a translator
gets real sentences); Omnilingual 300M gives gist-level input; whisper-small-wolof loses too much. Speed on this clip isn't meaningful: the box's
load average was 23 on 4 cores during the run (Omnilingual 300M RTF 3.4, whisper-small 2.2 here, vs 0.85 and
0.74 on FLEURS earlier in the day).

## Harrison decides

1. Is the quality acceptable as input to translation for a Wolof-speaking preacher, given the output is a
   translation, not verbatim captions? Recommend: try Kiriku at one Wolof church before building more.
2. Kiriku on the Mac: run the full n=30 + sermon bench there (mlx-whisper after a shard merge, or
   faster-whisper) and check it keeps up in real time on the slowest Mac a church would use. That settles the
   pick.
3. Windows/Intel: ship Wolof only on Apple Silicon at first, or also port Omnilingual CTC 300M (ONNX/Core ML
   export, or bundling PyTorch + fairseq2: faster to try, big, Mac/Linux only) for CPU-only machines.

## Credits needed if shipped

- Kiriku-Wolof-ASR: Apache-2.0 notice (AI Hub Senegal) + Whisper MIT notice (OpenAI); credit Kallaama
  (CC-BY-4.0) and FLEURS (CC-BY-4.0), which are in its training data.
- Omnilingual ASR: Apache-2.0 notice (Meta). Wolof voice already credits galsenai (CC-BY).
- whisper-small-wolof (fallback): MIT notice (M9and2M) + Whisper MIT notice (OpenAI).
