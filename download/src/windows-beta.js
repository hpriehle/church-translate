// POST /api/windows-beta: the "Request the Windows app (beta)" form on /download, forwarded to the Siteboard hub's
// inbox (site "hearchurch", Sales) the way local-website-engine's sites post /api/lead: JSON to /api/ingest/lead
// with Authorization: Bearer HUB_INGEST_TOKEN (the secret /api/collect already uses). Honeypot hits are dropped
// silently. Without the token, or if the hub refuses, the visitor is told to email us: a request is never
// reported as sent when it wasn't.
// The phone is optional: the hub keys phoneless form leads by email (hub 5abd753, LWE PR #134).
const LIMITS = { name: 100, church: 120, phone: 20, email: 200, city: 60, languages: 200, windows: 20, message: 2000 };
const DEFAULT_INGEST = "https://hub.clntacq.com/api/ingest/lead";

const clean = (v, max) => String(v ?? "").replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, max);
const text = (body, status) => new Response(body, { status, headers: { "content-type": "text/plain; charset=utf-8" } });
const thanks = (request) => Response.redirect(new URL("/download?requested#windows-beta", request.url).href, 303);
const FAIL = "Sorry, your request could not be sent. Please email support@hearchurch.com and we'll send you the Windows app.";

export async function windowsBeta(request, env) {
  let form;
  try {
    form = await request.formData();
  } catch {
    return text("Bad request", 400);
  }
  // Real visitors never see this field. Pretend success so bots learn nothing.
  if (clean(form.get("website"), 200)) return thanks(request);

  const f = {};
  for (const [k, max] of Object.entries(LIMITS)) f[k] = clean(form.get(k), max);
  f.message = String(form.get("message") ?? "").replace(/\r\n?/g, "\n").trim().slice(0, LIMITS.message);
  const digits = f.phone.replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
  if (!f.name || !f.church || (digits && digits.length !== 10) || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(f.email)) {
    return text("Please go back and enter your name, your church and your email (and a 10-digit phone number, if you add one).", 422);
  }

  const body = JSON.stringify({
    site: "hearchurch",
    form: "windows-beta",
    external_id: crypto.randomUUID(),
    name: f.name,
    email: f.email,
    ...(digits ? { phone: `+1${digits}` } : {}),
    city: f.city,
    service: "Windows app (beta)",
    message: [`Church: ${f.church}`, `Languages: ${f.languages || "-"}`, `Windows: ${f.windows || "not sure"}`, f.message]
      .filter(Boolean).join("\n"),
    church: f.church,
    languages: f.languages,
    windows: f.windows,
    page: "/download",
    source: new URL(request.url).hostname,
    submitted_at: new Date().toISOString(),
  });

  if (!env.HUB_INGEST_TOKEN) {
    console.error("[windows-beta] HUB_INGEST_TOKEN is not set; request NOT forwarded to the hub", body);
    return text(FAIL, 503);
  }
  try {
    const res = await fetch(env.INGEST_URL || DEFAULT_INGEST, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${env.HUB_INGEST_TOKEN}` },
      body,
    });
    if (!res.ok) throw new Error(`hub responded ${res.status}: ${(await res.text()).slice(0, 200)}`);
  } catch (err) {
    console.error("[windows-beta] forward failed", err.message, body);
    return text(FAIL, 502);
  }
  return thanks(request);
}
