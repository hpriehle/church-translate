# V3.1 languages: any-to-any, several speaker languages, Latvian

Status: engine side built on `chat/hc-v3-engine` (V3.1). Hub/listen page = V3.2, Mac app UI = V3.3.

## What changes

Before: one speaker language (`stt.language`, 4 choices), output languages assumed "from English"; English was
switched on automatically whenever the speaker wasn't speaking English, and the output in the speaker's own
language paused ("listen in the room").

Now a meeting is two sets:

- **speakers**: the languages people may speak at the microphone. Any of the 12 except Wolof (Whisper has no
  Wolof): fr, es, ru, pt, uk, fa, ne, vi, zh, en, lv.
- **targets**: the translation languages = the pills on the listen page. Any of the 12: fr, wo, es, ru, pt, uk,
  fa, ne, vi, zh, en, lv.

Each speech chunk is recognised once. With one speaker language Whisper is told that language (as before). With
several, Whisper's language detector runs on the chunk and the most likely language **among the meeting's speaker
languages only** wins; when the previous chunk's language is close behind (within 0.15 probability) it is kept, so
a short or unclear chunk doesn't flip languages. The chunk's language is its **source** (`ChunkRecord.src`).

Every target gets every chunk:

- target ≠ source: translated (Claude) and voiced as before;
- target = source: **pass-through**: the recognised original goes out as that language's caption and is read by
  that language's voice. No translation call, no "listen in the room" pause, never billed.

So in Riga (speakers ru + lv, targets en + lv) the English listener hears English from both speakers, and the
Latvian listener hears the Latvian speaker in the original and the Russian speaker translated.

## Data model (settings, `app/config.py`)

```jsonc
"meeting": {
  "speakers": ["ru", "lv"],   // ≥ 1, each in SPEAKER_LANGUAGES
  "targets":  ["en", "lv"],   // ≥ 1, each in LANGUAGES; order = order of the listen pills
  "auto_en":  false           // legacy mode, see below
},
"church": ""                  // the onboarding church name; "" or the LDS name = Latter-day Saints
```

Persisted in `overrides.json` like every other section. Migration from an older settings file (no `meeting`):
`speakers = [stt.language]`, `targets = languages (+ "en" when the speaker isn't English)`, `auto_en = true`.

The old fields stay as **views** for the current Mac app (0.2.x) and older clients, kept in sync on every change:

- `stt.language` = `speakers[0]`;
- `languages` = `targets` without English while `auto_en` is on (the old sidebar checkboxes never showed English).

`auto_en` keeps the old behaviour for the old UI: while on, English is a target exactly when some speaker
language isn't English. Writing `languages` or `stt.language` the old way keeps the mode; writing `meeting`
switches it off for good (the new UI owns English like any other language).

## Settings / status API

- `POST /api/settings {"meeting": {"speakers": [...], "targets": [...]}}`: live, also during a meeting (switching
  a target on loads its voice and Claude session; speakers switch at the next chunk). 400 when a set is empty
  or has an unknown code. Operator or speaker password (the speak page may change languages, as before).
- `POST /api/settings {"church": "Baptist"}`: operator only. Re-reads the glossaries (see below).
- `GET /api/state` adds `meeting`, `church`, `speaker_languages` (now 11), `translation_languages` (all 12).
  `all_languages` stays the old list (no English) for the old UI.
- `GET /api/status` / operator WebSocket status add:
  - `meeting: {speakers, targets, auto_en}`,
  - `source`: the language of the last recognised chunk (null before the first),
  - `pipeline.channels[code].passthrough`: true while the last chunk was in that language.
  `speaker` stays (= `source` or `speakers[0]`) for old clients. `channels[code].paused` is now always false.
- `GET /api/packages` / `POST /api/packages/download` without `languages`: speakers ∪ targets (every heard or
  spoken language is downloaded; speech recognition is one multilingual model for all).

## Wire format to the hub (engine → hub, `app/engine.py`)

