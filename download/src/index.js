// hearchurch.com's marketing site (built from site/), the Mac app's downloads and Sparkle files.
//
//   /download                  the download page (site/src/pages/download.astro), served like the other site pages
//   /download/HearChurch.dmg   always the newest DMG (the link to share); counted, anonymously, in D1
//   /download/<file>           a versioned DMG, Sparkle delta or the Wolof libraries from R2
//   /appcast.xml               Sparkle's update feed
//   /download/HearChurch-Intel.dmg, /appcast-intel.xml   the same for Intel Macs (separate build and feed)
//   /download/HearChurch-Setup.exe  always the newest public Windows installer (R2 windows/public/); counted like the DMG
//   /download/windows/<channel>/<file>  the Windows app's Velopack feeds, packages and installers
//   /privacy, /terms, /credits  the legal pages
//   /source, /source/<file>    the source code of the app's GPL/LGPL parts, per version (R2 source/*, from mac/release.sh)
//   /download/icon.png, *.woff2  the site's icon and fonts
//   /sitemap.xml               the pages we want in search
//   /<key>.txt                 IndexNow's key file, so search engines accept our URL submissions
//   /rr.js, /rrweb.min.js      Siteboard's tracker, loaded by every marketing page
//   /api/collect               forwards the tracker's batches to the hub
//
// R2 holds what mac/release.sh uploads: HearChurch-<version>.dmg, *.delta, wolof-libs-*.tar.gz, appcast.xml, latest.json,
// source/hearchurch-oss-<version>.tar.gz.
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
import { isPageView, point } from "./edge-log.js";

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
const SITE_PAGE = /^(\/(how-it-works|pricing|setup|lds|blog)(\/[a-z0-9-]+)?)?$/;
// Everything else this Worker answers itself.
const OWN = /^\/(download(\/.*)?|source(\/[\w.-]+)?|appcast(?:-intel)?\.xml|privacy|terms|credits|sitemap\.xml|rr\.js|rrweb\.min\.js|[0-9a-f]{32}\.txt)$/;
const SITE_PATHS = ["/", "/how-it-works", "/pricing", "/setup", "/lds", "/blog", "/blog/sacrament-meeting-translation", "/download"];

const SITEMAP = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${SITE_PATHS.map((p) => `<url><loc>https://hearchurch.com${p}</loc></url>`).join("\n")}
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

