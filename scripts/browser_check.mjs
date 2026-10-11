// Drives the listener page in a real browser engine: tap "Écouter", check captions + audio,
// cut the network for 10 s, check everything recovers. Saves screenshots to logs/.
//   node scripts/browser_check.mjs http://127.0.0.1:8000
// Needs Playwright (npm i playwright). Set PLAYWRIGHT_DIR to a folder that has it in node_modules.
import { createRequire } from "module";
import path from "path";
const require = createRequire(path.join(process.env.PLAYWRIGHT_DIR || process.cwd(), "noop.js"));
const { chromium, devices } = require("playwright");

const base = (process.argv[2] || "http://127.0.0.1:8000").replace(/\/$/, "");
const out = {};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const browser = await chromium.launch();
const ctx = await browser.newContext({ ...devices["iPhone 13"], defaultBrowserType: undefined });
const page = await ctx.newPage();
page.on("console", (m) => { if (m.type() === "error") console.log("[page error]", m.text()); });

const audioState = () => page.evaluate(() => {
  const a = window.__listener.audio;
  return { t: a.currentTime, paused: a.paused, src: a.src, err: a.error && a.error.code, listening: window.__listener.listening,
           lastSeq: window.__listener.lastSeq, state: document.getElementById("audioState").textContent,
           conn: document.getElementById("conn").textContent };
});
const seqs = () => page.$$eval("#captions p[data-seq]", (ps) => ps.map((p) => +p.dataset.seq));
const progressing = async () => { const a = await audioState(); await sleep(3000); const b = await audioState(); return { ok: b.t > a.t + 2, a, b }; };

await page.goto(base + "/");
await page.screenshot({ path: "logs/listener-before.png" });
const tClick = Date.now();
await page.click("#listen");
await page.waitForFunction(() => window.__listener.audio.currentTime > 0, null, { timeout: 30000 });
out.seconds_to_first_audio = (Date.now() - tClick) / 1000;
await page.waitForFunction(() => document.querySelectorAll("#captions p").length >= 3, null, { timeout: 90000 });
out.playing = await progressing();
out.seqs_before = await seqs();

// Simulated Wi-Fi drop: with --proxy-pid, the flaky proxy cuts every connection for 10 s.
const proxyPid = +(process.argv.find((x) => x.startsWith("--proxy-pid=")) || "=0").split("=")[1];
if (proxyPid) process.kill(proxyPid, "SIGUSR1"); else await ctx.setOffline(true);
await sleep(4000);
out.during_outage = await audioState();
await sleep(6000);
if (!proxyPid) await ctx.setOffline(false);
await sleep(20000);
out.after = await progressing();
const after = await seqs();
const missing = [];
for (let s = after[0]; s <= after[after.length - 1]; s++) if (!after.includes(s)) missing.push(s);
out.seqs_after_count = after.length;
out.caption_gaps_after_reconnect = missing;
await page.screenshot({ path: "logs/listener-live.png" });

const op = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
await op.goto(base + "/operator");
await sleep(4000);
await op.screenshot({ path: "logs/operator.png" });
const pr = await browser.newPage({ viewport: { width: 900, height: 1200 } });
await pr.goto(base + "/print");
await pr.fill("#ssid", "Eglise-Invites");
await sleep(800);
await pr.screenshot({ path: "logs/print.png" });

console.log(JSON.stringify(out, null, 2));
await browser.close();
