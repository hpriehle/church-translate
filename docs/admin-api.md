# Hear Church hub: admin API for the Siteboard desk

Operator-only JSON on the hub (hearchurch.com), for the Siteboard "Hear Church" page. Code: `app/hear/admin.py`
(new routes) and `app/hear/api.py` (the older admin routes). Tests: `tests/test_admin_api.py`.

**Sign-in.** The same check as every `/api/admin/*` route (`main.require_operator`): the operator password as
`?key=<operator_pin>`, or the `op_pin` cookie that `/login` sets, or a browser on the hub machine itself. Anything
else gets `403`. Note: `?key=` puts the password in the URL, so it shows up in access logs and proxy logs. If
Siteboard calls from a server, an `Authorization` header would be better, but the hub doesn't accept one yet.

**Conventions.** Times are Unix epoch seconds (UTC, float). Money comes as both `*_cents` (int) and `*_usd` (2
decimals). A language-minute is one minute of speech heard in one language. Pricing (Nov 14): every credits
congregation gets one free language-hour per calendar month (UTC, resets on the 1st, no carry-over); beyond it,
bundles: $20 = 4 h, $50 = 11 h, $100 = 25 h, $250 = 70 h. A top-up is stored as the minutes it bought
(`credits.seconds`); top-ups from before the bundles (and hand credits of other amounts) count at $5 per hour.
Paid time comes off a congregation's top-ups oldest first, each at its own rate. Credits never expire. Errors come back as `{"error": <code>,
"message": <text for a person>}` or as FastAPI's `{"detail": …}` (403, 404 and 422 on the older routes).

Example values below come from a scratch database: two congregations, one 45-minute meeting in fr + es, a $25
top-up, and a $5 manual credit.

---

Auth: send the operator password in the `X-Operator-Key` header (preferred: keeps it out of URLs and logs). `?key=` still works.

## GET /api/admin/stats

```json
{
 "at": 1794635908.43,
 "congregations": {"total": 2, "hidden": 0, "verified": 2, "by_billing": {"credits": 2}, "live_now": 0},
 "last_7d":  {"signups": 2, "active_congregations": 1, "meetings": 1,
              "language_hours": {"total": 1.5, "paid": 0.5, "free": 1.0, "own_key": 0.0, "internal": 0.0, "paid_usd": 2.5}},
 "last_30d": {"signups": 2, "active_congregations": 1, "meetings": 1, "language_hours": {"…": "same shape"}},
 "all_time": {"meetings": 1, "language_hours": {"total": 1.5, "paid": 0.5, "free": 1.0, "own_key": 0.0, "internal": 0.0,
              "paid_usd": 2.5}},
 "credit": {"sold_usd": 25.0, "manual_usd": 5.0, "refunded_usd": 0.0, "outstanding_usd": 27.5, "overdrawn_usd": 0,
            "free_hours_per_month": 1.0, "legacy_usd_per_language_hour": 5.0,
            "bundles": [{"usd": 20, "hours": 4, "usd_per_hour": 5.0}, {"usd": 50, "hours": 11, "usd_per_hour": 4.55},
                        {"usd": 100, "hours": 25, "usd_per_hour": 4.0}, {"usd": 250, "hours": 70, "usd_per_hour": 3.57}]},
 "installs": {"devices": 2, "active_7d": 1, "active_30d": 1, "never_connected": 1,
              "by_version": {"1.4.0":   {"devices": 1, "active_7d": 1, "active_30d": 1},
                             "unknown": {"devices": 1, "active_7d": 0, "active_30d": 0}}}
}
```

- `congregations.by_billing` counts visible congregations only; `hidden` counts the ones hidden by the
  administrator. `verified`: e-mail confirmed. `live_now`: meetings on the hub at this moment.
- `signups`: congregations created in the window. `active_congregations` and `meetings`: meetings started in the
  window, counting every meeting whether or not anyone spoke.
- `language_hours`: metered speech. `paid` is what credit-billed meetings used beyond the free hour, `free` is what
  the free hour covered, `own_key` is meetings paid with the church's own Anthropic key, and `internal` is meetings
  of `internal` congregations (both metered, never billed). `by_billing` may have `credits`, `own_key`, `internal`.
  `paid_usd` is `paid` at $5 per hour, which is earned revenue. Windows go by when the meeting started.
- `credit.sold_usd`: top-ups paid through Stripe. `manual_usd`: credits added by hand (see "kinds" below).
  `refunded_usd`: everything refunded. `outstanding_usd`: the sum of positive balances, meaning credit sold and not
  yet used, which the refund policy lets churches ask for back. `overdrawn_usd`: the sum of negative balances
  (meetings are never cut off mid-way).
- **installs** counts *Macs signed in to a congregation* (each one has a device token on the hub). It does not count
  downloads: the download counter lives in Cloudflare D1, in the download Worker, not on the hub. So:
  - An install that never finished sign-up isn't here. A Mac signed in twice (signed out and back in) counts twice,
    and a Mac thrown away still counts (look at `active_*`).
  - `active_7d`/`active_30d`: the Mac connected to the hub (opened its live link) in the window. The Mac connects
    whenever the app runs signed in, not only during meetings.
  - `never_connected`: signed in but never opened the live link.
  - `by_version`: the app version the Mac last reported. Only app builds from this branch on report a version, so
    every Mac on an older build shows as `"unknown"` until it updates and connects again.

## GET /api/admin/transactions?congregation=&kind=&limit=&before=

The ledger, newest first. All parameters are optional. `congregation`: a congregation id. `kind`: one of `top_up`,
`manual_credit`, `refund`, `usage`. `limit`: 1–500 (default 50). `before`: the `next_before` from the previous
page. When `next_before` is `null`, there are no more pages.

```json
{
 "transactions": [
  {"id": "refund:1", "kind": "refund", "at": 1794635908.49, "congregation_id": 1,
   "congregation": {"id": 1, "slug": "dakar", "name": "Dakar Ward"},
   "amount_cents": -500, "amount_usd": -5.0, "payment_id": "pi_3QabcDEF", "cents": 500,
   "refunded_total_cents": 500, "source": "admin", "stripe_refund_id": "re_3QxYz"},
  {"id": "usage:1", "kind": "usage", "at": 1794635908.37, "congregation_id": 1,
   "congregation": {"id": 1, "slug": "dakar", "name": "Dakar Ward"},
   "amount_cents": -250, "amount_usd": -2.5, "session_id": 1, "code": "20261109-100012", "billing": "credits",
   "started_at": 1794635908.37, "ended_at": 1794635908.38,
   "language_minutes": {"es": 45.0, "fr": 45.0}, "minutes": 90.0,
   "free_minutes": 60.0, "paid_minutes": 30.0, "cost_usd": 2.5},
  {"id": "credit:2", "kind": "manual_credit", "at": 1794635908.36, "congregation_id": 2,
   "congregation": {"id": 2, "slug": "thies", "name": "Thiès Branch"},
   "amount_cents": 500, "amount_usd": 5.0, "payment_id": "goodwill-2026-11", "cents": 500,
   "refunded_cents": 0, "refundable": false, "minutes": 60.0, "bundle": null}
 ],
 "next_before": "1794635908.363085|credit:2"
}
```

Kinds:
- `top_up`: a credit whose `payment_id` is a Stripe id (`pi_…`, also `ch_`/`cs_`/`py_`). The webhook credits these
  with the payment intent id. `refundable`: it was paid with a `pi_` and part of it is still unrefunded.
  `refunded_cents`: the total refunded from it so far. `minutes`: the language time it bought; `bundle`: its price in
  dollars when it is a bundle ($20/$50/$100/$250), else null.
- `manual_credit`: added by hand with `POST /api/admin/congregations/{id}/credits` under any other id (for example
  "goodwill-2026-11"). This is a naming rule: a hand credit filed under a real `pi_…` id counts as a top-up.
- `refund`: one row each time a top-up's refunded total grows. `source`: `"admin"` (the route below, with
  `stripe_refund_id`), `"stripe"` (the `charge.refunded` webhook, for a refund made in the Stripe dashboard), or
  `"unlogged"` (refunded before the hub started logging refunds on this branch: one row for the whole amount).
- `usage`: one meeting with metered speech (meetings with no speech aren't listed). `at` = when it started. Each
  month (UTC, by the meeting's start), a congregation's credit-billed meetings use up that month's free hour in
  order; `cost_usd` is the speech beyond it, at the rates of the top-ups it came from (oldest first; time no
  top-up covers counts at $5 per hour). Own-key meetings show `billing: "own_key"` and internal ones
  `billing: "internal"`; both cost 0. `ended_at: null` means the meeting
  is still running and its numbers are still growing.

For each congregation, the sum of `amount_usd` equals its balance (to the cent, apart from rounding per meeting).

## POST /api/admin/refunds  `{"payment_id": "pi_…", "cents": 500}`

Refunds through Stripe (the Refunds API on the payment intent), then takes the refund off the ledger.

- `cents` is optional. It defaults to the **unused part** of that top-up, pro rata at its own price: its minutes
  not used yet (meetings use the oldest top-ups first, after the month's free hour) × what it cost per minute. A
  $50 bundle (11 h) with 5.5 h unused refunds $25; a pre-bundle top-up, $5 per unused hour. Any refund takes
  `seconds × refunded / cents` off that top-up: refunding $25 of the $50 bundle takes off 5.5 h.
- Policy: unused credit can be refunded at any time, and used hours are final. The route still accepts any `cents`
  up to what's left of the top-up, so the admin can refund used credit by hand. When `cents` exceeds the unused
  part, the answer carries a `warning` and the balance goes negative.
- It is never more than the top-up: asking for more answers `409 too_much`.
- Stripe gets an idempotency key `hc-refund-<payment_id>-<refunded so far>-<cents>`. A double click or a retried
  request makes one refund. A deliberate second refund gets a new key, because the ledger has moved on by then.
- The ledger update works like the `charge.refunded` webhook (the top-up's refunded total grows to include the
  refund). When Stripe's webhook arrives afterwards with the charge's `amount_refunded`, nothing changes.

```json
{"ok": true,
 "refund": {"id": "re_3QxYz", "status": "succeeded", "cents": 500, "usd": 5.0},
 "credit": {"id": 1, "congregation_id": 1, "payment_id": "pi_3QabcDEF", "cents": 2500, "seconds": 18000.0,
            "created_at": 1794635908.36, "refunded_cents": 500, "refunded_at": 1794635908.49},
 "unused_cents_before": 2250,
 "balance": {"balance_usd": 17.5, "bought_usd": 20.0, "free_left_min": 0.0, "left_min": 210.0, "can_start": true, "…": "…"}}
