# Riga 2, Sunday Nov 15 2026: why the hub saw so little translation and metered 32 s

Congregation `riga-2` (billing `credits`, device 2 "Crystal's MacBook Pro", Intel, Hear Church 0.3.0 build 19 =
DMG built from `018dcd1`). Crystal says the meeting "mostly worked". The hub saw 20 Claude calls and metered 32 s.
Investigated Nov 16 from the hub only (DB copy, journal); Crystal's engine log was not available.

## What the hub recorded (all times UTC; Riga = UTC+2; meeting listed for 11:30 Riga = 09:30 UTC)

The hub ran one process all morning (PID 3472300, started 05:16:47 on `02bde27`, restarted next at 14:29:38).
Billing was `credits` in the DB snapshots from 05:16, 13:56 and 20:38. 60 free minutes left, so `can_start` was true.

| session | code | live (UTC) | offered | speech rows | metered | Claude calls in the window |
|---|---|---|---|---|---|---|
| 8  | 20261115-093247 | 09:32:47 – 09:39:13 | en | 7 (seq 1–6, 9) | 31.9 s | **0** |
| 9  | 20261115-094020 | 09:40:20 – 09:42:10 | en | 0 | 0 | 0 |
| 10 | 20261115-095228 | 09:52:28 – 09:53:38 | en | 0 | 0 | 0 |
| —  | none | **09:53:38 – 11:25:07** | | | | 0 |
| 11 | 20261115-112507 | 11:25:07 – 11:44:56 | en | 0 | 0 | **20** (11:42:22 – 11:44:47) |
| 12 | 20261115-115228 | 11:52:28 – 11:53:43 | en | 0 | 0 | 0 |

- All five sessions were ended by the Mac (`session_stop`): no "engine link lost" lines, so no drops or resumes.
- Device `last_seen` 13:43:42 UTC: the Mac stayed connected (or reconnected) long after session 12.
- No hub session at all for 1.5 h, 11:53 – 13:25 Riga, i.e. most of the 11:30 meeting block.

**The hub's log does count every proxy call.** Calibration from the same day: Harlem 1st (credits) session 15
metered 392 chunks and the journal has 749 `POST https://api.anthropic.com/v1/messages` lines for 15:00 – 17:10
(two languages). So "only 20 calls" is real: Claude translated through the hub for about 2.5 minutes all day.

## Ruled out (with the evidence)

| possibility | verdict |
|---|---|
| Proxy calls not logged | No: every successful call logs an httpx line (calibration above). Refusals were *not* logged (fixed below), but no refusal condition applied in session 8: session live on device 2, credit left, server key set, same process that served session 11. |
| Reports dropped (unknown session code) | No: session 8's report was stored under its own code; codes are unique per session; no `unknown` path can drop a live session's report. |
| Clock/code mismatch, sessions restarted so reports went to an old code | No: codes match their start times; no resume; no hub restart during the morning. |
| Billing mode changed / out of credit / "internal" | No: `credits` in every snapshot; 60 free minutes left. |
| Cached translations | No: build 19 has no translation cache; in the app (mode `claude`, no Ollama) a sentence is either translated by Claude or skipped. |

## Where the translation could have gone: every path in build 19, ranked against the timeline

1. **The speech was recognised as English, so it was passed through, not translated (most likely, for most of the
   day).** The meeting offered English only. A chunk whose recognised language equals the channel's language is
   passed through: no Claude call, and not metered by design (`pipeline._stt_stage`: "one passed through is not a
   translation"). This fits sessions 9, 10, 12 and the first 17 minutes of 11 (no calls, no meter).
   Two sub-cases the hub cannot tell apart:
   - the speakers really spoke English, or
   - the speaker languages were set to English only while people spoke Latvian/Russian: speech recognition is
     then forced to English (`_recognise` detects among the speaker languages only) and Whisper writes its own
     rough English rendering. That "mostly works", costs Hear Church nothing in Claude, and is never billed.
   Session 11's burst of 20 calls fits a stretch recognised as another speaker language (e.g. someone speaking
   Russian/Latvian with that language among the speakers).

