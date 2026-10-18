// Speak page test: Chromium's fake microphone plays a WAV into /speak while a second page
// listens on /. Prints what was heard/translated and whether the listener got captions + audio.
//   node scripts/speak_check.mjs <base-url> <key> <file.wav> [seconds]
// With LOGIN=1 the speak page is opened without the key and signed in through /login instead.
import { createRequire } from "module";
import path from "path";
const require = createRequire(path.join(process.env.PLAYWRIGHT_DIR || process.cwd(), "noop.js"));
const { chromium } = require("playwright");
const [base, key, wav, secs = "60"] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const browser = await chromium.launch({ args: [
  "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
  "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", `--use-file-for-fake-audio-capture=${wav}`,
  "--autoplay-policy=no-user-gesture-required"] });
const ctx = await browser.newContext({ permissions: ["microphone"] });
const listener = await ctx.newPage();
await listener.goto(base + "/");
await listener.click("#mute");
const speak = await ctx.newPage();
speak.on("console", (m) => { if (m.type() === "error") console.log("[speak error]", m.text()); });
if (process.env.LOGIN) {
  await speak.goto(`${base}/speak`);
  if (!speak.url().includes("/login")) throw new Error("speak page did not ask for the password: " + speak.url());
  await speak.fill("input[name=password]", key);
  await Promise.all([speak.waitForURL(/\/speak/), speak.click("button[type=submit]")]);
} else {
  await speak.goto(`${base}/speak?key=${encodeURIComponent(key)}`);
}
await sleep(1500);
await speak.click("#btn");
await sleep(+secs * 1000);
await speak.click("#btn");
await sleep(15000);
const feed = await speak.$$eval("#feed .card", (cs) => cs.reverse().map((c) => ({
  en: c.querySelector(".en").textContent, fr: c.querySelector(".fr").textContent, meta: c.querySelector(".meta").textContent })));
const lis = await listener.evaluate(() => ({ captions: document.querySelectorAll("#captions p").length,
  audioTime: window.__listener.audio.currentTime, state: document.getElementById("audioState").textContent }));
await speak.screenshot({ path: "logs/speak.png", fullPage: false });
await listener.screenshot({ path: "logs/listener-speak.png" });
console.log(JSON.stringify({ err: await speak.textContent("#err"), lag: await speak.textContent("#lag"), feed, listener: lis }, null, 2));
await browser.close();
