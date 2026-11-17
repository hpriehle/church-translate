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

// The stable Windows link, against its own bucket, with the D1 counter recorded.
function windowsEnv(files) {
  const counted = [], pending = [];
  const store = Object.fromEntries(Object.entries(files).map(([k, v]) => [k, typeof v === "string" ? new TextEncoder().encode(v) : v]));
  const obj = (key) => ({ key, size: store[key].length, httpEtag: `"${key}"` });
  const e = {
    FILES: {
      async head(key) { return store[key] ? obj(key) : null; },
      async get(key) {
        if (!store[key]) return null;
        const body = store[key];
        return { ...obj(key), body, async json() { return JSON.parse(new TextDecoder().decode(body)); } };
      },
    },
    STATS: { prepare() { return { bind(day, version) { return { async run() { counted.push(version); } }; } }; } },
    SITE: env.SITE,
  };
  const c = { waitUntil(p) { pending.push(p); } };
  const fetchIt = async (path, init = {}) => {
    const res = await worker.fetch(new Request(`https://hearchurch.com${path}`, init), e, c);
    await Promise.all(pending);
    return res;
  };
  return { fetchIt, counted };
}

const SETUP = "windows/public/HearChurchApp-public-Setup.exe";

test("/download/HearChurch-Setup.exe serves the public installer and counts it with latest.json's version", async () => {
  const { fetchIt, counted } = windowsEnv({ [SETUP]: ARCHIVE, "windows/public/latest.json": '{"version":"0.4.0"}' });
  const res = await fetchIt("/download/HearChurch-Setup.exe");
  assert.equal(res.status, 200);
  assert.equal(res.headers.get("content-type"), "application/octet-stream");
  assert.equal(res.headers.get("cache-control"), "no-store");
  assert.equal(res.headers.get("content-disposition"), 'attachment; filename="HearChurch-Setup.exe"');
  assert.deepEqual(counted, ["0.4.0-windows"]);
  // HEAD and a resumed download are not new downloads; a download from byte 0 is.
  assert.equal((await fetchIt("/download/HearChurch-Setup.exe", { method: "HEAD" })).status, 200);
  await fetchIt("/download/HearChurch-Setup.exe", { headers: { range: "bytes=2-" } });
  await fetchIt("/download/HearChurch-Setup.exe", { headers: { range: "bytes=0-" } });
  assert.deepEqual(counted, ["0.4.0-windows", "0.4.0-windows"]);
});

test("/download/HearChurch-Setup.exe takes the version from the Velopack feed without latest.json", async () => {
  const feed = JSON.stringify({ Assets: [
    { PackageId: "HearChurchApp", Version: "0.4.0", Type: "Full", FileName: "HearChurchApp-0.4.0-full.nupkg" },
    { PackageId: "HearChurchApp", Version: "0.10.0", Type: "Delta", FileName: "HearChurchApp-0.10.0-delta.nupkg" },
    { PackageId: "HearChurchApp", Version: "0.9.1", Type: "Full", FileName: "HearChurchApp-0.9.1-full.nupkg" },
    { PackageId: "HearChurchApp", Version: "0.9.1-rc.2", Type: "Full", FileName: "x.nupkg" },
  ] });
  const { fetchIt, counted } = windowsEnv({ [SETUP]: ARCHIVE, "windows/public/releases.public.json": feed });
  await fetchIt("/download/HearChurch-Setup.exe");
  assert.deepEqual(counted, ["0.9.1-windows"]);
});

test("/download/HearChurch-Setup.exe is a 404 before a public release, and counts nothing", async () => {
  const { fetchIt, counted } = windowsEnv({});
  assert.equal((await fetchIt("/download/HearChurch-Setup.exe")).status, 404);
  assert.deepEqual(counted, []);
  // Without any version file the download still works; it is counted as unknown.
  const other = windowsEnv({ [SETUP]: ARCHIVE });
  assert.equal((await other.fetchIt("/download/HearChurch-Setup.exe")).status, 200);
  assert.deepEqual(other.counted, ["unknown-windows"]);
});