```
With `cents` above the unused part, the answer also has `"warning": "2.50 USD of this refund was credit already used
for translation (used hours are normally final). …"`.

Errors (the ledger is untouched in every case):

| status | `error` | when |
|---|---|---|
| 404 | `not_found` | no top-up has that payment id |
| 409 | `not_stripe` | a manual credit: there is nothing to refund in Stripe |
| 409 | `fully_refunded` | nothing of the top-up is left |
| 409 | `nothing_unused` | no `cents` given and nothing is unused; carries `refundable_cents` |
| 409 | `too_much` | `cents` > what's left of the top-up; carries `unused_cents`, `refundable_cents` |
| 409 | `stripe` | Stripe refused it (for example already refunded, or the refund came back `failed`); `message` is Stripe's, plus `stripe_code`, `stripe_status` |
| 502 | `stripe` | Stripe unreachable, a Stripe server error, a bad key or a rate limit. **Check the Stripe dashboard before retrying.** Retrying with the same amount reuses the idempotency key, so it can't refund twice |
| 503 | `not_configured` | `STRIPE_SECRET_KEY` isn't set on the hub |
| 422 | (FastAPI) | `cents` ≤ 0 or not a whole number |

## Own-key codes

- `POST /api/admin/own-key-codes {"label": "Accra Ward"}` → `{"code": "HC-7KQM-X2PA-9TDW", "label": "Accra Ward"}`.
  This answer is **the only time the code is shown**: the hub stores only its hash, and the list never includes it.
  Logs carry the label only (`own-key code made for 'Accra Ward'`), never the code.
