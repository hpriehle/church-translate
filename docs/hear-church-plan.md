# Hear Church — plan

Turn church-translate into **Hear Church**: a Mac app anyone can download that runs the meeting's
translation on their own Mac and streams it through hearchurch.com, where listeners find their
congregation by location.

Status (2026-11-02): Phases 1–3 built on branch `hear-church-phase2` (74 tests pass), not pushed or
deployed. Phase 3's app builds, bundles the engine and publishes a test meeting to a local hub; the
direct API translator has only run against a fake client so far (no API key on this Mac). Next:
a real-key test and a full click-through of onboarding, then Phase 4.

Working notes for whoever continues:
- Build in a separate worktree (`~/dev/hear-church` on the Mac), never switch branches in
  `~/dev/church-translate`: the Mac runs the live ward's server from that synced folder.
- `~/dev` syncs between Beelink and Mac including `.git`: run git on one side only.
- Phase 1–2 code: `app/hear/` (db, api, live, mail, geo), `PublishClient` in `app/engine.py`,
  `web/find/`, listener changes in `web/listener/`, tests in `tests/test_hear.py`.
- The Mac app talks to: `POST /api/hc/signup` → email link → `POST /api/hc/claim` (device token),
  `POST /api/hc/device-link` (add a Mac), `POST /api/hc/location` (confirm pin; show pin screen
  when signup returns `located: false` or this returns 422), and `/ws/engine` with
  `engine.hub_url` + `engine.device_token` (PublishClient).
- Phase 3 code: `mac/` (Swift, `xcodegen` spec in `mac/project.yml`, `mac/build.sh` builds
  `mac/build/Hear Church.app` with a standalone Python + the engine inside, ~1.6 GB, torch is most of it).
  Engine side: `python -m app --app` (localhost only, Claude via `app/translate/api_session.py` with the
  Keychain key in `ANTHROPIC_API_KEY`), `HEARCHURCH_HOME` for writable files, `HEARCHURCH_HUB_URL` /
  `_DEVICE_TOKEN` / `_LISTENER_URL` env overrides, `/api/status`, `/api/packages` (`app/packages.py`).
  The hub now sends `{"type": "listeners"}` to the publishing Mac.
- Testing the app: `defaults write com.hearchurch.HearChurch HubURL http://127.0.0.1:<port>` points it
  at a local hub; `defaults write com.hearchurch.HearChurch EngineDir ~/dev/hear-church` runs the engine
  from the checkout with `uv run` instead of the bundle. App data: ~/Library/Application Support/Hear Church.
- Phase 3 left for later: auto-stop after ~10 min of silence, optional auto-start at meeting time,
  moving the building pin after setup (`/api/hc/location` exists), screenshots in the API-key guide,
  the "free first Sunday" button (disabled until Phase 5).
- Still open before launch: an email service (SMTP settings; today mail goes to logs/outbox.log),
  trial limits (Phase 5), deploy to the VPS.

## Decisions so far

- **Market:** mainly the USA. Direction is always English → listener language (French, Spanish,
  Russian, Wolof). No source-language setting for now.
- **Sign-up is open:** new congregations go live after email verification, no manual approval.
- **Claude access:** bring your own Anthropic API key. No credit sales for now, so no Stripe and no
  sales tax.
- **Free trial:** the first Sunday runs on Harrison's key through the VPS. After that the app asks
  for the congregation's own key.
- **No listener PIN.** Anyone within the radius can join; PINs add too much friction.
- **Open source:** the Mac app stays MIT in this repo.
- **Free trial = one full Sunday:** up to ~3 hours, all the congregation's languages (about $6–8 of
  Harrison's key each). After that the operator must add an API key (or, later, pay).
- **After the trial, every congregation chooses: pay or bring their own key.** Same rule for all
  churches (no religion-based pricing). Own key is free; paying buys credits on Harrison's key
  through the VPS metered proxy (the same path as the trial). Credits ship after the own-key
  launch; build the proxy so they plug in without changes.
