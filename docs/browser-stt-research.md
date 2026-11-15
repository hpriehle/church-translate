# Server speech recognition for the browser version: research note

Researched 2026-11-15. Every price below was read on 2026-11-15 from the URL given next to it.

**Why this is needed.** In the planned browser version, a hearchurch.com page on a church computer streams room audio to the hub. The audio is one speaker at a time, sometimes switching between two languages. The hub (a small VPS with no GPU) must turn that audio into text live. Translation and voice then use the existing path. Today the Mac app runs mlx-whisper large-v3-turbo on the Mac itself (`app/stt.py`), so the browser version needs a server-side replacement.

**What the replacement needs:**
- **Speaker languages, in priority order:** ru, lv, en, es, fr, pt, uk.
- **Nice to have:** fa, ne, vi, zh, wo.
- **Revenue it must fit under:** about $3.57–$5 per meeting-hour, plus 1 free hour per church each month.

**How sure each finding is.** "Listed" means the language appears in the vendor's language list for that model. Most vendors do not publish a separate list for streaming, so for those it is **not verified** that each language works in streaming. Anything marked *unverified* was not found on an official page today.

## Summary table

Prices are list price, pay-as-you-go, in USD per audio hour, for streaming. "Training" means whether the vendor trains on our audio by default.

| Option | Price $/h (streaming) | lv | wo | Other must-haves (ru en es fr pt uk) | Nice-to-haves (fa ne vi zh) | Code-switching in streaming | Custom vocabulary (streaming) | Training on our audio | Self-serve concurrency |
|---|---|---|---|---|---|---|---|---|---|
| **Deepgram Nova-3** | 0.29 one language, 0.35 multilingual; keyterms +0.08 | listed | ✗ | all listed | all listed (ne in one-language mode only) | `multi` mode, but which languages it covers is unclear | Keyterm Prompting: up to 500 tokens, can be changed mid-stream | **yes by default**; per-request `mip_opt_out=true` (then data is kept only while processing) | 150 streams |
| **ElevenLabs Scribe v2 Realtime / v3 Live** | 0.22; keyterms +0.08 | listed (≤5% WER, batch figure) | listed (25–50% WER) | listed as part of "90+" | listed | v3 Live `multilang_mode` (switches mid-sentence) plus `secondary_languages` | 50 keyterms of ≤20 characters each (v3 Live also has a 10k-term library) | **yes by default**; opt-out in the account settings; zero retention and EU residency need Enterprise | not stated |
| **Speechmatics** | 0.40 (Melia 1) to 0.80 (Enhanced) | listed (realtime *unverified*) | ✗ | all listed | fa vi zh listed, ne ✗ | Melia 1 / Oak 1 `language=multi` | `additional_vocab` custom dictionary | **no**; training is opt-in, and opting in gets 33% off | 50 sessions |
| **AssemblyAI Universal-3.6 Pro Realtime** | 0.45, keyterms included | **✗** | ✗ | **uk ✗**, rest listed | fa vi zh listed, ne ✗ | native, no setup needed | up to 100 keyterms | free accounts can't opt out; opt-out is by e-mail; EU servers are excluded from training | 100 new streams per minute |
| **Gladia Solaria-1** | €0.68 | listed | listed (no code-switching) | listed (en es fr not checked) | listed | yes, all except wo | custom vocabulary included | **yes by default**; opt-out in profile | 30 sessions |
| **Google STT V2 Chirp 3** | 0.96 | listed (eu) | **listed (wo-SN, eu)** | all listed | all listed | `auto`, or a list of expected locales (mid-stream switching *unverified*) | adaptation phrase sets, up to 1,000 phrases | **no**; only if we opt in to data logging | *unverified* |
| **Azure AI Speech real-time** | 1.00 (custom model 1.20; enhanced features +0.30) | ✓ real-time | ✗ | all ✓ real-time | all ✓ real-time | continuous language ID across up to 10 languages, but not within a single sentence | phrase list (about 200 recommended); not documented for lv/uk/fa/ne | no (recalled, *unverified* today) | not read |
| **OpenAI gpt-live-transcribe** | 1.02 (`gpt-transcribe` 0.27, but it is turn-based) | *no language list published* | ? | ? | ? | language hints only; no detected-language output | `prompt` plus a `keywords` list | no (API data not used unless we opt in); zero retention needs approval | 2,000 requests per minute |
| **Self-hosted faster-whisper large-v3-turbo** (rented GPU) | about 0.23–0.35 per meeting-hour with 2–3 meetings at once (see below) | yes, but about 18–24% WER (large-v3, FLEURS) | ✗ (not in Whisper) | all | all | Whisper detects language per chunk (as in today's app) | `initial_prompt` / `hotwords` | none (our own server) | as many as our GPU allows |

## Sources and details per option

### Deepgram
- **Pricing** ([deepgram.com/pricing](https://deepgram.com/pricing), read 2026-11-15):
  - Nova-3 one language: $0.0048/min, which is $0.29/h.
  - Nova-3 multilingual: $0.0058/min, which is $0.35/h.
  - Keyterm prompting add-on: $0.0013/min, which is $0.08/h.
  - The FAQ on the same page still shows older prices of $0.46/h and $0.55/h.
  - $200 free credit, no card needed. Pay-as-you-go allows 150 concurrent streams.
- **Languages** ([models-languages-overview](https://developers.deepgram.com/docs/models-languages-overview), read 2026-11-15):
  - The Nova-3 multilingual option lists ru, lv, en, es, fr, pt, uk, fa, vi, zh.
  - ne is in the one-language option only. Wolof is in no Deepgram model.
  - The page does not say per language whether streaming works.
  - The page names the languages covered by `multi` only for Nova-3 Medical and Flux (10 languages, without lv or uk). For general Nova-3 it is not clear whether `multi` switches between, for example, ru↔lv. **This must be tested.**
- **Keyterms** ([docs/keyterm](https://developers.deepgram.com/docs/keyterm), read 2026-11-15):
  - Works on Nova-3 in both one-language and multilingual mode, and in streaming.
  - Can be replaced mid-stream with a `Configure` message.
  - Limit is 500 tokens across all keyterms. Deepgram recommends 20–50 terms.
- **Data** ([Model Improvement Program](https://developers.deepgram.com/docs/the-deepgram-model-improvement-partnership-program)):
  - Requests are in the program, and so used for training, **by default**.
  - Setting `mip_opt_out=true` on each request opts out. Opted-out audio is kept only while it is processed; metadata and usage logs are kept.
  - No price difference for opting out was found on Deepgram's own pages.
  - An EU endpoint exists, but this comes from a secondary source; the official terms were not read.

### ElevenLabs
- **Pricing** ([elevenlabs.io/pricing/api](https://elevenlabs.io/pricing/api), read 2026-11-15):
  - Scribe v2 Realtime: $0.22/h. Keyterms: +$0.08/h.
  - The page appears to put Scribe v3 Live in the same table, but its exact row was unclear, so **confirm the v3 Live price**.
- **Languages and accuracy** ([STT capabilities](https://elevenlabs.io/docs/overview/capabilities/speech-to-text)):
  - "90+ languages". Accuracy tiers are for Scribe v2 in batch: lv, uk, ru ≤5% WER; fa, ne 5–10%; wo 25–50%.
  - The [realtime language options page](https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/language-options) does not list languages. So it is **unverified** that lv and wo work in realtime.
  - `multilang_mode=true` is v3 Live only, and works over the raw WebSocket only (not the SDKs). It follows switches mid-sentence.
  - `secondary_languages` narrows language detection to the languages we expect.
- **Keyterms** (same capabilities page): realtime allows up to 50 keyterms of up to 20 characters each. That is tight for our glossary.
- **Data:**
  - The [privacy policy](https://elevenlabs.io/privacy-policy) says inputs may be used to train models. The opt-out is under "Data use" in account settings. For business customers the contract governs instead.
  - [Data residency](https://elevenlabs.io/docs/overview/administration/data-residency): EU residency is Enterprise-only, and Zero Retention Mode is a separate switch.
- **Latency:** the marketing page claims about 150 ms.

### Speechmatics
- **Pricing** ([speechmatics.com/pricing](https://www.speechmatics.com/pricing), read 2026-11-15), realtime per hour:
  - Melia 1: $0.40
  - Oak 1: $0.46
  - Standard: $0.45
  - Enhanced: $0.80
  - $100 free credit, valid 12 months. 50 concurrent realtime sessions on self-serve.
- **Languages** ([docs languages](https://docs.speechmatics.com/speech-to-text/languages)):
  - lv, ru, uk, fa, vi, cmn, pt, fr, es, en are listed. ne and wo are not.
  - The docs and marketing pages disagree on whether Melia realtime is available yet ("coming soon").
- **Latency:** `max_delay` can be set from 0.7 s to 4 s, and partial results typically arrive in under 500 ms.
- **Data:** training is opt-in, and opting in earns a 33% discount. Default retention and EU terms are not verified (the trust page returned 404).

### AssemblyAI
- **Pricing** ([assemblyai.com/pricing](https://www.assemblyai.com/pricing), read 2026-11-15):
  - Universal-3.6 Pro Realtime: $0.45/h, keyterms included.
  - Universal-Streaming Multilingual (en es de fr pt it only): $0.15/h.
  - Billing is by WebSocket session time.
- **Languages** ([streaming multilingual](https://www.assemblyai.com/docs/streaming/multilingual-transcription.md)): **no Latvian and no Ukrainian.** That rules AssemblyAI out.

### Gladia
- **Pricing** ([gladia.io/pricing](https://www.gladia.io/pricing), read 2026-11-15):
  - Realtime: €0.68/h (€0.0113/min).
  - €50 free credit. 30 concurrent sessions.
- **Languages** ([supported languages](https://docs.gladia.io/chapters/language/supported-languages)):
  - Solaria-1 lists lv and wo. Code-switching works for every language except wo.
  - The page does not separate realtime from async support.
- **Data:** self-serve trains on our audio by default, with an opt-out in the profile. Zero retention is Enterprise-only.

### Google Cloud Speech-to-Text V2 (Chirp 3)
- **Pricing** ([cloud.google.com/speech-to-text/pricing](https://cloud.google.com/speech-to-text/pricing), read 2026-11-15; V2 "Standard" models include `chirp`):
  - $0.016/min, which is $0.96/h, for the first 500k minutes a month.
  - Cheaper tiers above that ($0.01, $0.008, $0.004 per minute).
- **Model** ([chirp_3 doc](https://docs.cloud.google.com/speech-to-text/v2/docs/chirp_3-model)):
  - Supports `StreamingRecognize`. GA in the `us` and `eu` multi-regions.
  - Adaptation phrase sets (GA) take up to 1,000 phrases.
  - `language_codes=["auto"]`, or a list of the languages we expect.
- **Languages** ([supported languages](https://docs.cloud.google.com/speech-to-text/v2/docs/speech-to-text-supported-languages)):
  - chirp_3 lists ru, lv-LV, en, es, fr, pt, uk, fa, ne, vi, cmn and **wo-SN** (in `eu`).
  - **Google is the only hosted option found that lists Wolof.**
  - Streaming per language and latency are *unverified*.
- **Data** ([data logging](https://docs.cloud.google.com/speech-to-text/docs/data-logging)): audio and transcripts are not logged by default. Google trains on data only if we opt in to data logging (which is discounted).

### Azure AI Speech
- **Pricing** (Microsoft's official [Retail Prices API](https://prices.azure.com/api/retail/prices), filtered to `productName eq 'Azure Speech'`, region westeurope, read 2026-11-15):
  - S1 Speech To Text: **$1.00/h**.
  - Custom Speech To Text: $1.20/h.
  - Enhanced Feature Audio: +$0.30/h.
  - Free tier: 5 h a month.
  - The [pricing page](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/) itself renders its prices as "$-".
- **Languages** ([language support](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=stt)): real-time ✓ for ru, lv, en, es, fr, pt, uk, fa, ne, vi, zh. No Wolof.
- **Language identification** ([doc](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-identification)):
  - Continuous language ID handles up to 10 candidate languages, but not a switch inside one sentence.
  - It can bill more time than the audio we send.

### OpenAI
- **Pricing** ([pricing](https://developers.openai.com/api/docs/pricing), read 2026-11-15):
  - `gpt-live-transcribe`: $0.017/min, which is $1.02/h.
  - `gpt-transcribe`: $0.0045/min, which is $0.27/h. It is turn-based: it starts after a committed turn.
  - Regional data-residency endpoints add 10%.
- **Languages:** no published list. Language hints only, and `gpt-live-transcribe` returns no detected language ([realtime transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription)).
- **Data:** API data is not used for training. Zero Data Retention needs approval from OpenAI ([your data](https://developers.openai.com/api/docs/guides/your-data)).

### Self-hosting (faster-whisper / whisper.cpp on a rented GPU)
- **GPU prices**, on-demand, read 2026-11-15:
  - [RunPod](https://www.runpod.io/pricing): L4 **$0.69/h**; RTX 4090 $0.89/h.
  - [Lambda](https://lambda.com/pricing): A10 $1.29/h (from 2026-11-16).
  - [Hetzner GEX45](https://www.hetzner.com/dedicated-rootserver/gex44/) (RTX PRO 4000 Blackwell): €214/month plus €209 setup, excluding VAT.
  - Vast.ai and the GCP L4 price did not load: *unverified*.
- **Capacity:**
  - The [faster-whisper README](https://github.com/SYSTRAN/faster-whisper) benchmark is large-v2 on an RTX 3070 Ti: 13 minutes of audio took 59 s in int8, 16 s when batched.
  - [WhisperLive](https://github.com/collabora/WhisperLive) defaults to 4 clients per server.
  - **Estimate, not measured:** one L4 running large-v3-turbo should carry 3–5 live streams with one model per stream, or more with one shared model and batching.
- **Cost per meeting-hour** at 2–3 meetings at once on one L4: $0.69 ÷ 2–3 ≈ **$0.23–0.35**. That holds only if the GPU runs just while meetings run.
  - Kept on all the time: about $500 a month on RunPod, or about €214 a month on Hetzner.
  - Hetzner breaks even with Deepgram multilingual (about $0.43/h, see below) at roughly 500 meeting-hours a month.
- **Weaknesses:**
  - Latvian accuracy is worse: large-v3 has about 18–24% WER on FLEURS ([Canary/Parakeet paper](https://arxiv.org/pdf/2509.14128); [ElevenLabs comparison](https://elevenlabs.io/speech-to-text/latvian)).
  - No Wolof ([Whisper tokenizer](https://github.com/openai/whisper/blob/main/whisper/tokenizer.py)).
  - Streaming Whisper re-decodes the audio, giving about 3 s of latency ([whisper_streaming](https://github.com/ufal/whisper_streaming)).
  - We would have to run GPU servers, which goes against "small VPS, no GPU".
- **Strength:** it is the same engine as the Mac app, so the existing glossary prompt (`initial_prompt`/`hotwords`) and language picking (`pick_language`) carry over unchanged.

## Recommendation

**Primary: Deepgram Nova-3 streaming, with keyterms, sending `mip_opt_out=true` on every request.**
- **Languages:** it covers all seven must-have languages, including Latvian and Ukrainian, plus fa, vi, zh, and ne in one-language mode.
- **Glossary:** keyterms work in streaming and can be swapped mid-stream. This fits our glossary files: send `local_names` plus the top glossary terms for the meeting's languages, up to the 500-token limit.
- **Price:** the cheapest option that has no language gap and a clear per-request opt-out from training. It also gives the most self-serve concurrency (150 streams) and $200 of free credit for the bake-off.

**Fallback: Google Speech-to-Text V2 Chirp 3, in the `eu` region.**
- **Languages:** the only hosted option that lists all 12 languages, Wolof included.
- **Privacy:** no training unless we opt in, and the audio stays in the EU.
- **Glossary:** 1,000-phrase adaptation.
- **Cost:** it costs about 2.5× Deepgram, which is still well under our price.
- **Also use it:** for Wolof meetings, even while Deepgram is the primary.

**Cost per meeting-hour.** A meeting is one stream: one speaker at a time.

| Setup | Cost per meeting-hour | Share of $3.57–$5/h revenue | Cost of the free hour, per church per month |
|---|---|---|---|
| Deepgram, one speaker language | $0.0048 + $0.0013 keyterms = $0.0061/min = **$0.37/h** | 7–10% | $0.37 |
| Deepgram, two speaker languages (multilingual) | $0.0058 + $0.0013 = $0.0071/min = **$0.43/h** | 9–12% | $0.43 |
| Google Chirp 3 (fallback) | **$0.96/h** | 19–27% | $0.96 |

## Before building, test this
1. **Bake-off on our own sermon recordings** (`samples/`): Deepgram vs ElevenLabs Scribe v3 Live vs Google Chirp 3, in ru, lv and en, plus one ru↔lv switching sample. Measure:
   - word errors on glossary terms;
   - delay until final text;
   - whether `multi` follows the ru↔lv switch.

   ElevenLabs is the strongest challenger. It is the cheapest ($0.30/h with keyterms), has a very low stated Latvian WER, and has true mid-sentence switching. It is not the primary for three reasons: training is on by default, zero retention and EU residency need Enterprise, the keyterm limit is 50 × 20 characters, and the v3 Live price is unconfirmed.
2. **Confirm with Deepgram** (docs or sales):
   - that lv and uk work in streaming and in `multi`;
   - that opting out of training costs nothing extra;
   - the EU endpoint terms.
3. **Fallback for code-switching.** If Deepgram `multi` can't switch between ru and lv, run one stream per language, or use our own language picking on chunks (as `pick_language` does today). Either way the cost per meeting-hour roughly doubles.

Ruled out:
- **AssemblyAI:** no Latvian or Ukrainian.
- **OpenAI:** no language list and no detected-language output.
- **Self-hosting for now:** worse Latvian, no Wolof, and a GPU to run. Look at it again above about 500 meeting-hours a month.
