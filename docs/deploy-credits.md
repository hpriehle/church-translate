# Go-live: credits (hub → site → Mac app, then Stripe live)

Runbook for shipping the credits chain (`chat/hc-pricing` … `chat/hc-realproxy`, plus `chat/hc-legal`). Written
2026-11-13 on `chat/hc-golive`. Each step needs Harrison's yes; never on a Sunday (or a Saturday evening, US or
Africa time: wards meet then). What the code does is in `docs/billing-status.md`.

## 0. What the repo says about today's deployment, and what it doesn't

| Piece | How it runs today | Source |
|---|---|---|
| **Hub** (`hearchurch.com`: `/listen`, `/<slug>`, `/api/*`, `/ws/*`, `/c/<slug>/stream/*`) | The same `python -m app` server as church-translate, on "the VPS", SQLite at `data/hearchurch.db` (`HearCfg.db_path`, relative to `HEARCHURCH_HOME`, else the checkout). Linux pattern: systemd unit like `scripts/church-translate.service.example` (`uv run … python -m app --no-browser`), public through Cloudflare (tunnel example: `scripts/church-translate-tunnel.service.example`). `server.update_from_git` lets "Restart server" on the operator page `git pull` first. | README "Running on a Linux server", `app/config.py`, docs/hear-church-plan.md:33-35, docs/seo-site-structure.md:62-66 |
| **Site** (`/`, `/pricing`, `/how-it-works`, `/lds*`, `/blog*`, `/privacy`, `/terms`, `/credits`, `/download*`, `/appcast*.xml`) | Cloudflare Worker `hearchurch-download` (`download/`): `wrangler deploy` from `download/` builds `site/` (Astro) first and serves `site/dist`; downloads and appcasts come from R2 bucket `hearchurch-downloads`. Any path the Worker's routes catch but don't own is passed to the VPS. | `download/wrangler.toml`, docs/seo-site-structure.md:145-151 |
| **Mac app** | `mac/release.sh <version>` (build + notarize) then `--publish` (Sparkle appcast signed with the `hearchurch` EdDSA key in the Mac's login Keychain, upload to R2). Running copies see the update within a day and install on quit. Last version in `mac/project.yml`: **0.1.4**. Release runs on Harrison's Mac only (Keychain items `hearchurch-r2`, `hearchurch-notary`). | `mac/release.sh:1-23`, docs/hear-church-plan.md:31-36 |

**Unknown (the repo doesn't say; Maddie must ask Harrison or look on the box before step 1):**
- The VPS's provider, IP/hostname for SSH, the login user, and whether it is reached by plain SSH or another way.
- The hub's systemd unit name, its `User=`, `WorkingDirectory=` (where the checkout is), and whether it uses
  `EnvironmentFile=`. The example unit is `church-translate.service`, user `translate`, `/home/translate/church-translate`.
- Which branch that checkout tracks, and whether `update_from_git` is on. The task treats **`origin/scripture-lookup`
  (24be883)** as deployed; `origin/main` (ffff6ce) doesn't even contain `hear-church-phase2`, so the hub can't be
  running `main`.
- Whether the hub is behind a Cloudflare tunnel or a proxied A record (the tunnel unit is only an example).
- From which checkout `wrangler deploy` was last run, and whether the live Worker matches `download/` on 24be883.
- Whether a Stripe **test** webhook endpoint already points at `https://hearchurch.com/api/stripe/webhook`
  (the e2e script signs its events locally and doesn't need one).

## 1. Branches: what to merge, in what order

The chain is linear: each branch contains the one before it, all on top of `origin/scripture-lookup` (24be883):
`hc-pricing (4) ⊂ hc-metering (5) ⊂ hc-stripe (6) ⊂ hc-proxy (10) ⊂ hc-e2e (11) ⊂ hc-local (12) ⊂ hc-checkoutfix
(14) ⊂ hc-realproxy (15 commits)`, and `hc-golive` (this runbook + the upgrade test) on top. So:

1. Merge **`chat/hc-golive`** (or `chat/hc-realproxy` if this doc should stay out) into the branch the VPS
   deploys from: a fast-forward of `scripture-lookup`, no conflicts. Merging the earlier branches one by one
   gives the same result.
2. **`chat/hc-legal`** (draft terms + privacy, from `hc-stripe`): merges into `hc-realproxy` with no conflict
   (`git merge-tree` clean). It is a draft "for review": merge only after Harrison has read it, and before or
   with the site step (the live `/terms` and `/privacy` otherwise still describe the own-key app).
3. **`origin/main`** has one commit the chain lacks (ffff6ce "Clear the translator's error once Claude starts
   again", 1 line in `app/translate/router.py`): merges cleanly. Optional for go-live.
4. **`origin/hear-church-phase2`** (75026d9) is already inside `scripture-lookup`: nothing to do.
5. **Mac session 39a7754a** has **2 unpushed commits on `hear-church-phase2` on Harrison's Mac** (not seen here,
   not fetched). They branch from 75026d9, so they diverge from `scripture-lookup` and from this chain. Before
   merging: push them from that Mac session, then `git merge-tree --write-tree chat/hc-golive origin/hear-church-phase2`
   to see conflicts. Likely spots if they touch them: `app/hear/*.py`, `app/engine.py`, `mac/…/OnboardingView.swift`,
   `download/src/*`.
6. **Pushed but unmerged site branches from Nov 11** (`origin/download-icon` ⊃ download-page, -page-2, -chip,
   -301-fix; and `origin/remove-download-page`) **conflict** with the chain:
   `download/src/index.js` (content), `download/src/page.html` (they delete it, the chain edits it),
   and for `remove-download-page` also `site/src/pages/_pricing.astro` (the chain deletes it, they edit it).
   Decide whether they ship; if yes, merge them after the chain and resolve: keep their download page, re-apply
   the chain's copy changes (no "free, your own key"; the chain's `/download` text about credits), keep
   `pricing.astro`, drop `_pricing.astro`.

Check before deploying anything: `uv run pytest -q` on the merged branch (expected on Linux: all pass except the 5
known Mac-only failures in `test_app_mode`/`test_languages`), and `scripts/e2e_credits.py` (17/17 PASS without
`--real-anthropic`).

## 2. Hub environment

Set in the hub service's environment (systemd `EnvironmentFile=` with mode 600, owned by the service user;
`data/secrets.env` is only read for Claude keys and is **not** where these go). Values: Harrison's
`~/.config/hearchurch/*.env` on the Beelink; never paste them into this doc, a chat, a commit or a log.

| Variable | From | Without it |
|---|---|---|
| `STRIPE_SECRET_KEY` | `stripe-test.env` (`sk_test_…`) first; live key in step 5 | checkout → 503 `not_configured`, `can_buy: false` |
| `STRIPE_WEBHOOK_SECRET` | `stripe-test.env` (`whsec_…` of the endpoint below) | webhook → 503, Stripe retries for 3 days |
| `STRIPE_PAYMENT_METHOD_CONFIG` | `stripe-test.env` (test: `pmc_1UbAjMK0BHzgWcTPV0fZxOrY`) | optional: Stripe's default methods |
| `ANTHROPIC_API_KEY` | `hub.env` (Hear Church's key, with a spend limit set at Anthropic) | `/api/hc/translate` → 503: credits congregations can't translate |

Notes:
- `ANTHROPIC_API_KEY` in the service environment is also what the VPS's own translator would use
  (`claude_access()` prefers the environment). If the VPS still runs meetings itself (a speak page on the server),
  those now run on Hear Church's key. Check the operator page's Claude access after restart.
- The Stripe **test** endpoint (Dashboard, test mode → Developers → Webhooks): URL
  `https://hearchurch.com/api/stripe/webhook`, events `checkout.session.completed`,
  `checkout.session.async_payment_succeeded`, `charge.refunded`. Its signing secret must equal `STRIPE_WEBHOOK_SECRET`.
- `stripe>=11` is a new dependency: the deploy must run `uv sync` (or the unit's `uv run` does it on start).

## 3. Order and what each step changes

**Hub → site → Mac app.** The hub first because the new site points at `/pricing` and credits that only the new
hub honours, and the new Mac app needs the hub's `/api/hc/billing`, `/api/hc/translate` and own-key-code routes.

### What existing Macs (0.1.0–0.1.4) do against the new hub — must keep working

Proven by `tests/test_live_schema_upgrade.py` (old database built by the deployed `db.py`, opened by the new code):
- Every congregation in the database at the first start of the new hub becomes `billing = own_key`, and every old
  session `own_key` too. No row or value changes (only the session still open at shutdown gets `ended_at`, as
  before). New tables `speech`, `credits`, `own_key_codes` appear empty. Reopening grandfathers nobody else.
- An old Mac sends no meter reports and never asks for billing. Its `session_start` is accepted (own key is never
  refused), it translates with its own key as before, `/api/hc/connect` hands a new Mac of that congregation
  `billing: own_key`, `/api/hc/checkout` and `/api/hc/translate` answer 409 for it.
- **Gap (Harrison decides):** a church that signs up *with an old app* after the hub deploy gets `credits`
  (the default for new sign-ups). The old app still makes it enter its own key and translates with it; nothing is
  metered, so it is never charged or refused. But once Sparkle updates that Mac to the credits release, its engine
  sees `credits` and translates through the hub on Hear Church's key (free hour, then pay). Options: (a) keep the
  window short — release the Mac app the same day as the hub (recommended); (b) after the release, list
  congregations created between the two deploys (admin list, `created_at`) and give them own-key codes.

### Step 1 — hub

Before: confirm nobody is live (`/api/admin/congregations?key=…`: `live` false everywhere) and it isn't Sunday.

1. On the VPS, as the service user: **back up the database while the hub is stopped**
   (`systemctl stop <unit>`; `cp data/hearchurch.db data/hearchurch.db.pre-credits-$(date +%F)`), note the
   current commit (`git rev-parse HEAD`).
2. `git fetch && git checkout <deploy branch> && git merge --ff-only origin/<branch with the chain>`; `uv sync`.
3. Add the 4 variables (step 2 above, **test** Stripe keys), `systemctl daemon-reload`, `systemctl start <unit>`.
4. `journalctl -u <unit> -n 50`: no traceback.

Smoke tests (from anywhere):
```sh
H=https://hearchurch.com
curl -s -o /dev/null -w '%{http_code}\n' $H/listen                         # 200
curl -s -o /dev/null -w '%{http_code}\n' $H/<an existing slug>             # 200
curl -s -o /dev/null -w '%{http_code}\n' $H/api/hc/billing                 # 403 (needs a device token): route exists
curl -s -o /dev/null -w '%{http_code}\n' -X POST $H/api/hc/translate       # 403 (no device token), not 404
curl -s -o /dev/null -w '%{http_code}\n' -X POST $H/api/stripe/webhook -d '{}'   # 400 (bad signature); 503 = secret missing
curl -s "$H/api/admin/congregations?key=$OPERATOR_PIN" | jq '[.congregations[].billing]|unique'   # ["own_key"]
```
- The live ward's Mac (0.1.x): start and stop a test meeting from its speak page; the phone page plays it.
- **Test-mode purchase on production:** sign up a throwaway congregation ("Go-live test", with a current Mac app
  build pointed at the hub, or `POST /api/hc/signup`), `POST /api/hc/checkout {"usd": 10}` with its device token,
  pay with `4242 4242 4242 4242`; then `GET /api/hc/billing` shows `bought_usd` 10, Stripe's webhook page shows 200,
  the hub log `credit: 1000 cents`. Refund it in the Stripe test dashboard → the balance drops back. Then hide the
  congregation (`POST /api/admin/congregations/<id>/hidden`); its test credit stays in the ledger (harmless, but
  remember it when reading revenue).
- Credits translation with the real key: a short meeting from that test congregation translates; costs cents.

Rollback: `systemctl stop <unit>`, `git checkout <noted commit>`, `uv sync`, start. The old code runs on the
upgraded database (tested: it ignores the new columns and tables). Restore the backup only if nothing new
happened since (sign-ups, devices, sessions would be lost). Congregations that **sign up while rolled back** get
`billing = credits` from the column default and are not grandfathered on the next roll-forward (tested): after
rolling forward again, set them to own key (`UPDATE congregations SET billing='own_key' WHERE created_at > <rollback time>`,
on a copy first) or give them own-key codes.

### Step 2 — site (Cloudflare Worker)

1. From a checkout of the merged branch (with `chat/hc-legal` if approved): `cd download && npx wrangler deploy`
   (builds `site/` first). New route in `wrangler.toml`: `hearchurch.com/pricing`.
2. Smoke tests:
```sh
for p in / /pricing /how-it-works /download /terms /privacy /credits /sitemap.xml /appcast.xml; do
  printf '%s %s\n' "$(curl -s -o /dev/null -w '%{http_code}' https://hearchurch.com$p)" $p; done   # all 200
curl -s https://hearchurch.com/pricing | grep -c '\$5 per language'            # ≥1: the price
curl -s https://hearchurch.com/ | grep -ci 'your own anthropic key'          # 0
curl -s -o /dev/null -w '%{http_code}\n' https://hearchurch.com/listen       # 200, still the VPS
```
   Check `/pricing` by eye against the decisions: $5 per language-hour, first hour free, refunds of unused
   credit anytime, credits never expire.
Rollback: `npx wrangler rollback` (Cloudflare keeps the previous version), or `wrangler deploy` from 24be883's
`download/`. Downloads and appcasts are in R2 and don't change with a Worker deploy.

### Step 3 — Mac app (Sparkle)

1. On Harrison's Mac, from the merged branch: `mac/release.sh 0.2.0` → notarized `mac/dist/HearChurch-0.2.0.dmg`.
   The Swift changes on the chain have **never been compiled** (Linux box): build errors are expected to show up here.
2. Install the DMG on a clean user account; onboarding a new congregation on credits needs no key; the speak page
   shows the credit box; a short meeting translates (online and "this Wi-Fi only"); an own-key code works.
   An existing own-key install updated in place keeps its key and translates as before.
3. `mac/release.sh 0.2.0 --publish` (and `--intel` / `--intel --publish` for the Intel build).
4. Smoke: `curl -s https://hearchurch.com/appcast.xml | grep -o 'shortVersionString>[^<]*'` shows 0.2.0;
   `/download` serves the 0.2.0 DMG; an old 0.1.x copy offers the update ("Check for Updates").
Rollback: a Sparkle update can't be taken back from Macs that installed it. Publish a 0.2.1 from the previous
code (higher version number) if 0.2.0 is broken; the hub keeps serving both.

## 4. Stripe live mode (a separate, later step; Harrison's yes again)

Prerequisites (business side, docs/billing-status.md "Business"): LLC/Stripe account in its name, Stripe Tax
decision, terms + refund policy and privacy policy published (step 2 with `hc-legal`), statement descriptor set.

1. Live mode → Developers → Webhooks → Add endpoint `https://hearchurch.com/api/stripe/webhook`, events
   **`checkout.session.completed`, `checkout.session.async_payment_succeeded`, `charge.refunded`**. Copy its own
   signing secret (live and test secrets differ).
2. Live mode → Settings → Payment methods: create a "Hear Church" configuration like the test one (card, Apple Pay,
   Google Pay, Link); copy its `pmc_…` id. (Apple Pay needs the domain verified in live mode.)
3. Live secret key (`sk_live_…` or a restricted key with Checkout Sessions write).
4. On the hub, replace the three Stripe values with the live ones (store them beside the test ones on the
   Beelink, e.g. `~/.config/hearchurch/stripe-live.env`), restart.
5. Smoke: buy the smallest amount ($5) with a real card for the go-live test congregation; the balance rises, the
   webhook shows 200; refund it in the dashboard; the balance drops. Hide the congregation again.
Rollback: put the test values back and restart (purchases already made live stay in the ledger; refund them in
the live dashboard if needed). `scripts/e2e_credits.py` refuses a live key by design: keep using the test file for it.

## 5. Still open (from docs/billing-status.md "Still missing")

After a refusal the Mac doesn't retry until the meeting is restarted; refunds are made by hand in Stripe; no
per-congregation cost record for the proxy; the Mac window doesn't show the balance.