- **Links work anywhere:** the 1-mile radius only decides what the nearby list shows. A QR code or
  direct link opens the session from anywhere (members at home, family far away).
- **Launch languages:** French, Spanish, Russian and Wolof (SpeechT5 default; MMS optional, see
  Licensing).
- **Repo:** keep building in church-translate; rename the GitHub repo to hear-church at launch.
- **Mac does the work, server relays:** every congregation's translation (speech recognition,
  Claude, voices) runs on their Mac. hearchurch.com always carries the stream to listeners but
  never translates for new congregations. The existing ward keeps today's server fallback (phone
  mic via /speak) until it runs on the app.
- **Recording:** local opt-in only. The operator can save a transcript on their own Mac; nothing
  is ever kept on the server.
- **No staging server:** test locally, then deploy straight to hearchurch.com, never on a Sunday.
- **Defaults** (unless changed): operator sign-in by email magic link; the current ward moves in
  as congregation #1 with today's links still working; the nearby list shows live sessions plus
  ones starting within 30 min; church dropdown = Latter-day Saints, Catholic, Baptist, Methodist,
  Non-denominational, Other; support email on hearchurch.com; Apple Silicon, macOS 14+.
- **Distribution:** signed and notarized download from hearchurch.com (Apple Developer account
  exists). Not the App Store.
- **Updates:** Sparkle.
- **No Claude Code / subscription sign-in** in the public app. Using a personal Pro/Max sign-in
  inside a distributed app goes against Anthropic's terms. The app calls the API directly.
- **No Ollama fallback** in the public app (15–30 GB). If Claude fails, the app shows
  "translation paused" and retries.

## How it fits together

```
 Mac app (Hear Church)                          VPS (hearchurch.com)
 ─────────────────────                          ────────────────────
 mic / soundboard                               accounts: congregations, devices, tokens
   → VAD → Whisper (mlx, local)                 session registry (who is live, where)
   → Claude API  ── own key (direct)            listener pages + location lookup
                 └─ trial: via VPS /trial proxy  caption + audio fan-out per session
   → Piper / SpeechT5 voices (local)
   → captions + MP3 ── one WebSocket ──────────► /ws/engine?session=…
```

The existing engine ↔ hub protocol (`app/engine.py`) already does most of the link: the Mac
connects out with a bearer token, and the hub fans captions and MP3 out to phones. What changes:

- Today the hub sends audio *to* the engine. In Hear Church the Mac's own microphone is the
  source, so the engine starts the meeting itself and the hub only relays.
- Today there is one hub, one engine and one meeting. It becomes many congregations, each with
  its own sessions, streams and caption rooms.

## User journeys

### Operator (first time)

1. Download the DMG from hearchurch.com, then drag the app to Applications.
2. **Church:** a dropdown (The Church of Jesus Christ of Latter-day Saints, plus a few common ones)
   with free text for others.
3. **Congregation name.** For the Latter-day Saint church the label reads **Ward / Branch** (small
   units are branches) and asks for the stake (optional). Other churches see "Congregation".
4. **Building address:** typed address, or "use this Mac's location". Either way it's geocoded and
   shown as a pin the operator confirms or drags. The building's location is stored, not the Mac's.
   Mac location comes from Wi-Fi and can be far off.
5. **Meeting time(s)**, e.g. Sunday 10:00. Used for the listener list ("starts 10:00") and for
   optional auto-start.
6. **Languages:** French, Spanish, Russian, Wolof, plus "Request another language" (a form that
   emails Harrison). Each shows its download size (below). Downloads run in the background with
   progress, then resume if interrupted.
7. **Claude:** "Start with a free first Sunday" or "Add your API key". The key screen has a
   step-by-step guide with screenshots (console.anthropic.com → Billing → API keys), a test call on
   paste, and a suggested monthly spend limit. The key is stored in the macOS Keychain.
8. **Confirm:** "My bishop / pastor has approved streaming this meeting" (checkbox, see Church
   policy).
