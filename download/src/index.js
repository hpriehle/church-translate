// Download page and Sparkle files for the Hear Church Mac app.
//
//   /download                  the page (version and size from latest.json)
//   /download/HearChurch.dmg   always the newest DMG (the link to share); counted, anonymously, in D1
//   /download/<file>           a versioned DMG, Sparkle delta or the Wolof libraries from R2
//   /appcast.xml               Sparkle's update feed
//   /privacy, /terms, /credits  the legal pages
//   /download/icon.png, *.woff2  the page's icon and fonts
//   /sitemap.xml               the pages we want in search
//   /<key>.txt                 IndexNow's key file, so search engines accept our URL submissions
//   /rr.js, /rrweb.min.js      Siteboard's tracker; only the /download page loads it
//   /api/collect               forwards the tracker's batches to the hub
//
// R2 holds what mac/release.sh uploads: HearChurch-<version>.dmg, *.delta, wolof-libs-*.tar.gz, appcast.xml, latest.json.
import PAGE from "./page.html";
import { onRequestPost as collectPost, onRequest as collectOther } from "./collect.js";
import TRACKER from "./tracker/rr.min.js.txt";
import RECORDER from "./tracker/rrweb.min.js.txt";
import PRIVACY from "./privacy.html";
import TERMS from "./terms.html";
import CREDITS from "./credits.html";
import ICON from "../../mac/HearChurch/Resources/Assets.xcassets/AppIcon.appiconset/icon_512x512@2x.png";
import ALEGREYA from "./fonts/alegreya.woff2";
import ALEGREYA_SANS_400 from "./fonts/alegreya-sans-400.woff2";
import ALEGREYA_SANS_700 from "./fonts/alegreya-sans-700.woff2";

// The page's own files, served from here so a visit sends nothing to a font or image host.
const ASSETS = {
  "/download/icon.png": [ICON, "image/png"],
  "/download/alegreya.woff2": [ALEGREYA, "font/woff2"],
  "/download/alegreya-sans-400.woff2": [ALEGREYA_SANS_400, "font/woff2"],
  "/download/alegreya-sans-700.woff2": [ALEGREYA_SANS_700, "font/woff2"],
};

const INDEXNOW_KEY = "b8a8b0718483acd04cef33be1f6d395c";

// The marketing pages, built from site/ by Astro (docs/seo-site-structure.md). A page is live when its path matches
// here, has a route in wrangler.toml and is in the sitemap; site/src/site.js lists the same pages.
const SITE_PAGE = /^(\/(how-it-works|lds|blog)(\/[a-z0-9-]+)?)?$/;
// Everything else this Worker answers itself.
const OWN = /^\/(download(\/.*)?|appcast\.xml|privacy|terms|credits|sitemap\.xml|rr\.js|rrweb\.min\.js|[0-9a-f]{32}\.txt)$/;
const SITE_PATHS = ["/", "/how-it-works", "/lds", "/blog", "/blog/sacrament-meeting-translation"];

