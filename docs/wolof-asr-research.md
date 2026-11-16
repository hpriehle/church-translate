# Wolof speech recognition for Hear Church: which open model can ship

Status: 2026-11-16. Research + benchmark only, no app changes. Bench script: `scripts/bench/wolof_asr.py`.

## Why

Wolof is a listener-only language today (`app/languages.py`: Whisper has no `<|wo|>` token and can't recognise
Wolof). To let a church *speak* Wolof, the app needs a Wolof speech-to-text model that runs locally
(mlx-whisper / faster-whisper today), with a licence that allows a paid, closed-source app.

## Short answer

- **Pick: Meta Omnilingual ASR, CTC 300M v2 (`omniASR_CTC_300M_v2`)**, Apache-2.0 code and weights, Wolof
  (`wol_Latn`) trained on ~50 h. Measured on FLEURS Wolof test (40 utterances): WER 46%, CER 14.5%. That's
  rough, but it's the best quality we measured for its size, the download is small (1.3 GB fp32) and it's a
  plain CTC encoder, so it's fast and easy to port (no autoregressive decoding, no hallucination loops).
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
| `CAYTU/whosper-large-v2` | LoRA adapter on Whisper large-v2 (1.55B) | Apache-2.0 (Whisper itself MIT) | ALFFA, FLEURS, Bus Urbain, Anta Women TTS, Kallaama | WER 23.5%, CER 11.0% (own test set) | Probably; datasets' licences not all stated (Kallaama's not verified) |
| `AIHubSN/Kiriku-Wolof-ASR` | Whisper large-v2 full fine-tune | Apache-2.0 | `galsenai/wolof-audio-data` (Apache-2.0), 88 h, "agriculture, banking, urban" | WER 20.7% (own test) | Yes. Not benchmarked: full large-v2, ~6 GB fp32 download (> 5 GB limit) |
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
| Whosper-large-v2 (merged, int8) | not finished | | ~36–150 s per 15 s utterance (RTF ~2.5–10) | | 2 GB |

Whosper (the adapter merged into large-v2, converted to CTranslate2 int8) was stopped after 22 of 40
utterances: on the loaded box it took 36–150 s per utterance (a 1.55B autoregressive model is the slowest
option by far). Its text looked close to Omnilingual 1B's, but it
wasn't a top pick anyway: training-data licences (Kallaama) aren't clear and FLEURS is in its training data.

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

Verdict: Omnilingual 300M gives a translator something to work with (gist-level); whisper-small-wolof loses
too much. Neither is good enough for verbatim Wolof captions. Speed on this clip isn't meaningful: the box's
load average was 23 on 4 cores during the run (Omnilingual 300M RTF 3.4, whisper-small 2.2 here, vs 0.85 and
0.74 on FLEURS earlier in the day).

## Harrison decides

1. Is ~40–46% WER / 12–15% CER acceptable as input to translation for a Wolof-speaking preacher, given the
   output is a translation, not verbatim captions? Recommend: try it at one Wolof church before building more.
2. Port route for the pick: ONNX/Core ML export of Omnilingual CTC (smaller app, works on Windows) vs
   bundling PyTorch + fairseq2 (faster to try, big, Mac/Linux only).

## Credits needed if shipped

- Omnilingual ASR: Apache-2.0 notice (Meta). Wolof voice already credits galsenai (CC-BY).
- whisper-small-wolof (fallback): MIT notice (M9and2M) + Whisper MIT notice (OpenAI).
