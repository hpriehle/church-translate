---
futurepacing: 1
slug: hear-church
title: Hear Church
goal: Hundreds of congregations every Sunday, profitable, runs without Harrison
artifact: https://claude.ai/artifact/32xnj9nNGEPrUAVg1mc8xt
---

# Future pacing — Hear Church

_Last pass: 2026-11-10 · Project is at **V1** overall (floor; download & updates is at V2)_

## V5 — the goal

**Hundreds of congregations, profitable, and fully self-serve.** In Harrison's words: hundreds
of congregations stream every Sunday and credits make it profitable, and any church finds it,
sets up, pays and runs its Sundays with zero help from Harrison.

In numbers: **200+ congregations stream in a typical week, credit revenue covers Claude, VPS,
bandwidth and Stripe with at least $800/month left over, and Harrison spends under an hour a week
on it.**

Reality check: the plan estimates about $4–5 profit per ward each month, so the margin only
arrives at roughly 200 wards, and only if most of them buy credits instead of bringing their own
free key. Mac-only also caps how many wards can join. The ladder is built to the goal as chosen.

Baseline measured 2026-11-09: 1 congregation and 1 device on the hub, 0 sessions started from the
app, 8 DMG downloads in total (0.1.0: 1, 0.1.1: 2, 0.1.2: 4, 0.1.4-intel: 1).

## How the project works

1. **Discovery** — the Astro marketing site at hearchurch.com (home, how it works, LDS page, one
   blog post), sitemap and IndexNow. Hands off: a pastor or clerk who clicks "Download for Mac".
2. **Download & updates** — a signed, notarized DMG from the Worker and R2 (Apple Silicon and
   Intel), Sparkle updates, `mac/release.sh`, an anonymous download counter. Hands off: an
   installed app that keeps itself current.
3. **Onboarding & sign-up** — in the app: church, congregation, building pin, meeting times,
   languages; email magic link, device token, connect code for a second Mac. Hands off: a
   congregation registered on the hub with a confirmed location.
4. **Claude access & payment** — today only the operator's own API key (or subscription token)
   in the Keychain; the "free first Sunday" button is a stub; no trial proxy, no billing. Hands
   off: permission to call Claude for this meeting.
5. **Sunday engine** — on the Mac: mic → VAD → Whisper → Claude → local voices, with preflight,
   mic watchdog, retries and keep-awake. Hands off: captions and MP3 per language over one
   WebSocket.
6. **Language catalog** — prompts, glossaries and voices per language; French, Wolof, Spanish and
   Russian released, Portuguese, Ukrainian, Persian, Nepali, Vietnamese and Chinese added
   2026-11-09 but unreleased; voices download on demand. Hands off: the languages a congregation
   can offer, and how good each sounds.
7. **Hub relay & listeners** — the VPS fans out each session; `/listen` finds congregations
   nearby, `/<slug>` and chapel QR codes open one directly. Hands off: people in the pews hearing
   the meeting in their language.
8. **Feedback & operations** — download counts in D1, site analytics, per-run cost reports on the
   Mac, an outbox mailer, a small admin page. Hands off: what to fix and what to build next.

## The ladder

### 1. Discovery _(today: V1)_

Axis: new congregations arriving each month without Harrison reaching out.

| | State | Proof |
|---|---|---|
| **V1** | The marketing site is live with four pages and one blog post. Nearly every download so far is Harrison's own or a test, and nobody outside his ward has found it. | 8 downloads in D1, 1 congregation on the hub. |
| **V2** | The full v3 site structure is live (audience pages, languages, demo, setup, FAQ), every listener page carries a "Get this for your church" link, and strangers start downloading. | Search Console shows the pages indexed; D1 shows 20+ downloads in a month that aren't Harrison's. |
| **V3** | Search and listener-page referrals bring a steady stream: the site ranks top-10 for its core terms ("sacrament meeting translation", "church translation app"). _(needs listeners V2)_ | Search Console positions; 100+ downloads/month; 10+ new congregations/month. |
| **V4** | A second channel works without Harrison posting by hand (e.g. LDS ward-clerk groups, mission offices, denominational newsletters), and `/pricing` is live. _(needs payment V3)_ | Analytics shows 25%+ of downloads from a non-search source; 300+ downloads/month. |
| **V5** | 40+ new congregations a month arrive on their own: enough to hold 200+ active weekly. | 40+ new verified congregations/month for three months with no outreach by Harrison. |