- `GET /api/admin/own-key-codes` → newest first:
  ```json
  {"codes": [
    {"id": 2, "label": "Lomé Branch", "created_at": 1794635908.42, "used_at": null, "voided_at": null,
     "status": "unused", "used_by": null, "used_by_id": null, "used_by_name": null},
    {"id": 1, "label": "Accra Ward", "created_at": 1794635908.41, "used_at": null, "voided_at": 1794635908.50,
     "status": "void", "used_by": null, "used_by_id": null, "used_by_name": null}]}
  ```
  `status`: `unused`, `used` or `void`. A used code names the congregation that used it: `used_by` (slug),
  `used_by_id`, `used_by_name`.
- `POST /api/admin/own-key-codes/{id}/void` → `{"ok": true, "code": {…as in the list, "status": "void"}}`. An unused
  code stops working right away: check and redeem both answer `bad_code`. Voiding again is fine. A used code
  answers `409 {"error": "used"}`, because that congregation has already switched to its own key (switch it back by
  hand if needed). An unknown id answers 404.

## GET /api/admin/congregations/{id}?meetings=20&transactions=50

One church in one call.

```json
{"congregation": {"id": 1, "slug": "dakar", "title": "Dakar Ward", "church": "LDS", "name": "Dakar", "unit_type": "ward",
                  "stake": null, "address": "", "city": "", "email": "bishop@example.org", "email_verified": true,
                  "hidden": false, "created_at": 1794635908.33, "billing": "credits", "timezone": "", "meeting_times": [],
                  "aliases": ["dakar-ward"]},
 "billing": {"billing": "credits", "balance_usd": 17.5, "language_hours_left": 3.5, "out_of_credit": false,
             "balance": {"free_left_min": 0.0, "used_min": 90.0, "metered_min": 90.0, "bought_usd": 20.0, "…": "…"},
             "message": "Credit $17.50 · 3.5 language-hours left.", "…": "…"},
 "live": false, "wifi_only": false, "listeners": 0,
 "devices": [{"id": 1, "name": "Chapel Mac", "created_at": 1794635908.34, "last_seen": 1794635908.35, "app_version": "1.4.0",
              "revoked_at": null}],
 "emails": [{"email": "bishop@example.org", "main": true, "added_at": 1794635908.33}],
 "own_key_codes": [],
 "meetings": [{"session_id": 1, "code": "20261109-100012", "started_at": 1794635908.37, "ended_at": 1794635908.38,
               "billing": "credits", "languages": ["fr", "es"], "language_minutes": {"es": 45.0, "fr": 45.0},
               "minutes": 90.0, "free_minutes": 60.0, "paid_minutes": 30.0, "cost_usd": 2.5}],
 "meetings_total": 1,
 "transactions": ["…its ledger rows, as in /api/admin/transactions…"],
 "next_before": "1794635908.3661845|usage:1"}
```

