# Terms and privacy for credits: draft for Harrison's review (2026-11-13)

Branch `chat/hc-legal` (from `chat/hc-stripe`). Files: `download/src/terms.html`, `download/src/privacy.html`.
Nothing pushed or deployed. The wording is yours to approve.

## Open before publishing

1. **[SELLER NAME]** in terms ("Credit is sold by [SELLER NAME]."). The repo only names the Stripe account
   "Automaddie"; no legal entity (the LLC isn't formed). Terms still say "run by Harrison Riehle" and limit
   "Harrison Riehle's" liability: change both if the LLC becomes the seller.
2. **The hub doesn't relay translation text yet.** Privacy now says that on credits the text goes Mac → our server →
   Anthropic under our account, and isn't saved on the server. That is the plan (trial proxy, billing-status.md
   "Still missing"), not the code: today every meeting translates on the church's own key, straight from the Mac.
   Publish only once the proxy ships, and the proxy must not store or log the text.
3. **Refunds are by hand.** Nothing removes refunded credit from the ledger (no `charge.refunded` handling), so after a
   Stripe refund the balance must be corrected by hand, or that handling added.
4. **Effective dates** (terms Nov 5, privacy Nov 9) unchanged; set them on the day this goes live.
5. **Not in the terms (undecided):** taxes (Stripe Tax isn't set up), whether credit can move between
   congregations, what happens to unused credit if Hear Church stops (it's covered by "refund on request, anytime").
6. Outside these pages: `/pricing` and the download page say "Hymns, music and silence aren't counted". The code
   counts voiced seconds of any chunk where Whisper recognizes words, so sung words can count. Terms say only
   "Silence and stretches with no recognized words aren't counted". Not changed (out of scope).

## Terms: changed sentences

New section **Credits and refunds** (after "What Hear Church is"):
- **What it costs**: $5 per language-hour; two languages for an hour = two language-hours; first language-hour free
  per new congregation. (Code: `balance()`, free 60 language-minutes first, $5/language-hour.)
- **What is counted**: seconds of speech in which the Mac recognizes words; silence and no-word stretches not
  counted; a language counts only while at least one listener follows it by voice or captions. (`speech_taps`,
  `LiveSession.heard`.)
- **Buying credit**: in advance, USD, $5–$500 per purchase, by card through Stripe Checkout; sold by [SELLER NAME];
  we never see or store the card number. (`POST /api/hc/checkout`: card only, whole dollars 5–500.)
- **Running out**: a meeting under way is never stopped, so the balance can go below zero; a new meeting can't start
  until a purchase brings it back above zero; what was owed comes out of that purchase. (`can_start`,
  `left_min <= 0`; resumes from the starting Mac never refused.)
- **Credits don't expire.**
- **Refunds**: unused credit refunded on request anytime via support@hearchurch.com; used credit not refundable.

Changed bullet **If you use your own API key**: was "A congregation that set up Hear Church with its own Anthropic
API key uses that key instead of credit." Now: own key only with a one-time setup code from us; congregations set
up on their own key before credits began keep it; own-key meetings don't use credit. Rest of the bullet unchanged.

## Privacy: changed sentences

- **Short version**, server bullet: added "For congregations on credits, the text sent for translation also passes
  through our server on its way to the translation service, and isn't saved there either." (See open item 2.)
- **Short version**, new bullet: "Card payments are handled by Stripe. We never see or store your card number."
- **What our server keeps**: hashed codes now include "any one-time setup code" (`own_key_codes`, hashes only);
  meeting record adds "which Mac started it" (`sessions.device_id`); new bullet "For billing": billing mode, seconds
  of speech per meeting per listened-to language, each purchase (amount, date, Stripe payment ID); not what was
  said. (Tables `congregations.billing`, `speech`, `credits`.)
- **On your Mac**: "Your Anthropic API key and access token are stored in the Mac's Keychain" → the access token,
  and the API key only if the congregation uses its own.
- **Anthropic** bullet: on credits the text goes Mac → our server → Anthropic under our account; on own key the Mac
  sends it straight to Anthropic with that key, governed by the church's agreement with Anthropic.
- **New Stripe bullet**: Stripe processes purchases; card details and anything entered on Stripe's checkout page go
  to Stripe; link to Stripe's privacy policy; of what Stripe sends us we keep only the amount, payment ID and
  congregation. (The webhook payload also includes the buyer's email; the hub doesn't store it.)