### 2. Download & updates _(today: V2)_

Axis: install reliability across strangers' Macs.

| | State | Proof |
|---|---|---|
| **V1** | A hand-built app that runs on Harrison's Mac; nobody else can install it. | Only a dev build exists. |
| **V2** | Signed, notarized DMGs for Apple Silicon and Intel, Sparkle updates, a one-command release script, an anonymous download counter. Nothing is checked on a clean machine, and nothing tells Harrison when an install fails. | `mac/release.sh` ships 0.1.4; the appcast serves it; D1 counts downloads. |
| **V3** | Every release is installed on a clean macOS user and updated from the previous version before publishing, and app crashes are reported home. | A release checklist entry per version; a deliberate test crash shows up in Harrison's reports. |
| **V4** | A beta channel gets each release one week before everyone else; update checks are counted per version, so you can see which builds are out there. | D1 shows update checks by version; at least one release has gone through beta first. |
| **V5** | Hundreds of Macs update silently; 90%+ are on the latest version within two weeks; install problems are not a support topic. | Update-check counts by version; zero install-related support emails in a month. |

### 3. Onboarding & sign-up _(today: V1)_

Axis: share of strangers who get from install to "ready for Sunday" with no help.

| | State | Proof |
|---|---|---|
| **V1** | The full onboarding flow exists (church, congregation, pin, times, languages, email link, connect code) but only Harrison has been through it; 1 congregation exists, and it isn't confirmed that real email goes out from production. | 1 row in `congregations`. |
| **V2** | Real email goes out from hearchurch.com, and an operator from a pilot ward sets up alone in under 20 minutes. | A pilot congregation with `email_verified = 1`, set up without a call or message to Harrison. |
| **V3** | The funnel is visible (install → sign-up → verified → languages downloaded → first session), and the API-key guide has screenshots; 60%+ of sign-ups reach "ready". _(needs operations V2)_ | The weekly digest shows each funnel stage. |
| **V4** | A rehearsal mode lets the operator hear the translation before Sunday; 75%+ of sign-ups hold a live meeting within 14 days. | Rehearsal sessions in the funnel; first-meeting rate in the digest. |
| **V5** | Hundreds of congregations have set up with no help from Harrison. | Fewer than one setup-related support email a week at 200+ active congregations. |

### 4. Claude access & payment _(today: V1)_

Axis: how a congregation pays for translation, from "bring your own key" to "it bills itself".

| | State | Proof |
|---|---|---|
| **V1** | Bring your own Anthropic key (or subscription token) is the only route. The "free first Sunday" button does nothing, and there is no trial proxy and no billing. | `OnboardingView.swift` stub; no `/api/trial` route. |
| **V2** | The trial proxy is live: a congregation's first free hour runs on Harrison's key through the VPS, metered per congregation, so a pilot ward can start without an API key. _(needs onboarding V2)_ | A pilot's first session has `trial = 1` and minutes metered per congregation. |
| **V3** | Prepaid credits at $5 a language-hour (speech minutes only) through Stripe, the balance shown on `/speak`, the negative-balance rule, an LLC, terms of service, and `/pricing` live. | A real card payment is credited and then used up by a real meeting. |
| **V4** | Auto top-up and receipts a clerk can submit; a failed charge never stops a meeting. | A threshold-triggered off-session charge in Stripe; a declined test card leaves the meeting running and sends the operator an email. |
| **V5** | Most active congregations are on credits, and revenue covers Claude, VPS, bandwidth and Stripe with $800+/month left over. _(needs discovery V5)_ | Monthly Stripe income minus Anthropic and VPS invoices is at least $800, three months running. |

