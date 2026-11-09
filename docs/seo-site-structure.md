# Hear Church site structure (v3)

Decided with Harrison on 2026-11-09. This replaces both drafts in `~/Downloads`
(`hear-church-seo-site-structure.md` and the "v2" text). Those drafts were partly modelled on
a competitor's product, so where they disagree with this file, this file wins. Every claim on
the site must match what Hear Church actually does (see "Product facts").

## Decisions

| Question | Decision |
|---|---|
| Where marketing lives | `hearchurch.com/` is the marketing homepage and pillar page. There is no `/for-churches`. |
| Where listeners find a congregation | `hearchurch.com/listen` (the finder, moved from `/`). No `hearchurch.app` and no subdomain. |
| A congregation's own link | Stays `hearchurch.com/<slug>`, e.g. `/harlem1`, because it's easy to say from the pulpit. `/c/<slug>` keeps redirecting to it. |
| Audience pages | Top level: `/lds`, `/small-churches`, `/multilingual`. |
| Blog | `/blog/<title>`. |
| Main button | "Download for Mac" until billing ships, then "Start free" (the first hour is free). Second button: "Try the demo". |
| Demo | `/demo`, a recorded sample talk with captions and voice in each language, laid out like the listener page. |
| Pricing | Prepaid credits, $5 per language-hour. `/pricing` is built now but goes live **when billing ships**. |
| Silence | Not billed. Only minutes with speech count. This changes the billing plan in `hear-church-plan.md`, which says "deducted per minute while a meeting runs". |
| Language pages | The four supported languages, plus pages for planned ones, marked "coming soon". |
| Features page | Current features, plus a labelled "Planned" section. |
| Build | Astro, in this repo, served by the `hearchurch-download` Worker. Then register the site with local-website-engine for SEO, ranking and analytics. |
| "Join a meeting" banner | A thin bar at the top of every marketing page, linking to `/listen`. |

## URL map

```text
hearchurch.com/
  /                         Marketing homepage and pillar (Worker)
  /listen                   Congregation finder (VPS, moved from /)
  /<slug>                   A congregation's listener page, e.g. /harlem1 (VPS, unchanged)
  /c/<slug>                 Old listener link, 301 to /<slug> (VPS, unchanged)

  /how-it-works  /features  /setup  /faq  /demo
  /download                 Exists. Restyle to match the site.
  /pricing                  Built now, published when billing ships
  /lds  /small-churches  /multilingual
  /languages
  /languages/french  /languages/wolof  /languages/spanish  /languages/russian
  /languages/haitian-creole  /languages/portuguese  /languages/mandarin  /languages/korean
                            Planned languages, marked "coming soon"
  /compare  /compare/hope-translator  /compare/human-interpreters  /compare/google-translate
  /blog  /blog/<title>
  /privacy  /terms          Exist (Worker)
  /sitemap.xml  /robots.txt
```

### Reserved congregation slugs

Congregation links and top-level marketing pages share one namespace. A congregation that
signs up as `pricing` or `demo` would take that page. Before launch, add every top-level
marketing path to `RESERVED_SLUGS` in `app/hear/db.py`:

```text
listen how-it-works features setup faq demo download pricing lds small-churches
multilingual languages compare blog sitemap-xml robots-txt app
```

Do the same whenever a new top-level page is added, after checking that no congregation
already uses the name. None of the paths above were taken on 2026-11-09.

### Routing

- The Worker gets one route per marketing path, plus the exact route `hearchurch.com/` for the
  homepage. Everything else (`/listen`, `/<slug>`, `/api/*`, `/ws`, `/stream/*`) keeps going
  to the VPS.
- On the VPS, the site-mode `/` (the finder, `app/main.py` `listener_page`) moves to
  `/listen`. A ward's own Mac (app mode) keeps serving its listener page at `/`.
- Text that sends listeners to "hearchurch.com" changes to "hearchurch.com/listen". Today that
  is `download/src/page.html` step 3. Check the Mac app and the printed signs as well.

## Product facts the pages must match

- **The app:** a Mac app, for Apple silicon on macOS 14 or newer. Listeners use their phone's
  browser and install nothing.
