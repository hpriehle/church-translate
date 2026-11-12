# Billing: where credits, the trial and Stripe stand (2026-11-12)

Survey of commit 24be883 (file:line there), then what branch `chat/hc-pricing` adds. Plan: future-pacing.md §4
"Claude access & payment"; rules: docs/hear-church-plan.md:270-281.

## Today (24be883)

**Mac app**
- Own key is the only route. `OnboardingView.swift:572-596` (`ClaudeStep`) asks for an Anthropic API key (or a
  subscription token) and won't continue without a working one (`:109`). `ClaudeKeyGuide` (`:558-569`) tells the
  operator to buy Anthropic credit and set a spend limit.
- "Start with a free first Sunday instead" (`:591-593`) is a disabled button with "Coming soon"; nothing behind it.
- The key lives in the Keychain (`Keychain.swift:9`) and reaches the engine as `ANTHROPIC_API_KEY` or
  `CLAUDE_CODE_OAUTH_TOKEN` (`AppModel.swift:108-110`, `Engine.swift:93`; check `AppModel.swift:239-263`).
- No balance, top-up, Stripe or credits screen anywhere in `mac/`.

**Engine (runs on the Mac)**
- Translation calls Claude straight from the Mac with the church's key (`app/config.py:244-250` picks the credential;
  `app/translate/api_session.py:45` prices tokens). Usage and dollars are tracked per chunk
  (`app/pipeline.py:179, 339`) and summarized per hour of speech in `app/report.py:55-58`: a local report, not a meter.
- `session_start` to the hub never sends `trial` (`app/engine.py:495`).

**Hub / VPS (`app/hear/`)**
- `sessions.trial` exists (`db.py:55`), written from `session_start.trial` (`live.py:10, 190`; `db.py:306-308`); always 0.
- No `/api/trial` route, no Claude key on the server, no per-congregation metering of speech minutes, no credit
  ledger, no billing mode on `congregations` (`db.py:20-40`), no Stripe code or webhook anywhere in the repo.
- Operator-only admin exists (`api.py:557-592`, `main.require_operator`), which is where billing admin can live.

**Site**: `/pricing` was a held-back draft (`site/src/pages/_pricing.astro`); the homepage FAQ, the schema.org offer,
`/download` and `/terms` said the app is free and uses your own Anthropic key.

## Missing for paid credits

| Piece | What's needed |
|---|---|
| Speech-minute meter (built, see below) | The engine reports speech seconds per language to the hub (session_start/heartbeat), the hub stores them per session. Chunk durations already exist (`ChunkRecord.audio_dur_s`, `pipeline.py:276`). |
| Trial proxy | `POST /api/trial/translate` on the VPS with Harrison's key, rate-limited, capped at the free hour; the engine needs a "hub" backend beside "api"/"cli" (`config.py:115`). |
| Credit balance (built, see below) | A ledger table (top-ups +, metered language-seconds −) and `balance(cid)`; `GET /api/hc/billing` (added on this branch) is where the app would read it; shown on `/speak` and in the app. |
| Negative-balance rule (built, see below) | A running meeting never stops; the next `session_start` is refused once nothing is left (decided: blocked until topped up, not at −2 language-hours). |
| Stripe top-up (test mode) | Checkout Session for a top-up, a `checkout.session.completed` webhook (signature-checked, idempotent on the event id) that credits the ledger. Needs test keys in the VPS secrets. |
| Business | LLC, Stripe account in its name, Stripe Tax, terms + refund policy, privacy policy update (text goes through the VPS on credits). |

## Built on this branch (nothing deployed)

- Site: `/pricing` live in the build, in the nav and footer, the sitemap and a Worker route; the "free, your own
  key" copy is gone (see the commit "Price Hear Church as prepaid credits on the site").
- Hub: `congregations.billing` = `credits` (default) or `own_key`; congregations that exist when this ships are
  grandfathered as `own_key`. One-time own-key codes (`own_key_codes`, hashes only), `POST /api/hc/own-key-code/check`,
  `POST /api/hc/own-key-code`, `GET /api/hc/billing`, `billing` in the connect answer and the admin list, admin
  `POST/GET /api/admin/own-key-codes`, CLI `uv run python -m app.hear.codes mint "Dakar Ward"` / `list`.
  Tests: `tests/test_billing.py`.