- `billing` is the same object the app and the operator page show. The free hour left is
  `billing.balance.free_left_min`.
- `meetings`: the newest first, including meetings with no speech (which show 0 minutes). `languages` are the
  languages offered, and `language_minutes` are the ones actually heard.
- `own_key_codes`: the codes this congregation used.
- `congregation.aliases`: the congregation's old slugs, oldest first. Each one redirects (301) to the current
  `slug` for good, so printed QR codes keep working. The list (`GET /api/admin/congregations`) has them too.
- `devices`: every Mac, revoked ones too (`revoked_at` set, listed last). `emails`: the main (billing) one first.
- For more transactions, call `/api/admin/transactions?congregation={id}&before=<next_before>`.

## POST /api/admin/congregations/{id}/billing  `{"billing": "credits" | "own_key" | "internal"}`

How the congregation pays. The body is `{"billing": …}` (what Siteboard sends); the older `{"mode": …}` still works,
and when both come `billing` wins. Exact values:

| `billing` | Who | Translation | Charged | Mac's Settings |
|---|---|---|---|---|
| `credits` | every new congregation | through the hub, Hear Church's API key | yes, prepaid; refused when out of credit | **Billing** tab: balance, buy credit |
| `own_key` | grandfathered churches, or unlocked by hand | straight from the Mac, the church's own key | no | **AI** tab (paste the key) |
| `internal` | Harrison's own congregations only (rare) | through the hub, Harrison's Claude Code login (`claude -p`), never the API key | no; never refused for balance | **Billing** tab reading "Included: no charge" (no balance, no buy) |

```
POST /api/admin/congregations/7/billing?key=…
{"billing": "internal"}
```
→
```json
{"ok": true,
 "billing": {"billing": "internal",
             "balance": {"metered_min": 120.0, "used_min": 0.0, "free_left_min": 60.0, "balance_min": 0.0,
                         "balance_usd": 0.0, "bought_usd": 0.0, "left_min": 60.0, "can_start": true},
             "balance_usd": 0.0, "language_hours_left": null, "out_of_credit": false, "usd_per_language_hour": 5.0,
             "free_hours_left": null, "paid_hours_left": null, "free_hours_per_month": 1.0, "free_resets_at": 1796083200.0,
             "bundles": [{"usd": 20, "hours": 4, "usd_per_hour": 5.0}, "…"],
             "can_buy": false, "buy_usd": [20, 50, 100, 250], "message": "Included: no charge."}}
```

The `billing` object is the same as `billing` in GET /api/admin/congregations/{id}. Errors:
- `400 {"detail": "billing: one of credits, own_key, internal."}` for any other value (wrong case, a number, missing).
- `404 {"detail": "No such congregation."}` for an unknown id. `403` without the operator key.
Setting the type it already has is fine.

