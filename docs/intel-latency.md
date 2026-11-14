# Why Intel Macs fall ~60 s behind, and the fix

Nov 14, 2026. Trigger: a tester in Riga on an Intel Mac (0.2.0, Russian speaker → English) saw the
translation about 60 seconds behind the speaker.

## Short answer

Speech recognition. On an Intel Mac it runs on the CPU (faster-whisper, int8, beam 1). With the
multilingual `small` model that Russian needs, a 4-core x86 laptop CPU needs about 0.6 s of work
per second of speech when it is idle, and 0.7–1.1 s when anything else is using the CPU. Above
about 1.0, the queue of speech waiting to be recognised grows for the whole meeting. That
lag adds up chunk after chunk: about a minute behind after a few minutes. Translation (Claude, ~1–3 s per
sentence) and voice synthesis add a few seconds. They don't make the lag grow.

The fix on this branch (`chat/hc-intel-lag`, from `chat/hc-ru-en`):

1. **Step down to a smaller model** when the measured speed shows the CPU can't keep up
   (small → base, once).
2. **Catch up when behind:** skip speech said more than 12 s ago when newer speech is already
   waiting, so captions stay live instead of drifting further behind.

Apple Silicon (mlx, large-v3-turbo) is unchanged: it never measures, steps down or skips.

## Where it was measured

- **Machine:** Beelink with Intel N95 (x86_64, 4 cores / 4 threads, no hyper-threading,
  Alder Lake-N). It stands in for an Intel Mac. A 2017–2020 dual-core i5 MacBook Air/Pro is roughly
  in the same class. Older or 2-core Macs will be slower. I couldn't measure an actual Mac.
- **Load:** the Beelink also runs other jobs (ffmpeg, Home Assistant, other agents). The runs
  marked "busy" had 2–3.5 of the 4 cores busy with other work, as measured from /proc/stat. That
  is noisy, but it is also realistic: a Mac in a church is also running a browser, Zoom, OBS…
- **Audio:** LibriVox public-domain readings, cut to 16 kHz mono. Russian: "Обрыв" (Goncharov),
  180 s, plus a 300 s and a 120 s cut. English: "Purity of Heart", 180 s.
- **Method:** `bench.py` cuts the audio with the app's own Chunker/VAD, then transcribes each chunk
  the way `app/stt.py` does (int8, beam 1, temperature fallback, previous-sentence prompt). It then
  replays the timings as a live meeting: chunk *i* is ready when the speaker finishes it, and one
  worker handles chunks in order. "Lag" is the time from the speaker finishing a chunk to its text
  being ready. The e2e runs use the real app (`--file`, real pipeline, real TTS) with Claude mocked
  at a fixed 1.5 s delay, pinned to one core (`taskset -c 0`) to imitate a slow Intel Mac.
- **Accuracy:** character error rate (CER) of each transcript, compared with what
  large-v3-turbo (the Apple Silicon model) produced for the same audio.

Scripts and raw JSON: `/tmp/hclag/{scripts,out}` on the Beelink. They are temporary and not in
the repo.

## Numbers

### Speech recognition speed (RTF = seconds of work per second of speech; must stay well below 1)

Quiet machine (first pass, no other load measured):

| Speaker | Model | Threads | RTF | Max lag over 3 min | CER vs turbo |
|---|---|---|---|---|---|
| ru | tiny | 4 | 0.12 | 1.2 s | 9.0 % |
| ru | base | 4 | 0.31 | 2.7 s | 6.5 % |
| ru | **small** (what 0.2.0 uses for Russian) | 4 | 0.58 | 5.2 s | 3.4 % |
| ru | large-v3-turbo (the Apple Silicon model) | 4 | 3.58 | 443 s | — |
| en | tiny.en | 4 | 0.16 | 0.9 s | 0.4 % |
| en | base.en | 4 | 0.35 | 2.7 s | 2.4 % |
| en | **small.en** (what 0.2.0 uses for English) | 4 | 0.45 | 5.7 s | 2.4 % |
| en | large-v3-turbo | 4 | 9.45 | 1388 s | — |

Busy machine (2–3.5 other cores busy, two passes):

