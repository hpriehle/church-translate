# Pricing research: Hope Translator and good usage-based pricing pages

Researched 2026-11-15 for the /pricing redesign (Harrison, Nov 14: "needs to be a better pricing card… what is the hope translation pricing?"). Hear Church's price stays as decided on 2026-11-08: $5 per heard language-hour, first language-hour free. Anything below that suggests a different price is a recommendation only.

## Hope Translator (hopetranslator.com)

`hopetranslation.com` doesn't resolve. The church live-translation product is **Hope Translator**, made by Hope Harbor, LLC. Its App Store name is "Live Voice Translator: Hope". The speaker broadcasts from a phone or laptop, and listeners scan a QR code with no app. Captions can go to OBS or ProPresenter. Per [faith.tools](https://faith.tools/app/hope-translator-live-sermon-translation), a third-party directory, it offers 103 output languages, 81 of them with audio.

The prices below were checked on the vendor page, [hopetranslator.com/pricing](https://hopetranslator.com/pricing), on 2026-11-15:

| Plan | Monthly | Yearly | Hours/mo | Languages at once | Audience |
|---|---|---|---|---|---|
| Free | $0 | – | 0.5 | not stated | up to 5 phone listeners |
| Stream Starter | $19.99 | $167 | 5 | up to 2 | up to 10 |
| Stream Growth 10h | $39.99 | $335 | 10 | up to 5 | up to 50 |
| Stream Growth 20h | $69.99 | $587 | 20 | not stated | not stated |
| Stream Growth 40h | $129.99 | $1,090 | 40 | not stated | not stated |
| Stream Scale | $99.99 | $840 | 5 | unlimited | unlimited reading, up to 100 listening |
| Stream Enterprise | $499.99 | $4,000 | 50 | unlimited | unlimited reading, up to 1,000 listening |
| Contact us | custom | custom | custom | custom | custom |

- **Yearly** billing is labelled "30% discount".
- **Free tier:** 30 minutes a month. There is no other trial.
- **Hours** reset each calendar month. The page doesn't say whether plan hours are counted per language or in total, and that is not verified.
- **Translator Credits** (Scale and above only): 1 credit = 1 minute in 1 output language, at $0.05 each. That is **$3.00 per language-hour**. Credits are used only after plan hours run out, and they never expire.
- **Listener caps:** the lower tiers cap the audience (5, 10 or 50 listeners), and spoken audio is capped separately from reading captions.
- **App Store prices:** the [App Store listing](https://apps.apple.com/app/6738706131) shows in-app purchases from $7.99 to $167.99 under repeated names. It is unclear what each one buys, so use the website prices.

### Other church translation tools

| Product | Price | Source |
|---|---|---|
| Church Translation | Usage-based, no subscription. 50,000 characters free with no card. Then $0.01 per 150 characters per language, about **$3.33 per language-hour**. Unlimited listeners. | [vendor help page](https://churchtranslation.featurebase.app/help/articles/9341805-pricing-and-whats-included), verified |
| Sunflower AI | $90/mo for 7.5 h and 5 languages; $200/mo for 30 h and 10 languages | search summary only; the pricing page returned 404 |
| OneAccord | from $150/mo | search summary only |
| Vozo | from $79/mo for 180 min per language | [Vozo blog](https://www.vozo.ai/blogs/sermon-translation-software), not verified |
| Voco | £24, £48 or £88 a month | [Capterra listing](https://www.capterra.com/p/10049324/Voco-Translate/), not verified |
| Spf.io, Wordly | quote only | – |

### How Hear Church compares

- **Usage-based rates:** Hope charges $3.00 per language-hour on its credits and Church Translation about $3.33. Hear Church charges $5. That rate counts only speech, and only in languages someone is listening to, so silence, songs with no recognized words and unheard languages are free. Hope's credits also need a $99.99/mo Scale plan first.
- **Subscriptions:** Hope's Starter plan works out to about $4 an hour for up to 2 languages at once, *if* its hours are not multiplied by languages.
- **What Hear Church has that Hope's paid tiers lack:**
  - no subscription at all
  - no listener cap
  - refunds of unused credit
  - a free first language-hour, against Hope's 30 minutes a month
- **What Hope has that Hear Church lacks:** a recurring free allowance, and the yearly discount.

**Recommendation (not applied):** keep $5. Price is not where Hear Church wins; no monthly fee and no listener cap are. Those are now said on the page. If price objections come up in pilots, there are two options worth testing:
- a prepaid pack discount, for example $100 of credit for $90, rather than a lower base rate
- naming "unlimited listeners" outright, after checking that the relay really has no cap

## Usage-based pricing pages that work

| Page | What makes it work |
|---|---|
| [Deepgram](https://deepgram.com/pricing) | "Free to start" with credit and no card. Three plan cards: Pay as you go, prepaid Growth, Enterprise. A short "what counts" note: per second, no rounding. The FAQ converts rates into cost per hour. |
| [Vercel](https://vercel.com/pricing) | Free, Pro and Enterprise columns. Each one lists what's included and the overage rate. Spend limits are shown up front, which takes away the fear of a surprise bill. |
| [Twilio SMS](https://www.twilio.com/en-us/sms/pricing/us) | Defines the unit exactly (160-character segments) and dates the page ("pricing current as of…"). It also shows the downside of no calculator: carrier fees make the real cost hard to guess. |
| [Stripe](https://stripe.com/pricing) | One headline rate, with "no setup, monthly or hidden fees" stated up front and repeated in the FAQ. |
| [Planning Center](https://www.planningcenter.com/pricing) | The church SaaS example. "Only pay for what your church needs", "no contracts" badges, and an "ALWAYS FREE" tag. It is the only one of these with a live calculator: you size your church and see the monthly total. |

Patterns used on the new /pricing page:

1. **Three cards, with the middle one featured.** Free first hour, Pay as you go at $5 (marked "How most churches pay"), and Your own key by invitation.
2. **Reassurances in the card itself.** Credit never expires, refund of unused credit anytime, no monthly plan, a meeting is never cut off.
3. **A small calculator.** Minutes of speaking × languages heard × meetings a month gives the cost per meeting and per month. It starts at 50 minutes, 2 languages and 4 meetings: $8.33 a meeting, $33.33 a month.
4. **A "What counts" list in two columns.** Counted: recognized speech, recognized and translated sung words, each language while someone follows it. Not counted: silence and pauses, music with no recognized words, untranslated parts, unheard languages, listeners.
5. **A worked example in the FAQ, plus the seller line.** "Credit is sold by Automaddie LLC; payments go through Stripe", linked to the terms.