const SITEMAP = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${SITE_PATHS.map((p) => `<url><loc>https://hearchurch.com${p}</loc></url>`).join("\n")}
<url><loc>https://hearchurch.com/download</loc></url>
<url><loc>https://hearchurch.com/listen</loc></url>
</urlset>
`;

// A marketing page from the built site, or the site's own not-found page.
async function sitePage(request, env, url) {
  const res = await env.SITE.fetch(new Request(new URL(url.pathname || "/", url.origin), { method: request.method }));
  if (res.status !== 404) return res;
  const missing = await env.SITE.fetch(new Request(new URL("/404", url.origin)));
  return new Response(missing.body, { status: 404, headers: { "content-type": "text/html; charset=utf-8" } });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.hostname.startsWith("www.")) {
      url.hostname = url.hostname.slice(4);
      return Response.redirect(url.toString(), 301);
    }
    if (url.pathname === "/api/collect") {
      return request.method === "POST"
        ? collectPost({ request, env, waitUntil: (p) => ctx.waitUntil(p) })
        : collectOther();
    }
    const path = url.pathname.replace(/\/+$/, "");
    // A route like hearchurch.com/lds* also matches a congregation's /lds-harlem: anything that isn't ours goes on
    // to the hub on the VPS, untouched.
    if (!SITE_PAGE.test(path) && !OWN.test(path)) return fetch(request);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405, headers: { allow: "GET, HEAD" } });
    }
    if (SITE_PAGE.test(path)) return sitePage(request, env, url);

    if (path === "/download") return page(env);
    if (ASSETS[path]) {
      const [body, type] = ASSETS[path];
      return new Response(body, { headers: { "content-type": type, "cache-control": "public, max-age=86400" } });
    }
    if (path === "/rr.js") return script(TRACKER);
    if (path === "/rrweb.min.js") return script(RECORDER);
    if (path === `/${INDEXNOW_KEY}.txt`) {
      return new Response(INDEXNOW_KEY, { headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "max-age=3600" } });
    }
    if (path === "/sitemap.xml") {
      return new Response(SITEMAP, { headers: { "content-type": "application/xml; charset=utf-8", "cache-control": "max-age=3600" } });
    }
    if (path === "/privacy") return html(PRIVACY);
    if (path === "/terms") return html(TERMS);
    if (path === "/credits") return html(CREDITS);
    if (path === "/appcast.xml") return file(request, env, "appcast.xml", "application/xml", "max-age=300");
    if (path === "/download/HearChurch.dmg") {
      const latest = await latestRelease(env);
      if (!latest) return new Response("Not released yet", { status: 404 });
      const response = await file(request, env, latest.file, "application/x-apple-diskimage", "no-store", latest.file);
      if (isNewDownload(request, response)) ctx.waitUntil(countDownload(env, latest.version));
      return response;
    }
    // The site's audio samples (site/public/download/media/).
    if (path.startsWith("/download/media/")) return env.SITE.fetch(request);
    // generate_appcast names deltas after the app, so they have a space: "Hear Church4-3.delta".
    const name = path.slice("/download/".length).replaceAll("%20", " ");
    if (path.startsWith("/download/") && /^[\w. -]+\.(dmg|delta|tar\.gz)$/.test(name)) {
      const type = name.endsWith(".dmg") ? "application/x-apple-diskimage" : "application/octet-stream";
      return file(request, env, name, type, "public, max-age=31536000, immutable", name);
    }
    return new Response("Not found", { status: 404 });
  },
};

function script(body) {
  return new Response(body, { headers: { "content-type": "application/javascript; charset=utf-8", "cache-control": "public, max-age=3600" } });
}

function html(body) {
  return new Response(body, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "max-age=300" } });
}

// A download starting from the first byte. Resuming an interrupted one, or only asking about the file, is not new.
function isNewDownload(request, response) {
  if (request.method !== "GET" || (response.status !== 200 && response.status !== 206)) return false;
  const range = request.headers.get("range");
  return !range || /^bytes=0-/.test(range);
}

// Adds one to today's total for this version. It keeps nothing about who downloaded, and a failure here
// must never affect the download.
async function countDownload(env, version) {
  const day = new Date().toISOString().slice(0, 10);
  try {
    await env.STATS.prepare(
      "INSERT INTO downloads (day, version, count) VALUES (?1, ?2, 1) ON CONFLICT (day, version) DO UPDATE SET count = count + 1",
    ).bind(day, version).run();
  } catch (e) {
    console.error("download count failed:", e.message);
  }
}

async function latestRelease(env) {
  const obj = await env.FILES.get("latest.json");
  return obj ? obj.json() : null;
}

async function page(env) {
  const latest = await latestRelease(env);
  const fill = {
    VERSION: latest ? latest.version : "",
    SIZE: latest ? `${(latest.size / 1e9).toFixed(1)} GB` : "",
    DATE: latest ? new Date(latest.date).toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" }) : "",
    RELEASED: latest ? "" : "hidden",
    UNRELEASED: latest ? "hidden" : "",
  };
  const html = PAGE.replace(/\{\{(\w+)\}\}/g, (_, k) => escapeHtml(fill[k] ?? ""));
  return new Response(html, {
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "max-age=60" },
  });
}

// An R2 object, with Range support so big downloads can resume.
async function file(request, env, key, type, cache, downloadName) {
  const obj = request.method === "HEAD"
    ? await env.FILES.head(key)
    : await env.FILES.get(key, { range: request.headers, onlyIf: request.headers });
  if (!obj) return new Response("Not found", { status: 404 });

  const headers = new Headers({ "content-type": type, "cache-control": cache, "accept-ranges": "bytes", etag: obj.httpEtag });
  if (downloadName) headers.set("content-disposition", `attachment; filename="${downloadName}"`);
  if (!("body" in obj)) {   // HEAD, or a precondition (If-None-Match etc.) failed
    headers.set("content-length", String(obj.size));
    return new Response(null, { status: request.method === "HEAD" ? 200 : 304, headers });
  }
  if (obj.range && request.headers.has("range")) {
    const start = obj.range.offset ?? obj.size - obj.range.suffix;
    const length = obj.range.length ?? obj.size - start;
    headers.set("content-range", `bytes ${start}-${start + length - 1}/${obj.size}`);
    headers.set("content-length", String(length));
    return new Response(obj.body, { status: 206, headers });
  }
  headers.set("content-length", String(obj.size));
  return new Response(obj.body, { headers });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}