- Mac: the Claude step explains credits and takes a key only after a code checks out (or when the joined
  congregation already uses its own key); the code is used up right after sign-up. Not compiled yet.

## Built on branch `chat/hc-metering` (from `chat/hc-pricing`; nothing pushed or deployed)

**Meter (engine → hub).** The pipeline calls `Pipeline.speech_taps` with (chunk seq, VAD-voiced seconds, languages
it's translated into) for every chunk Whisper heard words in (`app/pipeline.py`, `_stt_stage`); silence and chunks
with no words never count. `PublishClient` (`app/engine.py`) keeps a chunk only while the hub session is live and
only for the languages the hub last reported as heard: the hub's `listeners` message now carries `heard`, the
languages with at least one phone on the audio or the captions (`LiveSession.heard`, `app/hear/live.py`). Pending
chunks are sent every 15 s as `{"type": "meter", session, items}` in batches of 200 and dropped only when the hub
answers `meter_ok` with their seqs; after a dropped link they go out on the next one. At most 20,000 are kept.
Metering is a tap on the event loop that can't raise into the meeting; a lost report under-bills, never blocks.

**Ledger (hub, `app/hear/db.py`).** `sessions` gets `code` (the hub's session code) and `billing` (the mode the
congregation had when the meeting started; sessions from before credits become `own_key`). `speech` holds one row
per (session, chunk seq, language), so a resent report counts once (`record_speech`). `credits` holds top-ups,
unique on the Stripe payment id (`add_credit`). `balance(cid)` gives, in language-minutes: `metered_min` (all heard
speech, for stats), `used_min` (credit-billed sessions only), `free_left_min` (the first 60 go first),
`balance_min`/`balance_usd` (bought minus used past the free hour, at $5 per language-hour; may go negative),
`left_min` and `can_start`. Own-key meetings are metered and never debited, also after the church switches to
credits.

**Negative balance.** A meeting is never cut off: reports are stored whatever the balance. The check is in
`Live._start` (`app/hear/live.py`), through `Database.can_start`: a *new* meeting of a credit-billed congregation
with nothing left (`left_min <= 0`) gets `session_refused` "… is out of credit (balance -$2.50). Buy credit to start
the next meeting." Resuming a session within the grace, or re-announcing a session this congregation started in
the last 6 hours (the drop outlasted the grace, or the hub restarted), is never refused. Own key: never refused.

**Top-up entry point.** `credit_top_up(cid, payment_id, cents)` in `app/hear/api.py` (for the Stripe webhook:
`checkout.session.completed` → payment intent id and `amount_total`), and the operator route
`POST /api/admin/congregations/<id>/credits {payment_id, cents}` → `{created, credit, balance}`; same payment again
→ `created: false`, same id with another amount or congregation → 409. `GET /api/hc/billing` and the admin list
now include `balance`.

**Tests**: `tests/test_metering.py` (listener gating on the Mac, free hour first, a report sent twice counts once,
top-up idempotent, negative balance refuses only the next meeting, resumes never refused, own key metered but not
debited, old databases migrated, the Mac's batching/resending/backlog cap, end to end Mac → hub with a listener on
one of two languages). `uv run pytest -q`: 209 pass; 5 in test_app_mode/test_languages fail on this Linux box
without these changes too (no `huggingface_hub`, a voice default).

**Mac (Swift)**: no change. The meter runs in the Python engine the app bundles; the Swift code doesn't read
`/api/hc/billing` yet, and extra keys there don't break it.

## Built on branch `chat/hc-stripe` (from `chat/hc-metering`; nothing pushed or deployed, no Stripe call made)

**Buying credit (hub, `app/hear/api.py`).** `POST /api/hc/checkout` (device token) `{usd}` → `{url, id, usd}`:
a hosted Stripe Checkout session, `mode: payment`, `payment_method_types: ["card"]` only (a card is paid when
Checkout completes; a bank debit or Link bank payment would complete *unpaid* and its later success would be
missed), one line item "Hear Church credit" of `usd × 100` cents, `client_reference_id` and `metadata`
(`congregation_id`, `slug`, `cents`, also on the payment intent). Amounts: whole dollars $5–$500; the page offers
$10 / $25 / $50 / $100. Back to `/api/hc/checkout/done` or `/api/hc/checkout/cancelled` (plain pages). Without
`STRIPE_SECRET_KEY` it answers 503 `not_configured` and `billing.can_buy` is false; own-key congregations get 409.

**Webhook.** `POST /api/stripe/webhook`: the signature is checked with `stripe.Webhook.construct_event` (default
5-minute tolerance, so a captured request replayed later is refused with 400). Only `checkout.session.completed`
with `payment_status: paid`, `mode: payment`, `currency: usd` credits: `credit_top_up(congregation_id,
payment_intent, amount_total)`. Stripe's retries and duplicate deliveries credit once (unique payment id). Other
events → 200 and ignored; a paid session that can't be credited (unknown congregation, id reused) is logged as an
error and acknowledged (a retry wouldn't help, so check the hub log for `paid but not credited`); a database error
→ 500 so Stripe retries. Without `STRIPE_WEBHOOK_SECRET` → 503 (Stripe keeps retrying for 3 days).

**Balance.** `GET /api/hc/billing` adds `balance_usd`, `language_hours_left` (free hour included; negative when
owed), `out_of_credit`, `can_buy`, `buy_usd`, `usd_per_language_hour`, `message` ("Out of credit (balance -$2.50).
Buy credit to start the next meeting." / "Credit $9.17 · 1.8 language-hours left."). The Mac's engine proxies it
for its pages: `GET /api/billing` and `POST /api/buy-credit {usd}` (speaker or operator sign-in) call the hub with
the device token. `/speak` shows a credit box (red when out of credit), an amount menu and "Buy credit" (opens
Checkout in a new tab, then checks the balance every 10 s for 10 minutes, otherwise every minute), and the hub's
refusal notice as the page's error line.

**Resume loophole closed.** `sessions.device_id` records the Mac that started a meeting; `recent_session` (the
6-hour "never refuse a meeting under way" rule) now matches only that Mac. Another Mac of the congregation sending
the code is checked for credit like a new meeting. Sessions from before this column match no Mac (only matters for
credit congregations without credit, in the 6 hours around the deploy).

**Tests**: `tests/test_stripe.py` (mocked Stripe client: card-only session and its fields, bad amounts, strangers,
own key, not configured, Stripe down → 502; webhook signed with the SDK's own signing helper: paid → credited
once, sent again → not again, unsigned / wrong secret / tampered body / an hour-old signature → 400, unpaid / euro /
subscription / other event / unknown congregation → not credited; the out-of-credit billing answer; the Mac's
proxy routes; another Mac can't resume to dodge the credit check, the starting Mac can). `uv run pytest -q`: 220
pass; the same 5 in test_app_mode/test_languages fail on this Linux box without these changes too.

**Mac (Swift)**: no change needed. The speak page is served by the bundled engine, which already has the device
token. If the app window should show the balance too, it can read the engine's `GET /api/billing` (same JSON as
the hub's) — not done.

### What Harrison sets in the Automaddie Stripe account (test mode first)

1. Developers → API keys: copy the **secret key** (`sk_test_…`). On the VPS it goes in the hub service's
   environment as `STRIPE_SECRET_KEY` (systemd `Environment=`/`EnvironmentFile=`; `data/secrets.env` is only
   read for the Claude keys, not for these).
2. Developers → Webhooks → Add endpoint: URL **`https://hearchurch.com/api/stripe/webhook`** (the hub's public
   address + `/api/stripe/webhook`), events: **`checkout.session.completed`** only. Copy its **signing secret**
   (`whsec_…`) into the same environment as `STRIPE_WEBHOOK_SECRET`.
3. Settings → Payment methods: nothing to change; the code asks for cards only (Link bank payments and other
   delayed methods can't appear on this Checkout).
4. Settings → Public details / Branding: business name and statement descriptor (what shows on the card statement).
5. Restart the hub after setting both. Check: `GET /api/hc/billing` from a Mac says `can_buy: true`; buy $10 with
   the test card 4242 4242 4242 4242; the hub log shows `credit: 1000 cents`; Stripe's webhook page shows 200.
For live mode repeat 1–2 with the live key and a live-mode endpoint (each mode has its own signing secret).

## Still missing

- The trial proxy (meetings on credits still need the church's own key until it exists).
- After a refusal the Mac doesn't ask again for that run: after a top-up the operator stops and restarts the meeting.
- Refunds: done by hand in the Stripe dashboard; nothing takes the credit back off the ledger (no
  `charge.refunded` handling).
- The balance in the Mac app's own window (Swift), if wanted.