9. → Operator page.

### Operator (every Sunday)

Open the lid → the app is already open (launch at login) → one big **Start** button.

- **Preflight before Start:** mic permission granted, input level moving, on power (warn if on
  battery), internet up, key valid, languages downloaded.
- **During the meeting:** live captions per language, input level, listener count, connection
  status.
- **Auto-stop** after about 10 minutes of silence, so a forgotten session doesn't keep calling
  Claude all afternoon.
- **Optional:** auto-start at the scheduled time.

### Listener

1. Go to hearchurch.com, or scan the QR code in the chapel. **The QR code stays the fastest path**:
   it opens that congregation's page directly.
2. "Find my congregation" asks for location in the browser. Live sessions within 1 mile are listed
   with distance, name and start time, live ones first. If none are found, widen the search (rural
   branches). If location is declined, search by name.
3. Pick the congregation → its offered languages → listen as today.

## Location details

- **Several wards share one building**, with staggered and sometimes overlapping times. Three
  wards can all be 0.0 mi away, so the list must separate them by **name + start time + live
  badge**, not distance.
- **Privacy:** the server returns live sessions inside a rough bounding box, and the phone computes
  exact distances. Listener positions are never stored or logged. Say so on the page.
- **Geocoding:** one call per congregation at setup (Mapbox or Google; Nominatim's free tier has
  strict usage rules). Cheap at this volume.
- Indoor phone GPS is accurate to about 100 m, which is fine for a 1-mile radius.

## Server changes (VPS)

- **Data:** SQLite to start.
  - `congregations` (church, name, unit type, stake, lat/lng, address, meeting times, approved?)
  - `devices` (congregation, token, last seen)
  - `sessions` (congregation, languages, started/ended, trial?)
- **Sign-up API** used by the app's onboarding: create congregation → device token. Lets a ward add
  a second Mac (people take turns bringing the laptop): sign in by email link, then add a device.
