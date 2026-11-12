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
| Speech-minute meter | The engine reports speech seconds per language to the hub (session_start/heartbeat), the hub stores them per session. Chunk durations already exist (`ChunkRecord.audio_dur_s`, `pipeline.py:276`). |
| Trial proxy | `POST /api/trial/translate` on the VPS with Harrison's key, rate-limited, capped at the free hour; the engine needs a "hub" backend beside "api"/"cli" (`config.py:115`). |
| Credit balance | A ledger table (top-ups +, metered language-seconds −) and `balance(cid)`; `GET /api/hc/billing` (added on this branch) is where the app would read it; shown on `/speak` and in the app. |
| Negative-balance rule | A running meeting never stops; `session_start` is refused below −2 language-hours with a "top up" message (`live.py:190` is the place; `session_refused` already exists, `live.py:16`). |
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

## Smallest next step

The speech-minute meter (engine → hub, stored per session): it needs no LLC or Stripe, feeds both the trial cap
and the credit ledger, and can be tested on a weekday dry run.