- A meeting keeps the type it started under. The switch counts from the next meeting.
- `GET /api/admin/congregations` (each row's `billing`) and `GET /api/admin/congregations/{id}`
  (`congregation.billing` and `billing.billing`) show the type. `internal` is never shown on public pages.
- The Mac's engine follows the type it gets from `GET /api/hc/billing`. The Settings window asks again each time it
  opens. Switching to `own_key` doesn't put a key on the Mac: until someone pastes one in the AI tab, translation
  fails (the hub's proxy answers `409 own_key`). Switch only once the church has its key ready.
- `internal` needs a Claude Code login on the hub: `CLAUDE_CODE_OAUTH_TOKEN` in the hub's `data/secrets.env` (from
  `claude setup-token`), or the server user's `claude` login. The hub runs one `claude -p` process per language
  conversation (closed after 10 idle minutes, at most 24 at once) with `ANTHROPIC_API_KEY` removed from its
  environment, so it can't fall back to the paid key. Without a login, its turns fail with `502 unreachable`.
  Usage is metered as usual (`billing: "internal"`, `cost_usd: 0`), so stats stay right.
- Mac apps that know `internal` send `X-HC-Billing-Types: credits,own_key,internal` on their hub calls. Older apps
  (0.3.0 builds 18 and 19, which report `app_version` "0.3.0" for both, so the version can't tell them apart) don't
  send it and are told `billing: "credits"` instead (in /api/hc/billing, /api/hc/usage and the pairing answers):
  their engine then translates through the hub, which handles the congregation as `internal` anyway (no charge,
  never refused). Their Billing tab shows 0.0 hours and "Buying credit isn't available yet" until they update.
- Today `harlem-first` is `own_key` (grandfathered) and `riga-2` is `credits`.

## Listener link changes (the church does it)

The Mac changes the slug with `POST /api/hc/slug {slug}` (device token). There is no admin route for it. The old slug
goes into `slug_aliases` and redirects forever. A slug that is reserved (`RESERVED_SLUGS` in `app/hear/db.py`,
including `source` and the site's pages), already used, or another congregation's old slug is refused.

## E-mails and devices (V3.2)

### GET /api/admin/congregations/{id}/account

```json
{"congregation": {"id": 1, "slug": "riga", "name": "Riga Ward"},
 "main_email": "bishop@example.org",
 "emails": [{"email": "bishop@example.org", "main": true, "added_at": 1794635908.33},
            {"email": "clerk@example.org", "main": false, "added_at": 1794636000.1}],
 "devices": [{"id": 2, "name": "Clerk's Mac", "created_at": 1794636100.0, "last_seen": 1794636200.0,
              "app_version": "0.3.0", "revoked_at": null},
             {"id": 1, "name": "Chapel Mac", "created_at": 1794635908.34, "last_seen": 1794635908.35,
              "app_version": "0.2.1", "revoked_at": 1794637000.0}]}
```

404 for an unknown congregation. A congregation has 1 to 10 addresses. Exactly one is `main`: Stripe Checkout
sends its receipts there, and `congregation.email` elsewhere in this API is the same address. Congregations from
before V3.2 got their sign-up address as the main one. The church changes its addresses from the Mac (no admin
route for that).

### POST /api/admin/devices/{id}/revoke

→ `{"ok": true, "device": {…, "revoked_at": 1794637000.0}}`. 404 for an unknown device. Revoking again is fine.
The device's token stops working for good: its app calls get 403, a meeting it is running ends ("This Mac was
removed from the congregation."), and its link to the hub closes. The row stays for the history. To use that Mac
again, connect it with a new code (Add device or Email me a code).

## Older admin routes (unchanged)

`GET /api/admin/congregations` (the list, with balance), `POST /api/admin/congregations/{id}/credits
{payment_id, cents, minutes?}` (credit by hand, idempotent on payment_id; `minutes` defaults to the bundle at that
price, else $5 per hour, so a goodwill credit can be `{"payment_id": "goodwill-dec", "cents": 1, "minutes": 120}`), `POST /api/admin/congregations/{id}/hidden
{hidden}`. See the docstring at the top of `app/hear/api.py`.

## Limits

- The ledger is built from the tables on every call. That is fine for hundreds of congregations and thousands of
  meetings. Past that, it needs a real ledger table or caching.
- The refund log starts with this branch. Earlier refunds appear as one `"unlogged"` row per top-up.
- A refund that fails later at Stripe (`charge.refund.updated` → failed) doesn't give the credit back on the
  ledger. Add it by hand with the credits route.
