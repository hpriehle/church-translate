# Can the hub VPS do live speech recognition + Piper voices? (measured 2026-11-15)

**Short answer: only with the `small` Whisper model. That fits 2 meetings (3 at a push) next to the hub. Russian
is usable but rough; Latvian is not usable. The quality the Mac gives (large-v3-turbo) does not run in real time
on this box at all. For a browser version at Mac quality, rent a GPU box (Hetzner GEX45, €214/month + €209
setup). A bigger CPU box is a gamble that I could not measure.**

## Box and method

- `rr-vps`: 8 vCPU AMD EPYC 9354P (KVM, 1 thread per core, 2.0 GHz base), 31 GB RAM (≈19 GB free), no GPU. The
  live hub (`church-translate.service`) and other services run on it.
- I used the engine's own code from this branch (`app/chunker.py` Silero VAD with the `[vad]` defaults, and
  `app/stt.py` `Transcriber.transcribe` with the pipeline's prompt, the tail of the previous chunk). That means
  faster-whisper 1.2.1 / CTranslate2 4.9.0, int8, beam 1, and the same temperature fallback. Step-down was turned
  off so each model was measured on its own. Script: `scripts/bench/stt_stream.py`.
- Every run used `nice -n 19`, `taskset` (4 threads → CPUs 0-3, 8 threads → 0-7) and a fixed `cpu_threads`, in
  `/root/hc-bench`. The hub's service, venv and data were not touched.
- Audio (not in git):
  - Russian: 10 min of a real Orthodox sermon. archive.org `07.01.2014_201405` + `19.04.2014`, Public Domain Mark.
    The second recording starts at 8:38 with a noisy, hard stretch.
  - Latvian: 3 min of a sermon (archive.org `Audio2017.04.30AndisJassDievaNams`, from 25:00).
  - English: the Gettysburg Address (LibriVox, 2.6 min).
- **Lag** means: the chunk is cut → its text is ready. The listener waits this on top of the chunk itself
  (chunks are 3–10 s, 4.5 s on average here). Single-meeting runs use a virtual clock: chunk *i* starts at
  max(its cut time, chunk *i−1* done). That is exact for one meeting. Multi-meeting runs stream in real time
  (`--clock real`), so the meetings compete for the CPU the way live ones would.
- **Speech RTF** = work time ÷ seconds of chunk audio. It has to stay well under 1 during continuous speech, or
  lag grows. Pauses only give the worker a short break. RTF over the whole 10 min, pauses included, looks better
  than it is.
- Hub watch: `curl https://hearchurch.com/` every 5 s for the whole hour (755 requests). All returned 200: median
  0.10 s, p95 0.13 s, max 0.39 s (during the 4-meeting run), against a 0.12 s baseline before the start. The
  service is still `active`. (`/api/health` returns 404, so I watched `/`.)

## 1. Speech recognition, one meeting, Russian (10 min)

| model (int8) | threads | speech RTF | lag p50 | lag p90 | lag max | lag in last minute | CPU while working | RAM |
|---|---|---|---|---|---|---|---|---|
| small | 4 | **0.46** | **1.9 s** | 3.8 s | 5.6 s | 2.8 s | 2.7 cores | 1.1 GB |
| small | 8 | 0.54 | 2.3 s | 5.6 s | 12.4 s | 4.5 s | 2.9 cores | 1.1 GB |
| medium | 4 | 1.21 | 8.8 s | 38.7 s | 48.0 s | 40.2 s | 2.7 cores | 3.2 GB |
| medium | 8 | 1.24 | 7.9 s | 56.4 s | 63.9 s | 56.5 s | 3.2 cores | 2.6 GB |
| large-v3-turbo | 4 | 1.63 | 32.7 s | 62.2 s | 79.4 s | 52.2 s | 2.9 cores | 2.0 GB |
| large-v3-turbo | 8 | 1.37 | 17.3 s | 33.3 s | 48.9 s | 33.0 s | 3.9 cores | 1.7 GB |