| Speaker | Model | Threads | RTF pass 1 / 2 | Max lag over 3 min pass 1 / 2 |
|---|---|---|---|---|
| ru | small | 4 | 0.70 / 0.62 | 29 s / 26 s |
| ru | small | 2 | 0.71 / 0.61 | 32 s / 16 s |
| ru | small | 1 | 0.79 / 1.09 | 41 s / **71 s** |
| ru | base | 4 | 0.52 / 0.54 | 5.9 s / 8.7 s |
| ru | base | 2 | 0.38 / 0.34 | 3.7 s / 3.5 s |
| ru | base | 1 | 0.51 / 1.05 | 5.3 s / 18 s |
| en | small.en | 4 | 0.81 / 1.08 | 24 s / 40 s |
| en | small.en | 1 | 1.01 / 0.93 | 50 s / 50 s |

What this shows:

- **`small` is right at the edge.** Even at an average RTF of 0.6–0.7 the lag reaches 16–32 s within
  3 minutes. Chunks are uneven: a hard chunk falls back to a higher temperature and takes 10–15 s.
  The queue drains only when the speaker pauses. Once the RTF passes 1, the lag only grows
  (71 s after 3 minutes in the worst run). That is the tester's "~60 s behind".
- **`base` keeps up** in every busy run (max lag 3.5–18 s, and the 18 s was on 1 thread with 3.5
  other cores busy). The cost is accuracy: Russian CER rises from 3.4 % to 6.5 % against turbo.
  `tiny` is fast but 9 % CER on Russian, so it is not worth it.
- **Threads don't fix it.** faster-whisper's default (4 threads on this CPU) is as good as any
  setting on a quiet machine. A short thread sweep on one chunk took 2.9 / 2.1 / 1.3 / 1.5 s with
  1 / 2 / 3 / 4 threads. Under load, 2 threads was sometimes better, but the variation between passes
  is larger than the difference. Setting `cpu_threads` = physical cores isn't a change worth
  shipping on this evidence.
- **Russian is slower than English** with the same size model (multilingual small vs small.en):
  ru small 0.58 vs en small.en 0.45 on the quiet machine.

### End to end, the real app on one core (imitating a slow Intel Mac)

Russian → English, Claude mocked at 1.5 s, pinned to one busy core.

**0.2.0 behaviour (before the fix), 5 min of speech:** speech recognition took 11.0 s (p50) per
7.2 s chunk (RTF ≈ 1.5). Captions reached phones **109 s (p50) / 198 s (max)** after the speaker
paused, and the lag grew all meeting. Voice: 43 of 44 clips dropped by the existing audio catch-up,
because they arrived too late to be worth playing.

**Before and after, same 120 s Russian clip, same core** (back to back at 21:10 and 21:17. Core 0 was
very busy at that point: load average 11–14 on the 4 cores, so speech recognition ran at RTF
≈ 2.9 with `small`. That is a worse case than the tester's Mac, which is why it is a good
test of the catch-up):

| | 0.2.0 (before) | This branch |
|---|---|---|
| Caption on phones after speaker pauses, p50 / p90 / max | **126 s / 180 s / 194 s**, growing all the way | **20 s / 29 s / 32 s**, flat |
| Chunks captioned / skipped to catch up | 19 / 0 | 10 / 9 |
| Model | small throughout | small → base after 15 chunks ("2.86s per second of speech") |
| Speech recognition per chunk, after the step-down | — | 4.4–9.0 s for 5.6–8.7 s chunks (RTF ≈ 1.0, vs 2.9 on small) |
| Skips after the step-down | — | none (the last 4 chunks were all captioned) |
| Voice clips aired | 0 of 19 (all too late) | 2 of 10 |

From the per-chunk metrics of the fixed run: chunks 1, 2, 6, 8, 10 and 13 were recognised with `small`
(14.9–18.7 s each). The chunks between them were skipped as 14–24 s old. Chunks 16–19 used `base`,
with no skips. The step-down took ~100 s to happen because it waits for 6 measured chunks, and on a
CPU this slow each one takes 15 s. On a Mac at RTF 1.0–1.5 it comes sooner in speech time, because
fewer chunks are skipped. The run log has a "speech recognition 23s behind: skipping" warning, then
"switching to the smaller base model", and no errors.

### Translation and voice

