// What the whole site links to. Only pages that are live belong here: a link to a page that isn't live is a 404.
// Each top-level path must also be in SITE_PAGES in app/hear/db.py, so no congregation can take it.
export const NAV = [
  { href: "/how-it-works", label: "How it works" },
  { href: "/blog", label: "Blog" },
];

export const FOOTER = [
  { href: "/", label: "Hear Church" },
  { href: "/how-it-works", label: "How it works" },
  { href: "/blog", label: "Blog" },
  { href: "/lds", label: "For Latter-day Saint wards" },
  { href: "/download", label: "Download for Mac" },
  { href: "/listen", label: "Find your congregation" },
];