### 5. Sunday engine _(today: V1)_

Axis: meetings run start to finish with nobody touching the Mac.

| | State | Proof |
|---|---|---|
| **V1** | The engine has run Harrison's ward for weeks, but from the dev checkout through the old server path. The packaged app has never run a real meeting through the hub. | `sessions` table on the hub: 0 rows. |
| **V2** | The packaged app runs a full real meeting through hearchurch.com in Harrison's ward with no one touching it. | A `sessions` row with a start and end time; a run report with no gaps. |
| **V3** | Auto-stop after silence; failures report home; 4 Sundays in a row in 2+ wards with zero operator intervention. _(needs operations V2)_ | Session rows for 4 Sundays × 2 wards; no failure reports. |
| **V4** | Proven on minimum-spec Macs (8 GB, Intel with the small model); recovers on its own from network drops and Claude outages; per-session latency and gap metrics are reported. | Sessions from 8 GB and Intel Macs in the reports; a forced network drop resumes without a click. |
| **V5** | 98%+ of 200+ weekly sessions finish with no operator intervention, measured centrally. | The weekly digest shows the success rate across every session. |

### 6. Language catalog _(today: V1)_

Axis: languages at a quality a native speaker accepts, and commercially clean.

| | State | Proof |
|---|---|---|
| **V1** | French and Wolof are proven in real meetings. Spanish and Russian have shipped but haven't been used in a real meeting. Six more were added today and haven't been released. | Release notes for 0.1.3; today's commits. |
| **V2** | All 10 languages are in a release, and each has been checked by one native speaker on a recorded talk. | A review note per language in the repo. |
| **V3** | Listeners can rate translation quality; every language has a score; the request-a-language form decides what comes next. _(needs listeners V2)_ | A rating per language in the digest; request counts by language. |
| **V4** | Every voice is licensed for paid sessions (no MMS in credit sessions), and the next requested languages (e.g. Haitian Creole, Korean) are in. _(needs payment V3)_ | THIRD_PARTY_NOTICES covers every voice for commercial use. |
| **V5** | Any language requested by 3+ congregations ships within a month, above a set quality score, with an owned voice where licensing requires it. | Median request-to-release time under 30 days; every language above the threshold. |

### 7. Hub relay & listeners _(today: V1)_

Axis: Sunday-peak scale with nobody watching.

| | State | Proof |
|---|---|---|
| **V1** | One VPS with SQLite serves one ward's listeners through the old path. The multi-congregation code and `/listen` are live but haven't relayed a single session from the app. | 1 congregation, 0 app sessions. |
| **V2** | Several congregations stream at the same time in production; pilot chapels have printed QR codes; listeners see "reconnecting" instead of silence. _(needs engine V2)_ | 3+ sessions overlapping on one Sunday. |
| **V3** | Load-tested for a Sunday peak (50 sessions × 20 listeners, about 64 Mbps); an alert reaches Harrison if the hub drops on a Sunday morning. | A load-test report; a test alert arrives on his phone. |
| **V4** | Audio goes out through a CDN or relay, deploys cause no downtime, and Sunday-morning uptime is at least 99.9%. | Uptime monitor history over 8 Sundays. |
| **V5** | 200+ congregations at the Sunday peak and thousands of listeners, with nobody watching. | Peak concurrent sessions and listeners on the Sunday dashboard, with no incidents. |

### 8. Feedback & operations _(today: V1)_

Axis: how little of Harrison it takes to know what's happening and act on it.