- **Translation:** not measured here, because Claude was mocked in every run on this machine. From
  the earlier real end-to-end runs, Opus at low effort takes about 1–3 s per sentence. That is a
  fixed delay per sentence, not one that grows, and it runs on Anthropic's servers, not the Mac's
  CPU. Sonnet or Haiku would save maybe a second per sentence, which is small next to 60 s. It is
  not the cause, and switching model for this reason isn't worth the quality cost.
- **Voice synthesis (Piper)** also runs on the CPU and competes with speech recognition. On the
  busy single core it took 4–9 s per clip (vs about 0.8–1.1 s on a quiet machine). The existing
  backlog policy (speed up, condense, then drop old audio above 25 s) already keeps the *audio*
  from drifting. It was the *captions* that had nothing to stop them falling behind.

## The fix (branch `chat/hc-intel-lag`)

### 1. Step down to a smaller model when the CPU is too slow (`app/stt.py`)

- Every faster-whisper chunk of ≥ 2 s records its speed. RTF is the work time over the last 8 chunks
  divided by their speech time.
- After at least 6 measured chunks, if RTF > 0.8, the next chunk loads the next smaller model:
  `small` → `base` (`small.en` → `base.en` for English). It steps down only once and never below
  `base`, because `tiny` is too inaccurate for Russian. The chunk that loads a model isn't counted.
- The step-down lasts until the app is restarted. After a restart it starts on `small` again.
- The operator page shows a warning when it happens, and the meeting report says
  "model small → base (it needed 0.98s per second of speech)".
- **Download:** the Intel speech package now also downloads `base` (+148 MB, 486 → 634 MB shown).
  It is optional: the package counts as installed without it. Macs that installed 0.2.0 don't have
  it. The first time they prove too slow, it downloads in the background and the step-down happens
  once it has arrived. If they're offline, they stay on `small` and rely on catch-up.
- Models now load from their cache folder by path. faster-whisper loads a folder named like the model
  ("base", "small") from the working directory if one exists. That broke the step-down in the first
  test run here.

### 2. Catch up when behind (`app/pipeline.py`, `app/backlog.py`)

- Before recognising a chunk, if it ended more than `backlog.stt_behind_s` = 12 s ago **and** newer
  speech is already waiting, it is skipped. The newest chunk is always recognised, so the captions
  stay within roughly 12 s plus one chunk of the speaker, whatever the CPU.
- Only for faster-whisper. Set `stt_behind_s = 0` in config.toml to turn it off.
- The operator page shows a warning (at most once a minute) and `stt_rtf` / `stt_skipped` in its
  status. The report counts "chunks skipped to catch up" apart from non-speech, and has a new row,
  "Waiting for speech-to-text (behind)".

### What the listener notices

- On a Mac that keeps up (every Apple Silicon Mac, and Intel Macs fast enough for `small`), nothing
  changes.
- On a slow Intel Mac: **some sentences are missing.** When the Mac falls behind, the sentences it
  can't get to are skipped, and captions and voice continue with what the speaker is saying now. Before
  the fix, they got every sentence, but a minute or more late, which is useless in a live service.
- After the step-down to `base` (usually 1–2 minutes into the meeting), the skipping mostly stops.
  Recognition is a little less accurate (Russian about 6.5 % vs 3.4 % character errors against
  the Apple Silicon model), which may show as an occasional wrong word in the translation.

## Not done / options

- **Hosted speech recognition on credits** (no sign-up made, prices from public pages and
  aggregators, Nov 2026): Deepgram Nova-3 streaming about $0.0048–0.0077/min ($0.29–0.46 per hour),
  multilingual about $0.0058–0.0092/min; OpenAI gpt-4o-mini-transcribe $0.003/min ($0.18/h),
  gpt-4o-transcribe / whisper-1 $0.006/min ($0.36/h). OpenAI's file-based models would add upload
  round-trips per chunk. Deepgram's streaming API fits live use. At about $0.20–0.50 per meeting
  hour, it is cheap next to the translation itself. It would also need audio sent off the Mac (a
  privacy line in the terms) and a server-side proxy for the key. Worth considering only if many
  testers have Intel Macs too slow even for `base`.
- No measurement on a real Intel Mac. The first report from the Riga tester with this build would
  confirm it: the meeting report now shows RTF, the step-down and the skip count.
- Thresholds (0.8 RTF, 12 s) are judgement calls, set from the numbers above, and easy to change.
