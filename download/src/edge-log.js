// Copied from local-website-engine ops/edge-log/classify.mjs (2026-11-10). Keep it identical:
// the same crawler must be counted the same way on every site in the hub.
//
// Who asked for a page, and the one Analytics Engine point that records it.
//
// The rows land in rr_pageviews, the dataset the foundation markets' Worker has
// written since 2026-10 and the hub's AI search panel reads
// (apps/hub/src/lib/pageviews.ts). Every site writes the same shape, so the
// panel needs no per-site code:
//   index1 = hub site slug
//   blob1 path · blob2 referring host · blob3 who (human | search | bot | ai:<UA token>)
//   blob4 utm_source · blob5 country
//
// The three patterns are copied from sites/foundation/worker/index.mjs and must
// stay identical to it, or the same crawler is counted differently on two
// sites. test.mjs checks that they match.

// User agents that fetch for an AI product. The *-User ones fetch live for a
// person's question; the others crawl for training or search indexes.
export const AI_AGENTS = /GPTBot|ChatGPT-User|OAI-SearchBot|ClaudeBot|Claude-User|Claude-SearchBot|anthropic-ai|PerplexityBot|Perplexity-User|Google-Extended|GoogleOther|Applebot-Extended|Bytespider|CCBot|Amazonbot|meta-externalagent|DuckAssistBot|cohere-ai|MistralAI-User/i;
export const SEARCH_BOTS = /Googlebot|bingbot|Applebot|DuckDuckBot|YandexBot|Baiduspider/i;
export const OTHER_BOTS = /bot|crawl|spider|slurp|curl|wget|python|node-fetch|go-http|headless|lighthouse|httpclient|java\//i;

/** The hub's own probes. Its uptime check ran every 5 minutes as "rr-hub-health/1.0", matched none of the bot patterns, and was ~9,000 "human" views a month on every market. */
const OUR_PROBES = /^rr-/i;

/** The utm_source a verify request carries, so the hub can leave it out of every count. */
export const VERIFY_SOURCE = 'rr-verify';

export function agentClass(ua) {
  const m = ua.match(AI_AGENTS);
  if (m) return `ai:${m[0]}`;
  if (SEARCH_BOTS.test(ua)) return 'search';
  if (!ua || OTHER_BOTS.test(ua) || OUR_PROBES.test(ua)) return 'bot';
  return 'human';
}

/** The referring host, minus www, or '' when there is none or it is the site itself. */
export function refHost(referer, host) {
  try {
    const ref = new URL(referer ?? '').hostname.replace(/^www\./, '');
    return ref && ref !== host.replace(/^www\./, '') ? ref : '';
  } catch { return ''; }
}

/** One HTML page served: a GET answered 200 with text/html. Assets, redirects and 404s are not views. */
export function isPageView(method, status, contentType) {
  return method === 'GET' && status === 200 && (contentType ?? '').startsWith('text/html');
}

const clip = (s, n) => String(s ?? '').slice(0, n);

/** The Analytics Engine point for one view. */
export function point({ slug, path, referer, host, ua, utmSource, country }) {
  return {
    indexes: [clip(slug, 96)],
    blobs: [clip(path, 200), refHost(referer, host ?? ''), agentClass(ua ?? ''), clip(utmSource, 100), clip(country, 8)],
    doubles: [1],
  };
}
