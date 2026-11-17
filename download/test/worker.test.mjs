// The download Worker against a stubbed R2 bucket and site, in plain Node (no wrangler needed).
//   npx -y node@24 --test download/test/worker.test.mjs
// It bundles src/index.js with esbuild the way wrangler does (.html/.txt as text, .png/.woff2 as bytes).
import { test, before } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
let worker;

before(() => {
  const out = join(mkdtempSync(join(tmpdir(), "hc-worker-")), "worker.mjs");
  execFileSync("npx", ["-y", "esbuild@0.24", join(here, "../src/index.js"), "--bundle", "--format=esm",
    "--platform=neutral", "--loader:.html=text", "--loader:.txt=text", "--loader:.png=binary",
    "--loader:.woff2=binary", `--outfile=${out}`, "--log-level=error"], { stdio: "inherit" });
  return import(pathToFileURL(out)).then((m) => { worker = m.default; });
});

const ARCHIVE = new Uint8Array([31, 139, 8, 0]);
const objects = {
  "source/hearchurch-oss-0.3.0.tar.gz": ARCHIVE,
  "source/hearchurch-oss-0.10.0.tar.gz": ARCHIVE,
  "source/hearchurch-oss-0.9.1.tar.gz": ARCHIVE,
  "source/notes.txt": ARCHIVE,
  "windows/dev/releases.dev.json": ARCHIVE,
  "windows/dev/HearChurchApp-0.1.0-dev.13-full.nupkg": ARCHIVE,
  "windows/dev/HearChurchApp-dev-Setup.exe": ARCHIVE,
};
const r2Object = (key) => ({ key, size: objects[key].length * 5e6, httpEtag: `"${key}"` });
const env = {
  FILES: {
    async list({ prefix }) {
      return { objects: Object.keys(objects).filter((k) => k.startsWith(prefix)).map(r2Object), truncated: false };
    },
    async head(key) { return objects[key] ? r2Object(key) : null; },
    async get(key) { return objects[key] ? { ...r2Object(key), body: objects[key] } : null; },
  },
  SITE: { async fetch(req) { return new Response(`site ${new URL(req.url).pathname}`, { headers: { "content-type": "text/html" } }); } },
};
const ctx = { waitUntil() {} };
const get = (path, method = "GET") => worker.fetch(new Request(`https://hearchurch.com${path}`, { method }), env, ctx);

test("/source lists the archives, newest first, and skips other files", async () => {
  const res = await get("/source");
  assert.equal(res.status, 200);
  const body = await res.text();
  const order = [...body.matchAll(/href="\/source\/(hearchurch-oss-[\d.]+)\.tar\.gz"/g)].map((m) => m[1]);
  assert.deepEqual(order, ["hearchurch-oss-0.10.0", "hearchurch-oss-0.9.1", "hearchurch-oss-0.3.0"]);
  assert.ok(!body.includes("notes.txt"));
});

test("/source answers HEAD and a trailing slash", async () => {
  assert.equal((await get("/source", "HEAD")).status, 200);
  assert.equal((await get("/source/")).status, 200);
});

test("/source/<file> serves the archive; unknown names are 404", async () => {
  const res = await get("/source/hearchurch-oss-0.3.0.tar.gz");
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("content-type"), "application/gzip");
  assert.equal((await get("/source/hearchurch-oss-9.9.9.tar.gz")).status, 404);
  assert.equal((await get("/source/notes.txt")).status, 404);
});

test("the Worker's own pages and the site pages still route", async () => {
  for (const path of ["/terms", "/privacy", "/credits", "/sitemap.xml"]) assert.equal((await get(path)).status, 200, path);
  for (const path of ["/", "/pricing", "/download"]) assert.equal(await (await get(path)).text(), `site ${path}`, path);
});

test("/download/windows/<channel>/ serves the Velopack feed, packages and installer", async () => {
  const feed = await get("/download/windows/dev/releases.dev.json");
  assert.equal(feed.status, 200);
  assert.equal(feed.headers.get("cache-control"), "max-age=300");
  const pkg = await get("/download/windows/dev/HearChurchApp-0.1.0-dev.13-full.nupkg");
  assert.equal(pkg.headers.get("cache-control"), "public, max-age=31536000, immutable");
  const setup = await get("/download/windows/dev/HearChurchApp-dev-Setup.exe");
  assert.equal(setup.status, 200);
  assert.equal(setup.headers.get("cache-control"), "no-store");
  assert.match(setup.headers.get("content-disposition"), /attachment/);
  assert.equal((await get("/download/windows/beta/releases.dev.json")).status, 404);
  assert.equal((await get("/download/windows/dev/notes.txt")).status, 404);
  assert.equal((await get("/download/windows/public/HearChurchApp-public-Setup.exe")).status, 404);
});
