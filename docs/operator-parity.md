# Operator parity: the old web operator page vs the Mac app

The old operator page (`web/operator/index.html` + `operator.js`, what live.clntacq.com/operator served) next to the
Mac app's main window and Settings. "Before" = the Mac app at 16c0da9 (dev 0.3.0 build 12); "now" = after the
chat/hc-v3-mac change that added the Stats section and per-line delays.

## Live status (top bar of the web page)

| Web operator shows | Mac before | Mac now | Where in the Mac app |
|---|---|---|---|
| LIVE / FINISHING / STOPPED badge | partly (LIVE, no FINISHING) | partly | top bar, LIVE + elapsed time |
| Listeners: captions total (+ voice-stream count) | partly (total only) | yes, plus per language | top bar total; Stats → each language's 👤 count |
| Behind live: voice backlog per language, colour-coded | partly (a sentence over 6 s) | yes | Stats → "N s of voice waiting" per language; column header "N s behind" |
| Voice speed (max ×, which languages are condensing) | no | yes | Stats → "voice 1.10× · shortening" per language |
| Input level meter | yes | yes | top bar |
| Speech-to-text: ready/loading + engine | no | yes | Stats → Speech model (model name, Intel work-per-second, "(smaller)" after a step-down; tooltip says why) |
| Claude state per language (+ "busy N s") and model | partly (state only) | yes | Translating section (state); Stats → Translator (model), "translating for N s" per language |
| Claude usage: turns, tokens in/out, $ | no | yes (per meeting) | Stats → Translator: requests, tokens; $ only with the congregation's own key |
| Ollama state | n/a (not in the app) | n/a | — |
| Public-link tunnel state | n/a (app uses hearchurch.com) | n/a | top bar connection badge covers hearchurch.com |
| Microphone warning banner | yes | yes | red banner at the top |

## Meeting summary (above the web table)

| Web operator shows | Mac before | Mac now | Where |
|---|---|---|---|
| Caption delay after the speaker pauses, p50 / p95 per language | partly (one median, all languages) | yes | Stats → per language "captions 2.1 s (95%: 4.0 s)"; transcript header keeps the overall median |
| Audio (voice) delay, p50 / p95 per language | no | yes | Stats → per language "voice 3.2 s (95%: …)" |
| Translator used, counts per language (claude / ollama / none) | no | yes | Stats → per language, only when something other than Claude/original made lines |
| Chunks (lines) count | partly (shown lines, max 300) | yes (whole meeting) | Stats → Heard: lines and minutes of speech |
| Dropped audio count | no | yes | Stats → per language "N voice skipped to catch up" |
| — (not on the web page) skipped lines: no words / behind on Intel | no | yes | Stats → Skipped |

## Per line (the web chunk table)

| Web operator shows | Mac before | Mac now | Where |
|---|---|---|---|
| #, audio length, cut reason, speech-recognition ms (tooltip) | no | yes | tooltip on the time above each original line |
| Heard text, "skipped — why" | yes | yes | Original column |
| Translated text per language, status (skipped/merged/failed), note | yes | yes | language columns |
| Engine badge (claude/ollama) | partly ("Original" for pass-through) | yes (tooltip) | tooltip on the line's delay |
| MT ms, voice ms | no | yes (tooltip) | tooltip on the line's delay |
| Caption delay of this line | no | yes | under each translated line: "2.3 s behind" (orange > 8 s, red > 15 s) |
| Audio delay of this line / "audio dropped" | no | yes | same line: "· voice 4.1 s" or "· no voice" |
| Condensed, speed-up × | no | yes (tooltip) | tooltip on the line's delay |

## Controls (web sidebar)

| Web operator has | Mac | Note |
|---|---|---|
| Source: browser mic / server mic / test recording, upload | partly | Mac uses its own microphone only (test recordings are a developer tool) |
| Input device, channel, gain | yes | Sound section |
| Save the audio to logs/ | no | not needed for meetings; reports are written every meeting |
| Translate-into switches (live) | yes | Languages section during a meeting, large selector before |
| Start / Stop | yes | top left |
| Play a test sentence | yes | Listeners section and Settings |
| Server info (running since, version), restart server | partly | Restart engine appears when it failed; the version is in the standard About Hear Church window, not in the main window |
| Engine (faster computer) link | n/a | the Mac is the engine |
| Speaker password | n/a | hearchurch.com handles listeners |
| Voice per language + speed, compare samples | yes | Settings → Languages |
| Rate, volume | yes | Settings |
| Translator mode, Claude model, fallback timeout, Ollama model | partly (Claude model) | no Ollama in the app |
| Claude access (token / key) | yes | Settings → Claude API key (own-key congregations) |
| Listener link, QR, printable sign | yes | Listeners section, pinned bottom left |
| Glossary editor | yes | Settings → Glossary |
| Log | yes | Transcript → Activity |

## Engine-side support added

- Every transcript line carries `meeting` (the id of the meeting it was heard in); `/ws/operator` sends
  `{"type": "meeting", "meeting", "speakers", "targets"}` when a meeting starts; `/api/status` has
  `pipeline.meeting_id`. The Mac app and the web page start an empty transcript on a new meeting and ignore lines
  still finishing from the previous one.
- `pipeline.meeting_stats` (`app/metrics.py` `live_stats`): lines, skips, seconds of speech, per language
  captioned / aired / dropped / failed / sped up / shortened, translator counts, caption and voice p50/p95, and
  Claude usage since the meeting started. Also `pipeline.stt_model` and `pipeline.stt_stepped_down`.
- Listeners per language: the hub's `listeners` message now has `langs: {code: [stream, captions]}` (needs the hub
  deployed from this branch; until then the Mac shows per-language counts only for "This Wi-Fi only").