- **Languages:** the operator sets the speaker's language by hand: English, French, Spanish or
  Russian. Listeners get French, Wolof, Spanish or Russian. English is offered while the speaker
  isn't speaking English. Wolof is listen-only.
- **Language switching isn't automatic.** Don't claim "no operator flipping settings".
- **Not built, so only under "Planned" or not at all:** broadcasting from a phone, summaries,
  conversation mode, ProPresenter or OBS output, captions on the main screen.
- **Pricing once billing ships:** $5 per language-hour, prepaid. Packs are discounted (e.g. 20
  hours for $80), the first hour is free, there's no monthly plan, and silence isn't billed.
  A running meeting is never cut off. Self-hosting with your own API key stays free on GitHub (MIT).
- **Until billing ships:** the app is free and the congregation adds its own Anthropic API key.
  That is what `/download` says today. Update it, and the privacy policy and terms, when
  credits launch.
- **No:** App Store or Google Play badges, "broadcaster signup", Supabase, `/join/<code>`
  links or native listener apps. Those assumptions in the drafts don't apply.

## Page rules

- One H1. Title tag `<keyword> | Hear Church` (50 to 60 characters). Meta description 140 to 160
  characters. A self-referencing canonical, and Open Graph tags specific to the page.
- Schema: `SoftwareApplication` on `/` and `/features`, `Article` on posts, `FAQPage` where a
  page has an FAQ.
- Every page is linked from a hub or the footer and has at least three working internal links.
  Never link to a page that isn't live.
- The footer links every core page, the audience pages, `/compare`, `/faq` and `/blog/`.
- `/lds` and LDS posts: "LDS" in slugs, titles and meta descriptions. "Latter-day Saint" and
  "The Church of Jesus Christ of Latter-day Saints" in body copy. One line saying Hear Church is
  independent and not affiliated with or endorsed by the Church.
- `/compare` pages: every row factual and dated. No competitor claim we can't show.
- The sitemap lists live marketing pages and `/listen`. Congregation pages are `noindex` and
  left out of the sitemap.
- **Analytics:** every marketing page loads the tracker, and the privacy policy says so ("our pages
  about Hear Church"). `/listen` and `/<slug>` never load it.

## Build order

**Phase 0: listener move**
1. Reserve the slugs above.
2. Move the finder to `/listen` and update the "go to hearchurch.com" text.

**Phase 1: launch**
3. Set up Astro in this repo and serve it from the Worker.
4. Build `/`, `/how-it-works`, `/lds`, `/demo`, `/blog/` and `/blog/sacrament-meeting-translation`,
   and restyle `/download`.
5. Add `sitemap.xml` and `robots.txt`, and submit the sitemap in Search Console.
6. Build `/pricing` but hold it back until billing ships.
7. Register the site with local-website-engine.

**Phase 2**
8. `/features` (with its Planned section), `/setup`, `/faq` and `/languages`, plus the four
   supported language pages with real samples.
9. Blog cluster 1, the pastor and tech-volunteer searches. Then the rest of the LDS cluster.

**Phase 3**
10. The planned language pages (coming soon), language guides, problem posts (including
    "don't pay for silence" once speech-only billing is real), `/compare`, `/small-churches`
    and `/multilingual`.

The blog topic list in the v2 draft still applies, apart from claims these facts rule out.

## Open items

- **Demo recording:** a talk we have permission to publish, with captions and voice in all
  four languages.
- **Billing plan:** update `hear-church-plan.md` from "per minute while a meeting runs" to
  speech minutes only.

## How it's built (Phase 1, 2026-11-09)

- `site/` is the Astro project. `download/wrangler.toml` builds it before every deploy and serves
  `site/dist` through the Worker (`sitePage` in `download/src/index.js`).
- To publish a page, add its path in three places: `SITE_PAGE` and `SITE_PATHS` in
  `download/src/index.js`, a route in `download/wrangler.toml`, and `site/src/site.js` if it
  belongs in the nav or footer. New top-level paths also go in `SITE_PAGES` in `app/hear/db.py`.
- Paths the Worker's routes catch but doesn't own, such as a congregation at `/lds-harlem`, are
  passed through to the VPS unchanged.
- `/pricing` is `site/src/pages/_pricing.astro`. The underscore keeps it out of the build until
  billing ships.
- `/demo` waits for a recording we have permission to publish.
