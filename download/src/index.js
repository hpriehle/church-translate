// Download page and Sparkle files for the Hear Church Mac app.
//
//   /download                  the page (version and size from latest.json)
//   /download/HearChurch.dmg   always the newest DMG (the link to share)
//   /download/<file>           a versioned DMG or Sparkle delta from R2
//   /appcast.xml               Sparkle's update feed
//
// R2 holds what mac/release.sh uploads: HearChurch-<version>.dmg, *.delta, appcast.xml, latest.json.
import PAGE from "./page.html";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.hostname.startsWith("www.")) {
      url.hostname = url.hostname.slice(4);
      return Response.redirect(url.toString(), 301);
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405, headers: { allow: "GET, HEAD" } });
    }
    const path = url.pathname.replace(/\/+$/, "");

    if (path === "/download") return page(env);
    if (path === "/appcast.xml") return file(request, env, "appcast.xml", "application/xml", "max-age=300");
    if (path === "/download/HearChurch.dmg") {
      const latest = await latestRelease(env);
      if (!latest) return new Response("Not released yet", { status: 404 });
      return file(request, env, latest.file, "application/x-apple-diskimage", "no-store", latest.file);
    }
    const name = path.slice("/download/".length);
    if (path.startsWith("/download/") && /^[\w.-]+\.(dmg|delta)$/.test(name)) {
      const type = name.endsWith(".dmg") ? "application/x-apple-diskimage" : "application/octet-stream";
      return file(request, env, name, type, "public, max-age=31536000, immutable", name);
    }
    return new Response("Not found", { status: 404 });
  },
};

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