| | State | Proof |
|---|---|---|
| **V1** | Download counts, site analytics, and cost reports kept on the Mac. Harrison finds out about problems by being in the meeting, and email may still be landing in the outbox log. | No view across congregations exists. |
| **V2** | A Monday digest covers sign-ups, sessions, minutes, cost and errors for every congregation. | The digest arrives every Monday. |
| **V3** | App errors are collected centrally, a support inbox exists, and a setup guide and FAQ answer the common questions. | Error reports grouped by type; the support inbox and FAQ are live. |
| **V4** | An alert fires when a meeting fails partway through, and admin tools can hide a bad entry or take a refund action without SSH. | A forced failure triggers an alert; admin actions are logged. |
| **V5** | Harrison spends under an hour a week; the digest tells him what to build next. | A month of the time log or support count stays under the bar at 200+ active congregations. |

## Where the project actually is

| Piece | Today | Gap to V5 |
|---|---|---|
| Discovery | V1 | 4 rungs |
| Download & updates | V2 | 3 rungs |
| Onboarding & sign-up | V1 | 4 rungs |
| Claude access & payment | V1 | 4 rungs |
| Sunday engine | V1 | 4 rungs |
| Language catalog | V1 | 4 rungs |
| Hub relay & listeners | V1 | 4 rungs |
| Feedback & operations | V1 | 4 rungs |

The packaging work is ahead of everything else. **The Sunday engine and onboarding are what hold
the project at V1**: no real meeting has gone through the app and hub yet, and nobody but Harrison
has set it up. Discovery and payment can't climb in any meaningful way until those two do.

---

## Build plan

Effort tags: **hours**, **days** or **weeks** of work. Rungs marked _external_ depend on
someone or something outside Harrison's control, so they can't be scheduled like ordinary work.
Anything that touches a real meeting can only be proven on a Sunday, and deploys never happen on
a Sunday.

### 1. Discovery: V1 → V5

- **V1 → V2** _(days)_ — Build: the rest of the v3 URL map (`/lds` exists; add `/small-churches`,
  `/multilingual`, `/languages/*`, `/setup`, `/faq`, `/demo`), a "Get Hear Church for your church"
  footer on every listener page, Search Console verification and an IndexNow push. Check: the
  pages are indexed, and D1 shows 20+ downloads in a month that aren't Harrison's.
- **V2 → V3** _(weeks, external: Google)_ — Build: one blog post a week aimed at a single search
  term, plus the `/compare/*` pages. Check: top-10 in Search Console for the core terms; 100+
  downloads and 10+ new congregations a month. Needs listeners V2, so the listener-page footer
  reaches real pews.
- **V3 → V4** _(weeks, external: group admins)_ — Build: a repeatable post-and-share kit for ward
  clerks and mission offices (a short video, a one-page PDF, a QR poster), with `/pricing` switched
  on. Check: 25%+ of downloads come from a non-search source; 300+ downloads a month. Needs
  payment V3.
- **V4 → V5** _(weeks, external)_ — Build: whatever the referral data shows is working, doubled
  down on, with no outreach by hand. Check: 40+ new verified congregations a month, three months
  running.

### 2. Download & updates: V2 → V5

- **V2 → V3** _(days)_ — Build: a release checklist step that installs the DMG under a fresh
  macOS user and updates from the previous version, and crash reports sent to the hub (Swift
  crash handler plus an engine exception hook, posted to `/api/hc/report`). Check: a deliberate
  test crash appears in the reports.
- **V3 → V4** _(days)_ — Build: a beta appcast that opt-in operators follow, and update checks
  counted per version in D1. Check: one release goes to beta first; D1 shows checks by version.
- **V4 → V5** _(hours, then ongoing)_ — Build: release-blocking smoke tests in `release.sh`.
  Check: 90%+ on the latest version within two weeks; no install-related support email in a
  month.

### 3. Onboarding & sign-up: V1 → V5

- **V1 → V2** _(days, external: a pilot operator)_ — Build: confirm SMTP is sending from
  production (or wire up a transactional mail service), then recruit one pilot ward and hand over
  only the download link. Check: a pilot congregation with `email_verified = 1`, set up without a
  call or message to Harrison.