2. **Listeners on the Mac itself in an online meeting were never metered (a bug, fixed).** In an online meeting
   the meter counted only phones on the hub (`heard` from the hub's `listeners` message). The Mac's own listening
   page and stream (always allowed on the Mac itself) were ignored, although that translation goes through the
   hub's proxy on Hear Church's key. A Wi-Fi-only meeting already counted them (`_local_heard`). This fits
   session 11 exactly: 20 proxy calls, 0 s metered, i.e. no phone on the hub's English page at the time; whoever
   heard it heard it on the Mac (captions on a projector, or the voice on the Mac's speakers).

3. **The Mac translated with its own key (fits session 8 only, needs a key in the Mac's Keychain).** At start-up
   the app picks the API key or Claude token stored in the Keychain if there is one (`app_backend()`), and only
   switches to the hub when `GET /api/hc/billing` answers on connect (`_learn_billing`). If that one request fails
   the Mac keeps translating on the stored key until the next reconnect: no proxy calls, but chunks are still
   metered. Session 8 is the only one with metered chunks (so: non-English speech, a phone on the hub) and no
   proxy call. The other explanation for session 8 is that its 7 sentences were not translated at all on the Mac
   (translator not ready or erroring), which no hub data can show.

4. **A meeting ran on the Mac with no hub session (09:53 – 11:25 UTC).** Either no meeting was running, or the Mac
   was refused (`session_refused` was not logged on the hub: fixed below) and carried on locally. A refused
   meeting gets `409 not_live` from the proxy for every sentence, so only passed-through English would have
   worked in that time. Not a billing leak, but it would explain "mostly".

Paths that exist but did not apply on Sunday: a link drop (chunks during a drop aren't metered while the proxy
still serves the 60 s grace + 120 s trailing window), and a hub-ended session (120 s trailing window unmetered).

**What settles it:** Crystal's `~/Library/Application Support/Hear Church/logs/engine-output.log` for Nov 15.
Look for `#N: now lv|ru|en` (recognised languages), `claude API session` vs `claude hub session` and
`billing credits: translating through hub (was api)` (path 3), `billing not known yet`, `Translator start failed`,
`hub refused the meeting` (path 4), and `live on hearchurch.com` lines between 11:53 and 13:25 Riga time.
Also worth asking Crystal: which speaker language(s) were set, and how people listened (phones on
hearchurch.com, or the Mac's screen/speakers).

## Fixed (branch `chat/hc-v3-mac`, not deployed, not in a build)

- **Mac (`app/engine.py`):** an online meeting now meters the languages heard on the Mac itself as well as the
  hub's phones (`_speech` uses `heard ∪ _local_heard()`; Wi-Fi-only unchanged). Takes effect for Macs on the next
  build; build 19 Macs keep under-metering this case.
  Test: `tests/test_metering.py::test_an_online_meeting_also_meters_listeners_on_this_mac` (fails on the old code).
- **Hub (`app/hear/proxy.py`):** every `/api/hc/translate` refusal is logged at INFO with congregation, device,
  status and error code (never the text); successful calls now log at INFO with the congregation, model, size,
  tokens and time (they were DEBUG, so calls could only be counted through anonymous httpx lines).
  Test: `tests/test_proxy.py::test_every_translate_answer_is_logged_with_its_congregation`.
- **Hub (`app/hear/live.py`):** a refused `session_start` is logged with the device and reason.

## For Harrison to decide

- Passed-through speech is free. A meeting whose speaker language is set to English only, with people speaking
  another language, gets Whisper's English for nothing and never shows on the hub. Bill it, warn the operator
  when the detected speech doesn't look like the set language, or accept it?
- The Mac keeps a stored API key as its translator until `/api/hc/billing` answers: retry that request (rather than
  wait for the next reconnect) if a credits congregation should never translate on a leftover key.
- Translation for languages nobody hears is paid by the hub and never billed (by design).
