# The money path of a new church

What a church that signs up today (from a cold e-mail) goes through, as the hub code on `deploy/trial` (c1a5570)
does it, with where each step lives and which test pins it. Units: a **language-hour** = one hour of speech
translated into one language someone listened to (2 languages × 1 h = 2 language-hours).

## 1. Sign-up

- Mac: `POST /api/hc/signup`; web: `/start` → `POST /api/hc/web/signup`. Both call `api.create_from_signup` →
  `Database.create_congregation`, billing `credits`, `free_trial = 1`.
- An own-key code at sign-up switches to `own_key` and clears `free_trial` (own key and internal never get a trial).
- **One trial per congregation (fixed on this branch):** the same church and name signing up again with an e-mail
  that already belongs to that congregation gets no trial (case and spaces ignored). Another unit on the same
  e-mail (a clerk with two wards) still gets one, as the Nov 16 decision says.
  **Known gap:** the same church under a different e-mail, or a renamed one, gets a new trial. Accepted (Maddie,
  Nov 19): tiny abuse risk at today's volume.
- A new Mac (pairing code, e-mailed link, connect code) joins the existing congregation: same row, same trial.

## 2. Free trial: 14 days or 20 language-hours, whichever first

`db.Database._walk` / `balance` (`TRIAL_DAYS = 14`, `TRIAL_SECONDS = 20 h`).

- The clock starts at the **first metered credit-billed meeting** (one with speech), not at sign-up.
- A meeting counts as trial if it **started** before day 14 and trial hours were left; it then uses trial time until
  the 20 h cap. Trial use is free and leaves the month's free hour and the credit alone.
- **Ends by hours mid-meeting:** the meeting goes on (the hub never cuts off a running meeting: `proxy.may_translate`
  lets the live Mac through). The overflow is billed like any meeting: the month's free hour, then credit, then the
  balance goes negative.
- **Ends by days:** a meeting that started on day 13.9 is all trial; one on day 14+ is billed normally.
- What the church sees during the trial: billing answer `free_trial {days_left, hours_left, ends_at, text}`,
  message "Free trial: N days and H hours left". After it: `free_trial: null` and the normal message.

## 3. Free monthly hour

`FREE_SECONDS = 3600` per UTC calendar month, counted by the meeting's **start** month; unused time doesn't carry
over. Trial use doesn't count against it, so a trial that crosses a month boundary leaves both months' free hours.

## 4. Out of hours: the next meeting is blocked

`Database.can_start`: credits congregations may start a meeting while the trial runs, or free hour + paid balance > 0.

- Operator (Mac): the hub answers the start with `session_refused` "<name> is out of credit (balance -$X). Buy credit
  to start the next meeting." (`live.Live._start`); the app shows it as its notice. Billing answer:
  `out_of_credit: true`, "Out of credit (balance …). This month's free hour is used up: buy credit …".
  Translation outside a live meeting gets `402 out_of_credit`.
- Listeners: no session goes live, so the listener page shows the congregation as not live (nothing is charged).
- A meeting already running, or resumed by the same Mac within 6 h, is never refused.

## 5. Buying a bundle (Stripe Checkout)

`POST /api/hc/checkout {usd}` (also `save_card`): only the bundle prices ($20/4 h, $50/11 h, $100/25 h, $250/70 h),
less a pending % off code. Works at 0 or negative hours (no balance check). 1-click (`/api/hc/topup`) charges a saved
card. The hub never credits from the redirect; only the webhook does.

## 6. Webhook credits the hours (idempotent)

`POST /api/stripe/webhook` (signed, 5-minute tolerance): `checkout.session.completed` (paid),
`checkout.session.async_payment_succeeded`, `payment_intent.succeeded` all credit through `credit_top_up` →
`Database.add_credit`, keyed on the **payment intent id**: a retry, a duplicate delivery or the other event for the same
payment (in either order) credits once. Hours come from the bundle metadata if it matches the amount paid, else the
bundle at that price, else $5/h. An overdraft is paid first from the new hours (−0.5 h + 4 h bundle = 3.5 h).
Bought hours never expire; during the trial they wait untouched.