- Every chunk costs at least ~1.6 s with small, ~5 s with medium and ~6–7 s with turbo, however short the chunk
  is, because Whisper pads to 30 s. Short chunks (20 of the 69 are under 3 s) are what sink medium and turbo.
- 8 threads barely help and sometimes hurt: one faster-whisper process only keeps ~3–4 cores busy.
- Medium and turbo fall further behind the whole time. In the engine, `stt_behind_s = 12` would kick in and
  **skip** speech that is more than 12 s old, so captions would come out with gaps instead of with growing lag.
  Either way they are not usable live on this box.
- The noisy stretch (8:50–9:10) triggers temperature fallback: up to 5 s of work per chunk on small, 14 s on
  medium.

**Accuracy by eye (same chunks):**

| | small | medium | large-v3-turbo |
|---|---|---|---|
| 0:33 | "Я битель у них совершу, я ли не буду цыганей дочеряем." | "И обитель у них совершу, и они мне будут сынами и дочерями." ✓ | "…и они не будут сынами и вечерями." |
| 1:03 | "жизнь светлеправы… Рождество Христома" | "Жизнь, свет и правда… Рождество Христома" | "Жизнь, свет и правду… Рождество Христово" ✓ |
| 1:39 | "Придите ко мне, цитрундающиеся и о временении" | "…сей груждающейся и обременение" | "…вседруждающиеся, и о временении" |
| 0:27 | "…придукнем и всем в них." | "…придут к ним и вселюся в них." | "…придут к ним и вселятся в них." ✓ |

Small gets the gist of plain sentences but garbles scripture and Church words, and gets about one chunk in three
wrong. A translator would mistranslate those. Medium and turbo are clearly better, though still not perfect on
this recording.

**Latvian (3 min) and English:**

| | threads | speech RTF | lag p50 / p90 / max | sample |
|---|---|---|---|---|
| small, lv | 4 | 0.73 | 2.3 / 3.9 / 6.2 s | "Lai **dius** piesūkāt **cilvāku**, bet es nevaru… Pies pies, čiem cilvākam" ✗ |
| large-v3-turbo, lv | 8 | 1.75 | 24.7 / 50.8 / 56.2 s | "Lai **Dievs** piesūk kādu **cilvēku**, bet es nevaru. Piespiest šiem cilvēkam." ✓ |
| small, en | 4 | 0.74 (0.34 incl. pauses) | 1.8 / 2.3 / 3.5 s | word-perfect (but the Gettysburg Address is a famous text Whisper has likely seen) |

Latvian on small is not usable ("dius" for Dievs throughout). It is also slower than Russian, because the chunks
are shorter (3.4 s on average). Turbo reads it well but at 1.75× real time.

## 2. Piper voices (same box, 4 sentences × 5 rounds; `scripts/bench/piper_speed.py`)

| voice | threads | work ÷ speech | per sentence (mean / max) |
|---|---|---|---|
| ru_RU-denis-medium | 1 | 0.083 | 343 / 407 ms |
| ru_RU-denis-medium | 2 | 0.059 | 242 / 296 ms |
| ru_RU-denis-medium | 4 | 0.052 | 216 / 311 ms |
| lv_LV-aivars-medium | 1 | 0.081 | 316 / 428 ms |
| lv_LV-aivars-medium | 2 | 0.046 | 181 / 246 ms |
| lv_LV-aivars-medium | 4 | 0.040 | 158 / 208 ms |

Voices are cheap. One listener language costs under 0.1 core on average (≈1 thread busy ~8 % of the time) and
adds ~0.3 s per sentence. Even 3 voice languages per meeting add only ~0.25 cores. Piper is not the bottleneck.

## 3. How many meetings fit (small, Russian, real-time, all at once)

| meetings × threads | speech RTF each | lag p50 | lag p90 | lag max | lag in last minute |
|---|---|---|---|---|---|
| 1 × 4 | 0.46 | 1.9 s | 3.8 s | 5.6 s | 2.8 s |
| 2 × 4 | 0.47–0.48 | 1.8–1.9 s | 3.2–3.6 s | 7.4–7.6 s | 2.1–2.3 s |
| 4 × 2 | 0.64–0.67 | 2.8–2.9 s | 5.8–9.9 s | 11.8–20.9 s | 5.0–9.8 s |

