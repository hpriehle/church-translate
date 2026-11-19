// What the whole site links to. Only pages that are live belong here: a link to a page that isn't live is a 404.
// Each top-level path must also be in SITE_PAGES in app/hear/db.py (pages of this site) or RESERVED_SLUGS there (the
// hub's own pages, like /start and /account), so no congregation can take it.
export const NAV = [
  { href: "/how-it-works", label: "How it works" },
  { href: "/setup", label: "Setup" },
  { href: "/pricing", label: "Pricing" },
  { href: "/blog", label: "Blog" },
  { href: "/account", label: "Sign in" },
];

// The header's primary button: sign up on the hub (hearchurch.com/start, web/account/start.html).
export const CTA = { href: "/start", label: "Get started" };

export const FOOTER = [
  { href: "/", label: "Hear Church" },
  { href: "/how-it-works", label: "How it works" },
  { href: "/setup", label: "Setup guide" },
  { href: "/pricing", label: "Pricing" },
  { href: "/blog", label: "Blog" },
  { href: "/lds", label: "For Latter-day Saint wards" },
  { href: "/start", label: "Get started" },
  { href: "/account", label: "Sign in" },
  { href: "/download", label: "Download for Mac or Windows" },
  { href: "/listen", label: "Find your congregation" },
];