const site = {
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

    if (path === "/download") return sitePage(request, env, url);
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
    if (path === "/appcast-intel.xml") return file(request, env, "appcast-intel.xml", "application/xml", "max-age=300");
    if (path === "/download/HearChurch-Intel.dmg") {
      const latest = await latestRelease(env, "latest-intel.json");
      if (!latest) return new Response("Not released yet", { status: 404 });
      const response = await file(request, env, latest.file, "application/x-apple-diskimage", "no-store", latest.file);
      if (isNewDownload(request, response)) ctx.waitUntil(countDownload(env, `${latest.version}-intel`));
      return response;
    }
    if (path === "/download/HearChurch.dmg") {
      const latest = await latestRelease(env);
      if (!latest) return new Response("Not released yet", { status: 404 });
      const response = await file(request, env, latest.file, "application/x-apple-diskimage", "no-store", latest.file);
      if (isNewDownload(request, response)) ctx.waitUntil(countDownload(env, latest.version));
      return response;
    }
    // The link to share for Windows: the public channel's installer (docs/windows-release.md).
    if (path === "/download/HearChurch-Setup.exe") {
      const response = await file(request, env, "windows/public/HearChurchApp-public-Setup.exe", "application/octet-stream",
        "no-store", "HearChurch-Setup.exe");
      if (isNewDownload(request, response)) ctx.waitUntil(windowsVersion(env).then((v) => countDownload(env, `${v}-windows`)));
      return response;
    }
    // The written offer in THIRD_PARTY_NOTICES.md points here: every version's source archive stays available.
    if (path === "/source") return sourceList(env);
    if (path.startsWith("/source/")) {
      const name = path.slice("/source/".length);
      if (!/^hearchurch-oss-\d+\.\d+\.\d+\.tar\.gz$/.test(name)) return new Response("Not found", { status: 404 });
      return file(request, env, `source/${name}`, "application/gzip", "public, max-age=31536000, immutable", name);
    }
    // Windows (Velopack): feeds, packages and installers under R2 windows/<channel>/ (docs/windows-release.md).
    const win = path.match(/^\/download\/windows\/(dev|public)\/([\w.-]+)$/);
    if (win) {
      const [, channel, wname] = win;
      const key = `windows/${channel}/${wname}`;
      if (/^(releases|assets)\.[\w-]+\.json$/.test(wname) || /^RELEASES(-[\w-]+)?$/.test(wname))
        return file(request, env, key, wname.endsWith(".json") ? "application/json" : "text/plain", "max-age=300");
      if (wname.endsWith(".nupkg")) return file(request, env, key, "application/octet-stream", "public, max-age=31536000, immutable", wname);
      if (wname.endsWith("-Setup.exe")) {
        const response = await file(request, env, key, "application/octet-stream", "no-store", wname);
        if (isNewDownload(request, response)) ctx.waitUntil(countDownload(env, `windows-${channel}`));
        return response;
      }
      return new Response("Not found", { status: 404 });
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

// The source archives in R2, newest version first, as a plain page.
async function sourceList(env) {
  const listed = await env.FILES.list({ prefix: "source/" });
  const files = listed.objects
    .map((o) => ({ name: o.key.slice("source/".length), size: o.size }))
    .filter((f) => /^hearchurch-oss-\d+\.\d+\.\d+\.tar\.gz$/.test(f.name))
    .sort((a, b) => versionOf(b.name).localeCompare(versionOf(a.name), "en", { numeric: true }));
  const items = files.map((f) =>
    `<li><a href="/source/${escapeHtml(f.name)}">${escapeHtml(f.name)}</a> (${(f.size / 1e6).toFixed(0)} MB)</li>`).join("\n");
  const body = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Source code of the open-source parts - Hear Church</title></head><body style="font-family: system-ui, sans-serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5">
<h1>Source code of the open-source parts</h1>
<p>The Hear Church app for Mac includes Piper and eSpeak NG (GNU GPL v3), LAME and libsoxr (GNU LGPL), and in the Intel version FFmpeg (GNU LGPL v3) with x264 and x265 (GNU GPL v2). Each archive below holds their complete source code for that version of the app, the licence texts, and a README on how they were built. The app's own engine code is MIT-licensed and ships as source inside the app.</p>
${items ? `<ul>\n${items}\n</ul>` : "<p>No archives yet.</p>"}
<p>Questions, or to ask for the source on a physical medium: <a href="mailto:support@hearchurch.com">support@hearchurch.com</a>. <a href="/credits">Voice credits</a> · <a href="/download">Download</a></p>
</body></html>`;
  return html(body);
}

function versionOf(name) {
  return name.replace(/^hearchurch-oss-|\.tar\.gz$/g, "");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

async function latestRelease(env, key = "latest.json") {
  const obj = await env.FILES.get(key);
  return obj ? obj.json() : null;
}

// The public Windows release's version, for the download count: windows/public/latest.json ({"version": "0.4.0"}),
// written when a release is published, else the newest full package in Velopack's feed. "unknown" if neither says.
async function windowsVersion(env) {
  try {
    const latest = await latestRelease(env, "windows/public/latest.json");
    if (latest?.version) return latest.version;
    const feed = await latestRelease(env, "windows/public/releases.public.json");
    const versions = (feed?.Assets ?? []).filter((a) => a.Type === "Full" && a.Version).map((a) => a.Version);
    versions.sort((a, b) => compareSemver(b, a));
    if (versions[0]) return versions[0];
  } catch (e) {
    console.error("windows version failed:", e.message);
  }
  return "unknown";
}

// SemVer order, enough for Velopack's versions: 0.4.0-dev.57 < 0.4.0 < 0.4.1.
function compareSemver(a, b) {
  const [ca, pa] = a.split(/-(.*)/s), [cb, pb] = b.split(/-(.*)/s);
  const byCore = ca.localeCompare(cb, "en", { numeric: true });
  if (byCore) return byCore;
  if (!pa !== !pb) return pa ? -1 : 1;
  return (pa ?? "").localeCompare(pb ?? "", "en", { numeric: true });
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

// Every HTML page this Worker answers is logged into rr_pageviews (people,
// search bots, AI crawlers) for the hub's AI search panel; see
// local-website-engine ops/edge-log/README.md. Crawlers don't run JavaScript,
// so the tracker can't see them. Logging never changes the response.
export default {
  async fetch(request, env, ctx) {
    const res = await site.fetch(request, env, ctx);
    if (isPageView(request.method, res.status, res.headers.get("content-type"))) {
      try {
        const url = new URL(request.url);
        env.PAGEVIEWS?.writeDataPoint(point({
          slug: "hearchurch",
          path: url.pathname,
          referer: request.headers.get("referer"),
          host: url.hostname,
          ua: request.headers.get("user-agent") ?? "",
          utmSource: url.searchParams.get("utm_source") ?? "",
          country: request.cf?.country ?? "",
        }));
      } catch (err) { console.error("[edge-log]", err.message); }
    }
    return res;
  },
};