Lag here comes on top of the chunk length, and translation time (not measured here) comes on top of that.
Allowing for both, **< 8 s end-to-end lag holds for 2 meetings with voices; 3 is borderline; 4 is over**
(p90 up to 10 s, the worst chunk 21 s). All the speakers in these runs talked at the same moment, which is the
worst case. Meetings on Sunday mornings largely overlap anyway. RAM is no limit: ~1.1 GB per meeting.

With medium or large-v3-turbo the answer is **0 meetings** at acceptable lag on this box.

Two Russian meetings take about 1.7 of the 8 cores on average, and up to ~7 cores in bursts. The hub stayed
fast because the bench ran at nice 19. A production setup would need the same low priority, or a core limit
(systemd `CPUQuota`/`AllowedCPUs`), so a busy Sunday can't slow down the hub.

## 4. Recommendation

1. **This VPS as is:** fine for a **trial or demo** of the browser version: 2 simultaneous meetings, small model,
   English and Russian (rough), not Latvian. It is not Mac quality, so I would not sell it as the same product.
   Cost: €0 extra.
2. **Bigger CPU box (estimate, not measured):** a desktop-class Ryzen with AVX-512 and fast single cores runs
   Whisper roughly 1.5–2× faster per core than these 2.0 GHz EPYC vCPUs. That might bring large-v3-turbo near
   real time with 8 threads, about 1 meeting per 8 cores (turbo needs ~1.4× this box's speed just to break
   even, and more for headroom).
   - Hetzner **AX102**: Ryzen 9 7950X3D, 16 cores, 128 GB, **from €159/month + €129 setup** (excl. VAT). Perhaps
     2 turbo meetings or ~6–8 small meetings. https://www.hetzner.com/dedicated-rootserver/matrix-ax/
   - Hetzner **AX162**: EPYC 9454P, 48 cores, 128 GB, **from €319/month + €304 setup**. Same per-core speed
     class as this VPS, so the scaling is mostly linear: ~10–12 small meetings, turbo still doubtful.
     https://www.hetzner.com/dedicated-rootserver/matrix-ax/

   Both would have to be benchmarked before committing, and Hetzner bills monthly. The per-chunk overhead (~6 s
   of work per chunk for turbo on these cores) is the thing to beat.
3. **GPU box (recommended if the browser version is to match the Mac):** Hetzner **GEX45**: RTX PRO 4000
   Blackwell 24 GB, i5-13500, 64 GB, **€214/month + €209 setup** (excl. VAT).
   https://www.hetzner.com/dedicated-rootserver/matrix-gpu/ A GPU of this class usually runs large-v3-turbo
   (faster-whisper, float16) far faster than real time, so 10+ simultaneous meetings at Mac quality is a
   reasonable expectation. **This is not measured** (no GPU available without signing up). The CPU would carry
   Piper. It would also need a CUDA build of CTranslate2, a small change from today's CPU path.

Not covered: translation (an API call per chunk today; that is a per-use cost unless it moves to a local model
too), browser audio upload, and Latvian on medium (skipped: medium already can't keep up with Russian).

## Reproduce

```
python3 -m venv venv && venv/bin/pip install faster-whisper piper-tts onnxruntime numpy pydantic
# from a checkout (needs app/ and models/silero_vad.onnx); audio as 16 kHz mono wav
nice -n 19 taskset -c 0-3 venv/bin/python scripts/bench/stt_stream.py --audio ru.wav --lang ru --model small --threads 4 --out r.json
nice -n 19 taskset -c 0-3 venv/bin/python scripts/bench/piper_speed.py --voices voices/ --threads 1 2 4
```

Raw per-chunk results (JSON + logs, with transcripts) are on the VPS in `/root/hc-bench/results/`.
Everything else there (venv, models, audio, code) has been deleted.