- **Session routing:**
  - `/ws/engine` takes the device token and opens a session for that congregation.
  - Listener URLs become `/c/<slug>` (keep today's URL working for the existing ward).
  - Captions and MP3 are fanned out per session and language.
- **Nearby endpoint:** `GET /api/live?lat&lng` returns live sessions in a box (rounded coordinates,
  never logged).
- **Trial proxy:** `POST /api/trial/translate` holds Harrison's key server-side.
  - Limits: one trial per congregation (email-verified), one Sunday: up to ~3 hours, all its
    languages. Meters usage per congregation so paid credits can reuse it later.
  - Rate-limited. The key never reaches the app.
- **Sign-up is open:** congregations go live once their email is verified. Add a simple admin list
  page to hide or remove a bad entry after the fact.
- **Capacity:** most meetings start between 9 and 1 on Sunday, so plan for peak, not average.
  Each listener is about 64 kbps of MP3, so 100 listeners on one language is about 6.4 Mbps.
  Check the VPS bandwidth plan, and add a CDN or relay later if needed.

## Mac app

- **Shell:** a small Swift app (menu bar icon + window) that runs the bundled Python engine as a
  child process and shows the existing operator web UI in a WKWebView. This reuses the operator
  page and keeps the native part to:
  - onboarding
  - Keychain
  - sleep prevention
  - Sparkle
  - launch at login
  - mic permission
- **Bundle:** Python 3 + uv-locked dependencies + ffmpeg inside the .app, signed with hardened
  runtime and notarized. Expect most of the packaging work here: mlx, torch (Wolof SpeechT5) and
  ffmpeg all need signing.
- **Apple Silicon only** (mlx). State the minimum on the download page; 8 GB RAM works for Whisper
  turbo, Wolof SpeechT5 wants 16 GB.
- **Downloads on demand, not in the DMG:**

  | Package | Size |
  |---|---|
  | Speech model (Whisper large-v3-turbo), always needed | ~1.6 GB |
  | French voices (Piper) | ~60–130 MB |
  | Spanish / Russian voices (Piper) | ~60–120 MB each |
  | Wolof (SpeechT5 + vocoder) | measure (torch) |

  Hosted on the VPS or a CDN, and checked by hash.
- **Keeping awake:** `IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleSystemSleep)`
  while a session runs. Closing the lid still sleeps the Mac, so the operator page warns "keep the
  lid open and plugged in" and alerts on battery.
- **Network drops:** the engine buffers and reconnects, and listeners see "reconnecting…".
- **Updates:** Sparkle with an appcast on hearchurch.com, plus an EdDSA signing key kept off the
  repo.

## Licensing (before public release)

- **Wolof:** SpeechT5 Wolof (MIT) is the default voice and is fine for any use. The MMS voice is
  CC BY-NC (non-commercial): offer it only as an optional second voice for free and own-key
  sessions, with a license note, and turn it off for paid sessions when credits arrive (or get
  written permission from Meta / rnjema-unima).
- `fr_FR-tom-medium`: check its model card before redistributing (siwis is CC BY 4.0, which only
  needs credit).
- Spanish and Russian Piper voices: pick ones with permissive licenses and add them to
  THIRD_PARTY_NOTICES.
- The repo is MIT and the Mac app stays open source in it.

## Church policy and trust

- **Approval:** Latter-day Saint guidance limits broadcasting meetings, so setup asks for
  bishop/pastor approval. No listener PIN: a ward that streams is public to anyone
  nearby, and setup says so.
- **No implied endorsement:** naming the church in the dropdown is fine, but no logos and no
  suggestion of official endorsement.
- **Privacy policy** (needed for the website and notarization metadata):
  - Audio never leaves the Mac.
  - Text goes to Anthropic for translation.
  - Nothing is kept on the server; an operator may opt in to save a transcript on their own Mac.
  - Listener location is not stored.

  Children speak in some meetings, which makes this matter more.

## Making it a business later

Not for launch. What would be needed to charge with a margin:

- **Commercial licenses everywhere:** no MMS in paid sessions; check `fr_FR-tom`, Spanish and
  Russian voice cards; Whisper/Silero are MIT; Claude API commercial terms allow a paid service
  built on it (not reselling raw API access).
- **Own Wolof voice:** pay a native speaker to record a few hours with a signed release, then
  fine-tune. Owned outright, likely better quality, and a moat competitors can't copy.
- **Revenue:** credits with a margin (e.g. $3 per language-hour vs ~$2 cost) or a flat plan
  ($15–20/month per ward). Own-key users pay nothing unless a platform fee is added.
- **Business setup:** LLC; Stripe + Stripe Tax; terms of service with a "translation may contain
  errors" disclaimer; privacy and refund policies; receipts clerks can submit.
- **Protecting it while open source:** trademark "Hear Church" (~$350 US filing). Forks can use
  the code but not the name, the hearchurch.com listener network or an owned voice.
- **Who pays:** ask a bishop / stake clerk whether wards can pay from their local budget or
  members would pay personally, before building billing.
- **Scale:** ~$4–5/month profit per ward means it only adds up at hundreds of wards. Prove it free
  in a few wards first.

## Phases

1. **Server multi-tenancy:** congregations, device tokens, per-session routing, `/c/<slug>`, keep
   the current ward working. Engine starts the meeting from its own mic.
2. **Location:** geocode at sign-up, `/api/live`, listener "Find my congregation" page with fallback
   search and the QR code path.
3. **Mac app shell:** Swift wrapper, bundled engine, onboarding, Keychain key, preflight, keep-awake,
   launch at login.
4. **Language packages:** on-demand downloads with sizes, Spanish and Russian voices and prompts,
   request-a-language form.
5. **Trial proxy:** VPS-side key, limits, switch-to-own-key prompt after the first Sunday.
6. **Ship:** signing and notarization, Sparkle appcast, download page, privacy policy, setup guide.
   Pilot with 2–3 wards before opening sign-ups.

## Open questions

- None right now.
