// POST /api/collect — the visitor tracker's batches (ops/tracker/), forwarded to
// the hub with HUB_INGEST_TOKEN. The browser never holds a token or talks to the
// hub. We pass the visitor's IP so the hub can turn it into a daily one-way hash;
// the hub stores no address (see /privacy/ and ops/tracker/README.md).
// Answers 204 at once: a beacon sent as the page closes must not wait on the hub.
const DEFAULT_COLLECT = 'https://hub.clntacq.com/api/collect';
const MAX_BODY = 256 * 1024; // a replay chunk is the largest batch

const done = () => new Response(null, { status: 204, headers: { 'Cache-Control': 'no-store' } });

export async function onRequestPost({ request, env, waitUntil }) {
  // Do Not Track and Global Privacy Control: dropped here as well as in the
  // tracker, so a stale script on a cached page can't override the setting.
  if (request.headers.get('DNT') === '1' || request.headers.get('Sec-GPC') === '1') return done();
  if (!env.HUB_INGEST_TOKEN) return done();

  const body = await request.text();
  if (!body || body.length > MAX_BODY) return done();

  const cf = request.cf ?? {};
  const forward = fetch(env.COLLECT_URL || DEFAULT_COLLECT, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${env.HUB_INGEST_TOKEN}`,
      'X-RR-IP': request.headers.get('CF-Connecting-IP') ?? '',
      'X-RR-UA': request.headers.get('User-Agent') ?? '',
      'X-RR-Country': cf.country ?? '',
      'X-RR-Region': cf.region ?? '',
      'X-RR-City': cf.city ?? '',
    },
    body,
  }).catch((err) => console.error('[collect] forward failed', err.message));
  // In the Worker, waitUntil keeps the forward alive after the 204 goes out.
  if (waitUntil) waitUntil(forward);
  return done();
}

export function onRequest() {
  return done();
}