- **V2 → V3** _(days)_ — Build: funnel events on the hub (installed, signed up, verified,
  languages ready, first session) reported in the Monday digest; screenshots in the API-key
  guide; moving the building pin after setup. Check: the digest shows the funnel, and 60%+ of
  sign-ups reach "ready". Needs operations V2.
- **V3 → V4** _(days)_ — Build: a rehearsal mode that plays a sample talk through the operator's
  own languages, without publishing to listeners. Check: 75%+ of sign-ups hold a live meeting
  within 14 days.
- **V4 → V5** _(ongoing)_ — Build: fix whatever the funnel shows as the biggest drop, one step
  at a time. Check: fewer than one setup support email a week at 200+ active congregations.

### 4. Claude access & payment: V1 → V5

- **V1 → V2** _(days)_ — Build: `POST /api/trial/translate` on the VPS holding Harrison's key,
  metered per congregation (speech minutes only), capped at the first hour, and rate-limited;
  wire up the "free first Sunday" button. Check: a pilot's first session has `trial = 1` with
  metered minutes. Needs onboarding V2.
- **V2 → V3** _(weeks, external: LLC filing, Stripe approval)_ — Build: an LLC; Stripe and Stripe
  Tax; a credit ledger at $5 a language-hour that the trial meter already feeds; the balance and
  the negative-balance rule on `/speak`; terms, refund policy and `/pricing` live. Check: a real
  card payment is credited and then used up by a real meeting.
- **V3 → V4** _(days)_ — Build: a saved card with a threshold and a pack size, off-session
  charges, emailed receipts, and the "failed charge never stops a meeting" path. Check: a
  threshold-triggered charge in Stripe; a declined test card leaves the meeting running and
  emails the operator.
- **V4 → V5** _(external: volume)_ — Build: nothing new; this rung arrives with discovery V5.
  Check: Stripe income minus Anthropic and VPS invoices is at least $800 a month, three months
  running.

### 5. Sunday engine: V1 → V5

- **V1 → V2** _(days + one Sunday)_ — Build: move Harrison's own ward onto the packaged app.
  Connect the app to congregation #1, do a weekday dry run against production with a recorded
  talk, then run Sunday from the app with the old server path ready as a fallback. Check: a
  `sessions` row with a start and end time, and a run report with no gaps.
- **V2 → V3** _(days + four Sundays, external: pilot wards)_ — Build: auto-stop after about
  10 minutes of silence, and failure reports home (shared with download V3). Check: 4 Sundays in a
  row in 2+ wards with no intervention. Needs operations V2.
- **V3 → V4** _(weeks)_ — Build: test runs on an 8 GB Mac and an Intel Mac; automatic recovery
  from a network drop and from a Claude outage; latency and gap numbers in each report. Check: a
  forced network drop resumes without a click; sessions from minimum-spec Macs appear in the
  reports.
- **V4 → V5** _(ongoing)_ — Build: fix the top failure cause from the weekly digest each week.
  Check: 98%+ of sessions finish without intervention, across 200+ a week.

### 6. Language catalog: V1 → V5

- **V1 → V2** _(days, external: native speakers)_ — Build: ship the six new languages in the next
  release (this also lets the held `site` branch deploy), and have one native speaker per language
  review a recorded talk. Check: a review note for each language in the repo.
- **V2 → V3** _(days)_ — Build: a one-tap rating on the listener page, with per-language scores
  and request-a-language counts in the digest. Check: every language has a score. Needs
  listeners V2.
- **V3 → V4** _(weeks)_ — Build: a licence audit of every voice for paid use (MMS off in credit
  sessions), then the top two requested languages. Check: THIRD_PARTY_NOTICES clears every voice
  for commercial use. Needs payment V3.
- **V4 → V5** _(weeks per language, external: voice talent for an owned voice)_ — Build: a
  repeatable add-a-language checklist (prompt, glossary, voice, reviewer). Check: requests reach
  release in under 30 days on median.

