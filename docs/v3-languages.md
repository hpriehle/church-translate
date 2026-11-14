# V3.1 languages: any-to-any, several speaker languages, Latvian

Status: engine side built on `chat/hc-v3-engine` (V3.1). Hub, listen pages and accounts on `chat/hc-v3-hub`
(V3.2). Mac app UI on `chat/hc-v3-mac` (V3.3, see the end).

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

### What the hub does with it (V3.2, `app/hear/live.py`)

- Pills = `targets` (or `languages` from a Mac without `targets`), in that order, limited to what the Mac's
  `hello.languages` can produce. Same list on the congregation's page (`hearchurch.com/<slug>`), in
  `/api/hc/c/<slug>`, and on `/listen` (live rows show the pills; `/api/live` and `/api/hc/search` give
  `languages`). `lv` shows as "Latviešu".
- Listener `init` adds `speakers` and `passthrough`; a `speaker` message to phones always carries `speaker`,
  `speakers`, `passthrough`. With `passthrough: true` the page never shows "listen in the room".
- A `languages` update with a new `speakers` list sends phones a `speaker` message, then `languages`.
- Old Macs (0.1.x/0.2.x: `languages` + `speaker`, no `passthrough`) work as before: pills = `languages`, and the
  phones in the speaker's language are told to listen in the room.

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

## Accounts on the hub (V3.2): API for the Mac app (V3.3)

A congregation has 1 to 10 e-mail addresses, exactly one of them **main** (billing: Stripe Checkout gets it as
`customer_email`, so receipts go there). Macs are devices with their own token (`Authorization: Bearer hcd_…`).
All bodies are JSON. Errors are `{"error": "<code>", "message": "…"}` unless noted.

### First launch: "Create congregation"

`POST /api/hc/signup` as before. `email` is the **main** address (required); `emails: ["…"]` (optional, up to 9)
adds more. The answer adds `emails: [{email, main, added_at}]`. Old apps send only `email`: still works.

### First launch: "Connect by code"

`POST /api/hc/pair {"code": "7KQM-X2PA", "device_name": "Clerk's Mac"}` →
`{ok, device_token, slug, congregation, billing}`. Takes a code from "Add device" or from the e-mail; case,
spaces and the dash don't matter. `403 bad_code` (unknown, used, expired, or the congregation is hidden),
`429 {"error": "locked", "retry_after": s}` after 10 wrong codes from one IP in 15 minutes. Codes are 8 characters
from `ABCDEFGHJKMNPQRSTVWXYZ23456789`, work once, for 15 minutes.

### First launch: "Email me a code"

`POST /api/hc/email-code {"email": "…"}` → always `{ok: true, message}` (the same whether or not the address
belongs to a congregation). If it does, a code goes to **every** address of that congregation (one e-mail each,
from "Hear Church <no-reply@hearchurch.com>"). The Mac then shows a code field and calls `/api/hc/pair`. A new
request replaces the previous e-mailed code. `503 not_configured` while the hub has no `RESEND_API_KEY` (the app
should then suggest "Add device" on a connected Mac). `429` after 3 requests per address or 10 per IP in an hour.

### Settings: "Add device"

`POST /api/hc/pairing-code` (device token) → `{ok, code: "7KQM-X2PA", expires_at, expires_in: 900}`. Show the
code with a 15-minute countdown. A new code replaces the previous unused one. 30 per hour per congregation.

### Settings: e-mails

- `GET /api/hc/account` (device token) → `{ok, slug, main_email, emails: [{email, main, added_at}],
  devices: [{id, name, created_at, last_seen, app_version, revoked_at, this}]}` (main first; `this` = the Mac asking;
  revoked Macs aren't listed).
- `POST /api/hc/emails {"email": "…", "main": false}` → add (`main: true` also makes it main). Answer:
  `{ok, added, emails, main_email, devices}`. `409 too_many` past 10, `422` for a malformed address.
- `POST /api/hc/emails/main {"email": "…"}` → make one of them main; `404 not_found` if it isn't one.
- `POST /api/hc/emails/remove {"email": "…"}` → `409 main` (make another main first), `404 not_found`.

### Settings: devices

`POST /api/hc/devices/revoke {"id": 12}` (device token, added for V3.3) → revoke another Mac of the same congregation:
its token stops working and a meeting it runs ends (as the admin's revoke). Answer: `{ok, emails, main_email, devices}`.
`409 this_device` for the Mac asking (it signs out instead), `404 not_found` for an id that isn't one of the
congregation's Macs.

Every one of these answers 403 for an unknown or revoked device token; the app should then go back to first launch
("Create congregation" / "Connect by code"). The older routes (`/api/hc/connect-code` + `/api/hc/connect` with a
6-digit code and the slug, `/api/hc/device-link` magic links) still work for 0.2.x apps; V3.3 should use the ones
above.

## The Mac app (V3.3, `chat/hc-v3-mac`)

- First launch: "Create a congregation" (church → name → place → times → languages → microphone → credits or own-key
  code → e-mails → sign-up) or "Connect to my congregation" (a code from Add device, or "Email me a code" → code →
  `/api/hc/pair`, then languages → microphone → credits/key). The permission step is gone (inferred; `approved` is
  set on sign-up). The old 6-digit connect code is no longer shown or asked for.
- The meeting lives in `profile.json` as `speakers` / `targets` and goes to the engine as
  `{"meeting": {...}}` when the engine starts and on every toggle (live during a meeting). A profile from 0.2.x
  takes the engine's migrated meeting once (`meetingPicked`). The church goes to the engine (`{"church"}`) on every
  engine start and after connecting. `languages` / `stt.language` are no longer sent.
- Language grids (`LanguageSelector.swift`): all 11 speaker and 12 translation languages, each marked ready,
  downloading or "Download N MB"; switching one on downloads it (during a meeting a language that isn't downloaded
  only downloads, and is switched on once ready). Large above Start meeting before a meeting, small in the sidebar
  during one.
- Transcript: one column for the original (with the chunk's `src`) and one per target; pass-through cells say
  "Original".
- Settings → Congregation: e-mails (add, remove, make main), devices (revoke others), Add device (code + countdown).
  A 403 from `/api/hc/account` (at launch or in Settings) signs the Mac out, back to first launch.
