# Trial e-mails

Four plain-text e-mails to a new congregation's sign-up address during and after its free trial
(`app/hear/trial_emails.py`). Sent through the existing Resend sender (`Hear Church <no-reply@hearchurch.com>`),
reply-to `maddie@hearchurch.com`, signed "Maddie / Hear Church". Wording adapted from the reply playbook's
drafts 10a/10b; facts checked against hearchurch.com/pricing and /setup on 2026-11-19. Prices come from
`db.BUNDLES` and trial terms from `db.TRIAL_DAYS` / `db.TRIAL_SECONDS`, so the text follows them if they change.

## Switch

`HC_TRIAL_EMAILS` in the hub's environment (read at start; restart the hub after changing it):

| value | what happens |
|---|---|
| unset (default) | nothing: no checks, no sends, no cutoff stored |
| `dry` | each e-mail that would go out is logged once per process (`trial e-mail (dry run) …`); nothing is sent or recorded, Resend not needed |
| `1` | sent through Resend; never without `RESEND_API_KEY` |

## Who gets them

Only congregations with the free trial (`free_trial = 1`, credits billing) whose e-mail is verified and that were
made at or after the **cutoff**: the time the hub first ran with `HC_TRIAL_EMAILS=1`, stored once in
`hub_settings.trial_emails_cutoff` and never moved. Congregations from before that never get any. Before the first
`=1`, a dry run counts from the hub's own start.

Each e-mail goes at most once per congregation (`trial_emails`: congregation, kind, sent_at; the row is written
before sending, removed again if Resend refuses, so the next check retries).

## When

Checked every 15 minutes by the hub, right after a sign-up is verified (Mac sign-up, hearchurch.com/start, or the
verify link) and when a meeting ends.

| kind | when |
|---|---|
| `welcome` | right after sign-up (once the e-mail is verified) |
| `day3` | 3 days after sign-up, if there has been no metered (credit-billed) meeting yet |
| `trial75` | trial still running and 75% used: day 10.5 of 14 since the first metered meeting, or 15 of 20 language-hours |
| `ended` | the trial is over (days or hours); a trial that jumps past 75% straight to the end gets only this one |

## Operator view

`GET /api/admin/trial-emails` (operator sign-in, like the other `/api/admin/*` routes) →
`{mode, cutoff (null until first enabled), counting_from, resend_configured, congregations: [{id, slug, name, email,
created_at, sent: {kind: unix time}, due: [kinds]}]}` for the congregations in scope, newest first.

## The e-mails

Shown for "Dakar Ward"; the 75% one fills in the days and hours actually left.

### welcome

Subject: Welcome to Hear Church

```
Hello,

Thanks for signing up Dakar Ward. Everything you need to get ready is here: https://hearchurch.com/setup

Before Sunday, do the 5-minute test: connect the cable from the sound board, press Start meeting, scan the QR code with your own phone, and have someone speak at the pulpit.

Your free trial is 14 days, up to 20 language-hours, whichever comes first. The 14 days start with your first translated meeting, and a test with a phone listening counts, so do it in the week you plan to start. No card is needed.

Questions? Just reply to this e-mail.

Maddie
Hear Church
```

### day3

Subject: Need a hand setting up before Sunday?

```
Hello,

Dakar Ward signed up a few days ago and hasn't had a translated meeting yet. Need a hand setting up before Sunday?

Your 14 free days haven't started: they begin with your first translated meeting, so nothing is wasted.

The 5-minute test: connect the cable from the sound board, press Start meeting, scan the QR code with your own phone, and have someone speak at the pulpit. The steps are here: https://hearchurch.com/setup

Stuck anywhere? Reply and I'll help, or we can do a 10-minute call.

Maddie
Hear Church
```

### trial75

Subject: Your free trial is three-quarters used

```
Hello,

A quick heads-up: the free trial for Dakar Ward has about 3 days and 11 language-hours left, whichever runs out first.

After that, nothing is charged; we never took a card. Every month you get one free language-hour, renewing on the 1st. For more, buy prepaid hours: $20 for 4 hours, $50 for 11, $100 for 25 or $250 for 70. They never expire, unused hours are refundable, and there's no monthly plan or contract. A meeting that's under way is never cut off.

You can buy hours any time at https://hearchurch.com/account

How has it gone for your members? I'd love to hear, and to fix anything that got in the way.

Maddie
Hear Church
```

### ended

Subject: Your free trial has ended

```
Hello,

The free trial for Dakar Ward has ended. Thank you for trying Hear Church.

Nothing is charged, and you can keep going. Every month you get one free language-hour, renewing on the 1st. For more, buy prepaid hours: $20 for 4 hours, $50 for 11, $100 for 25 or $250 for 70. They never expire, unused hours are refundable, and there's no monthly plan or contract. A meeting that's under way is never cut off.

You can buy hours any time at https://hearchurch.com/account

If anything got in the way, reply and tell me. I read every answer.

Maddie
Hear Church
```