`session_start` (and the `languages` update sent when the set changes during a meeting):

```jsonc
{"type": "session_start",
 "languages": ["en", "lv"],     // as today: the targets switched on = the listen pills (old hubs read only this)
 "targets":   ["en", "lv"],     // new, same list, explicit name
 "speakers":  ["ru", "lv"],     // new
 "speaker":   "ru",             // legacy: the first speaker language that isn't a target (else speakers[0]);
                                //   an old hub tells phones in this language to "listen in the room"
 "passthrough": true,           // new: phones in a speaker language are never paused (pass-through)
 "resume": "...", "private": true?}

{"type": "languages", "languages": [...], "targets": [...], "speakers": [...]}
```

There is no separate heartbeat message today; `languages` goes out whenever the targets or speakers change.
Captions relayed to the hub (`{"type": "hub", "lang", "msg"}`) carry `msg.src` (the chunk's source language) and
`msg.original: true` on a pass-through caption, so the listen page can mark "original" later.

Listener WebSocket `init` (local pages, and relayed status on the hub) adds `speakers`, `passthrough: true`.

V3.2 (hub) must: accept `lv` in its language list (name "Latviešu"), read `targets`/`speakers`, stop the
"listen in the room" pause when `passthrough` is true, and show pills = `targets`. The local listener page in this
repo already does the last two.

## Billing (unchanged in principle)

The meter tap gets, per chunk, the targets that **translated** it (pass-through excluded) and bills those that
someone listens to, exactly as before: seconds of speech × languages heard. A Latvian listener isn't billed for the
Latvian speaker, only for what was translated into Latvian.

## Glossaries by church

Glossary files keep their format (`terms`/`names`/`books` with `en` + the language's form in `fr`). Each entry is
**Christian** (shared by all churches) or **LDS** (Latter-day Saint only). An entry is LDS when it has
`"scope": "lds"`, or, without a `scope`, when its English key is in the built-in list `app/glossary_scope.py`
(LDS terms like "sacrament meeting", "Relief Society", "stake"; Book of Mormon / D&C / Pearl of Great Price names
and books). That list also covers glossaries already copied to a Mac before this change.

- church = LDS (or not set): Christian + LDS entries, prompts as today.
- any other church: Christian entries only, and the interpreter's prompt says "a Christian church service"
  instead of naming the Church of Jesus Christ of Latter-day Saints and its terminology.

Several sources into one target: English reads every non-English speaker language's glossary the other way round
(the V3 rush patch, now for a set); any other target X gets its own glossary plus, for each non-English speaker
language Y, "Y term → X term" pairs joined on the English key (e.g. Russian → Latvian).

## Latvian

- Speech recognition: Whisper `lv` is in both mlx-whisper (large-v3-turbo, Mac) and faster-whisper (CPU).
- Voice: Piper `lv_LV-aivars-medium` (63.5 MB), CC0 1.0 (model card + upstream
  huggingface.co/RaivisDejus/Piper-lv_LV-Aivars-medium). The only Latvian voice in rhasspy/piper-voices.
- Captions + translation: `_OTHER["lv"]` prompt; glossary `glossary.lv.json` (sources and uncertain terms
  inside the file).

## What the Mac app must change in V3.3

1. Idle main window: speaker grid (11, downloaded ones) + translation grid (12) → `POST /api/settings {"meeting":
   {...}}`; read `status.meeting` back. During a meeting the same toggles replace the sidebar checkboxes.
2. Stop sending `languages` / `stt.language` (legacy; they still work but keep `auto_en`).
3. Downloads: speakers ∪ targets (`/api/packages` default does this already); add Latvian to `OutputLanguage.all`
   (flag 🇱🇻, 64 MB) and to the speaker list.
4. Onboarding: send the chosen church to the engine (`{"church": profile.church}`) on start and when it changes.
5. Columns UI (V3.4) can key columns on `chunk.src` and `chunk.langs[code].engine == "original"`.