## 7. Auto top-up

`payments.auto_topup`, after each meeting ends (`live.after_end`): on credits, auto top-up on, card saved, paid
hours left < 1 h → off-session charge of the chosen bundle, at most once per 24 h. **Skipped while the trial is
active**; the first meeting that ends after the trial ran out (by hours or days) triggers it. A failure turns it off
and e-mails the main address.

Limitation (not fixed, low impact): the check runs when the meeting ends; meter reports the Mac sends after that are
not seen until the next meeting ends. A church can only be blocked by this if those late reports alone take it from
≥ 1 h to ≤ 0, which the reporting cadence (every few seconds) doesn't allow in practice.

## 8. Refunds

- Operator: `POST /api/admin/refunds {payment_id, cents?}` refunds through Stripe; default = the unused part of that
  top-up at its own rate (`Database.unused_cents`). More is allowed with a warning (used hours are final by policy).
- Stripe dashboard refunds: `charge.refunded` → `Database.refund_credit` takes the refunded total off that top-up
  pro rata; idempotent and order-proof (the total only grows).
- No self-serve refund for churches.

## Tests

| Edge | Test | |
|---|---|---|
| Trial starts at the first metered meeting | `test_free_trial::test_the_clock_starts_at_the_first_metered_meeting` | existed |
| Trial ends by hours, overflow billed | `test_free_trial::test_the_trial_ends_at_20_language_hours_and_the_overflow_is_billed` | existed |
| Trial ends by hours mid-meeting, meeting not cut off, next one refused | `test_money_path::test_the_trial_ending_by_hours_mid_meeting_never_cuts_it_off` | new |
| Trial ends by days / between meetings | `test_free_trial::test_the_trial_ends_after_14_days_then_the_free_hour_counts`, `test_a_meeting_on_day_15_is_billed_normally` | existed |
| Not restarted by a new Mac | `test_money_path::test_a_new_mac_doesnt_restart_the_trial` | new |
| Not restarted by re-signup of the same church (+ clerk case, known gap) | `test_money_path::test_signing_up_again_doesnt_restart_the_trial` | new (bug fix) |
| Own key never gets a trial | `test_free_trial::test_own_key_*` | existed |
| Webhook retry / replayed signature | `test_stripe::test_a_signed_paid_checkout_credits_once`, `test_bad_and_replayed_signatures_are_refused` | existed |
| Checkout event then payment event | `test_cards_codes::test_checkout_can_save_the_card` | existed |
| Payment event first, then checkout events, duplicates | `test_money_path::test_a_payment_event_before_its_checkout_event_credits_once` | new |
| Checkout at 0 (negative) hours, then can start | `test_money_path::test_a_church_at_zero_hours_can_buy_and_start_again` | new |
| Hours bought during the trial wait | `test_money_path::test_hours_bought_during_the_trial_wait_for_its_end` | new |
| Auto top-up off during the trial, on after (by days) | `test_free_trial::test_auto_top_up_never_charges_during_the_trial` | existed |
| Auto top-up after the trial ends by hours | `test_money_path::test_auto_top_up_charges_once_the_trial_ends_by_hours` | new |
| Free hour resets at the month boundary | `test_bundles::test_months_are_utc_and_reset_on_the_first`, `test_one_free_hour_every_month_no_carry_over` | existed |
| Trial across a month boundary keeps both free hours | `test_money_path::test_a_trial_across_a_month_boundary_leaves_both_free_hours` | new |
| Meter only heard languages (Mac) | `test_metering::test_the_mac_meters_only_heard_languages_and_only_while_live` | existed |
| Meter once per language per chunk, capped, offered-but-unheard free (hub) | `test_money_path::test_only_the_languages_listened_to_are_metered_once_per_chunk` | new |
| Meeting refused when out of credit, resume allowed | `test_metering::test_the_hub_refuses_a_new_meeting_without_credit_but_never_a_resume` | existed |
| Refunds (webhook, admin, pro rata) | `test_refunds.py`, `test_bundles::test_a_refund_*`, `test_admin_refund_default_*` | existed |