### 7. Hub relay & listeners: V1 → V5

- **V1 → V2** _(days, external: pilot wards)_ — Build: printed QR posters for pilot chapels, and a
  "reconnecting" state on the listener page that has been tested. Check: 3+ sessions overlapping
  on one Sunday. Needs engine V2.
- **V2 → V3** _(days)_ — Build: a load-test script (50 sessions × 20 listeners) run against
  production on a weekday, plus an uptime check that alerts Harrison's phone on Sunday mornings.
  Check: a load-test report; a test alert arrives.
- **V3 → V4** _(weeks)_ — Build: audio sent out through a CDN or relay, and deploys with no
  downtime. Check: 99.9%+ Sunday-morning uptime over 8 Sundays.
- **V4 → V5** _(ongoing)_ — Build: scale the relay ahead of the digest's peak numbers. Check:
  200+ concurrent congregations at the Sunday peak with no incidents.

### 8. Feedback & operations: V1 → V5

- **V1 → V2** _(hours)_ — Build: a Monday job on the VPS that emails Harrison sign-ups, sessions,
  minutes, cost and errors for every congregation. Check: the digest arrives on Monday.
- **V2 → V3** _(days)_ — Build: error reports grouped by type in the digest (from download V3), a
  support address on the site, and `/faq` and `/setup` written from the first pilots' questions.
  Check: error groups appear; the support inbox and FAQ are live.
- **V3 → V4** _(days)_ — Build: an alert when a session drops partway through a meeting, and
  admin actions (hide, refund, reset trial) on the admin page. Check: a forced failure alerts
  Harrison; admin actions are logged.
- **V4 → V5** _(ongoing)_ — Build: an auto-reply that sends people to the FAQ, plus a time log.
  Check: under an hour a week at 200+ active congregations.

## Sequence

**Everything to V2**
1. Sunday engine: move your own ward onto the packaged app (weekday dry run, then Sunday) — days + 1 Sunday
2. Feedback & operations: Monday digest — hours
3. Onboarding: confirm production email, recruit one pilot ward — days, _external_
4. Language catalog: ship the six new languages and line up native-speaker reviews — days, _external_
5. Claude access & payment: trial proxy and the "free first Sunday" button — days
6. Hub relay & listeners: QR posters, a tested "reconnecting" state, 3 overlapping sessions — days, _external_
7. Discovery: finish the v3 site, listener-page footer, Search Console — days

**Everything to V3**
1. Download & updates: clean-user install test and crash reports (shared with the engine) — days
2. Feedback & operations: error groups, support inbox, FAQ and setup pages — days
3. Onboarding: funnel in the digest, key-guide screenshots, moving the building pin — days
4. Hub relay & listeners: load test and Sunday uptime alert — days
5. Language catalog: listener ratings and request counts — days
6. Sunday engine: auto-stop, then 4 clean Sundays in 2+ wards — days + 4 Sundays, _external_
7. Claude access & payment: LLC, Stripe, credits, terms, `/pricing` — weeks, _external_
8. Discovery: a weekly blog post and the compare pages until the rankings come — weeks, _external_

**V4 / V5**
1. Claude access & payment V4 (auto top-up), onboarding V4 (rehearsal mode), operations V4 (alerts, admin)
2. Download V4 (beta channel), engine V4 (minimum-spec Macs, automatic recovery), languages V4 (licences, next languages)
3. Discovery V4 (a second channel), listeners V4 (CDN, no-downtime deploys)
4. V5 everywhere is mostly volume: it arrives as discovery brings congregations in, with the digest steering the fixes

## First move

**Run your own ward from the packaged app.** This week, connect Hear Church 0.1.4 to congregation
#1, do a weekday dry run against hearchurch.com with a recorded talk, and run this Sunday's
meeting from the app with the old server path ready as a fallback. It's first because the
listener, onboarding and operations rungs all wait on the first real session through the app and
hub, and the hub still shows 0.
