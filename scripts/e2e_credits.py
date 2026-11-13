"""End-to-end test of the paid credits chain against a real local hub, Stripe in TEST mode only.

    uv run python scripts/e2e_credits.py            [--env ~/.config/hearchurch/stripe-test.env] [--usd 10]
                                                    [--real-anthropic [--hub-env ~/.config/hearchurch/hub.env]]

1. starts the hub (this repo's FastAPI app, hub routes only, no pipeline) on a free port with a throwaway database
   in a temp dir, STRIPE_* loaded from the env file (never printed)
2. signs up a test congregation (billing mode credits)
3. POST /api/hc/checkout → a real Stripe TEST Checkout session (only its id and amount are printed); skipped with a
   fake session id when STRIPE_SECRET_KEY isn't in the env file. A live key (sk_live_/rk_live_) is refused.
4. builds checkout.session.completed (paid) for that session, signs it like Stripe (t=…,v1=HMAC-SHA256), posts it
   to /api/stripe/webhook twice (credited once) and once with a bad signature (400)
5. GET /api/hc/billing: the balance went up by exactly the top-up
6. a meeting over the engine's link (/ws/engine): session_start, then the engine's `meter` report of 90
   language-minutes (sent twice): the free hour goes first, the other 30 min cost $2.50 at $5/language-hour
7. charge.refunded for part of the top-up (posted twice): the balance drops by the refunded part
8. a "this Wi-Fi only" meeting: session_start with private=true starts a billing session that the congregation's
   public page, search and listener socket don't show; its meter report (12 language-minutes) costs $1.00
9. only with --real-anthropic: the credits translation proxy with the real Anthropic key. ANTHROPIC_API_KEY is read
   from the hub env file and given to the hub child process only (never printed; this process, which plays the
   engine, has no key at all). The engine's own `hub` backend (app/translate/hub_session.py: HubSession and
   HubClient, the app's system prompt, glossary, model and effort from config.toml) translates one English sentence
   to French and one to Spanish in a live meeting, and one to French in a "this Wi-Fi only" (private) meeting. Then
   the hub log (production log levels, plus the proxy's per-request debug line) and every file of the throwaway
   database must contain neither the English nor the translations, and a second congregation that used up its free
   hour gets 402 out_of_credit (its meeting's end is moved back past the proxy's trailing window). Costs a few
   cents (3 short sentences).
10. PASS/FAIL per step, exit 1 on any failure; the hub is stopped and the temp dir deleted.

Without --real-anthropic: no Anthropic call (the hub gets no key; tests/test_wifi_only.py covers the proxy with a
fake), no live Stripe keys, nothing deployed. Stripe is called only in step 3 (create a test Checkout session, then
read it back).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = Path("~/.config/hearchurch/stripe-test.env").expanduser()
DEFAULT_HUB_ENV = Path("~/.config/hearchurch/hub.env").expanduser()
CLAUDE_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
PROD_LOG = "E2E_HUB_PROD_LOG"   # set for the hub child: log like the production hub (and the proxy's debug line)
STRIPE_VARS = ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PAYMENT_METHOD_CONFIG")
USD_PER_LANGUAGE_HOUR = 5.0
FREE_MIN = 60


# ------------------------------------------------------------------ the hub (child process)

def serve(port: int, home: Path) -> None:
    """Run the hub in this process: the real app and routes with lifespan off (no Whisper, voices or tunnel),
    the Hear registry opened on a database in `home`, like the tests' `server` fixture."""
    sys.path.insert(0, str(ROOT))
    if os.environ.get(PROD_LOG):
        import logging
        # As app/__main__.py sets it up, plus the proxy's per-request debug line, so step 9 can check what a
        # translation leaves in the log.
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
                            stream=sys.stdout)
        for noisy in ("httpx", "httpcore", "uvicorn.access"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        logging.getLogger("hear").setLevel(logging.DEBUG)
    import uvicorn

    from app import main
    from app.broadcast.hub import Hub
    from app.config import Settings
    from app.hear import api as hear_api
    from app.hear.api import Hear
    from app.hear.db import Database
    from app.hear.live import Live
    from app.hear.mail import Mailer

    class NoGeocoder:  # never the real Nominatim
        async def geocode(self, address):
            return None

        async def town(self, lat, lng):
            return ""

    main.S.settings = Settings.model_validate({"server": {"operator_pin": secrets.token_hex(8),
                                                          "public_url": f"http://127.0.0.1:{port}/"}})
    main.S.local_addrs = set()
    main.S.operator = Hub("operator")
    db = Database(home / "hearchurch.db")
    hear_api.H = Hear(db, Mailer(home / "outbox.log"), Live(db, main.S.settings.stream, grace_s=5),
                      geocoder=NoGeocoder(), verify_email=False)
    uvicorn.run(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")


# ------------------------------------------------------------------ helpers

def load_env(path: Path, names: tuple[str, ...] = STRIPE_VARS) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.removeprefix("export ").split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in names and v:
            out[k] = v
    return out


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sign(payload: bytes, secret: str, t: int | None = None) -> str:
    """The Stripe-Signature header Stripe sends: t=<unix>,v1=hex(HMAC-SHA256(secret, "<t>.<payload>"))."""
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def event(kind: str, obj: dict) -> bytes:
    return json.dumps({"id": f"evt_e2e_{secrets.token_hex(8)}", "object": "event", "type": kind,
                       "api_version": "2025-01-01", "created": int(time.time()), "livemode": False,
                       "data": {"object": obj}}).encode()


class Report:
    def __init__(self):
        self.rows: list[tuple[str, bool, str]] = []

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
        return ok

    def skip(self, name: str, why: str) -> None:
        self.rows.append((name, True, "skipped"))
        print(f"SKIP  {name}  ({why})", flush=True)

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)


# ------------------------------------------------------------------ the test

async def run(base: str, env: dict[str, str], usd: int, r: Report, real: bool = False, tmp: Path | None = None) -> None:
    key, whsec = env.get("STRIPE_SECRET_KEY", ""), env.get("STRIPE_WEBHOOK_SECRET", "")
    ws_base = base.replace("http://", "ws://")
    async with httpx.AsyncClient(base_url=base, timeout=30) as c:
        # 2 ------------------------------------------------------------ a test congregation on credits
        tag = secrets.token_hex(3)
        res = await c.post("/api/hc/signup", json={
            "church": "E2E Test Church", "name": f"E2E Credits {tag}", "unit_type": "ward",
            "email": f"e2e-{tag}@example.invalid", "meeting_times": [{"day": "sun", "time": "10:00"}],
            "device_name": "E2E Mac"})
        if not r.step("2 sign up a test congregation", res.status_code == 200 and "device_token" in res.json(),
                      f"HTTP {res.status_code}"):
            return
        signup = res.json()
        auth = {"Authorization": f"Bearer {signup['device_token']}"}

        async def billing() -> dict:
            b = await c.get("/api/hc/billing", headers=auth)
            b.raise_for_status()
            return b.json()

        b0 = await billing()
        r.step("2 billing mode is credits, nothing bought, the free hour intact",
               b0["billing"] == "credits" and b0["balance_usd"] == 0 and b0["balance"]["free_left_min"] == FREE_MIN
               and b0["can_buy"] == bool(key),
               f"billing={b0['billing']} balance=${b0['balance_usd']:.2f} free={b0['balance']['free_left_min']} min "
               f"can_buy={b0['can_buy']}")
        cid = None  # the congregation id the webhook needs, from the Checkout metadata or the admin-free fallback

        # 3 ------------------------------------------------------------ a real Stripe TEST Checkout session
        cents = usd * 100
        session: dict
        if key:
            res = await c.post("/api/hc/checkout", json={"usd": usd}, headers=auth)
            ok = res.status_code == 200 and res.json().get("id", "").startswith("cs_test_")
            if not r.step("3 hub creates a Stripe TEST Checkout session", ok, f"HTTP {res.status_code}"):
                return
            sid = res.json()["id"]
            import stripe
            s = await asyncio.to_thread(stripe.StripeClient(key).v1.checkout.sessions.retrieve, sid)
            s = s.to_dict()
            print(f"      session {sid}: amount_total {s.get('amount_total')} {s.get('currency')}, "
                  f"livemode={s.get('livemode')}", flush=True)
            r.step("3 the session is test mode, for the right amount and congregation",
                   s.get("livemode") is False and s.get("amount_total") == cents and s.get("currency") == "usd"
                   and "card" in (s.get("payment_method_types") or []) and s.get("mode") == "payment"
                   and (s.get("metadata") or {}).get("slug") == signup["slug"],
                   f"amount_total={s.get('amount_total')} metadata.slug={(s.get('metadata') or {}).get('slug')}")
            session = s
            cid = (s.get("metadata") or {}).get("congregation_id")
        else:
            res = await c.post("/api/hc/checkout", json={"usd": usd}, headers=auth)
            r.skip("3 Stripe TEST Checkout session", "no STRIPE_SECRET_KEY in the env file: using a fake session id")
            r.step("3 without a key the buy route answers 503 not_configured",
                   res.status_code == 503 and res.json().get("error") == "not_configured", f"HTTP {res.status_code}")
            sid = f"cs_test_e2e_nokey_{secrets.token_hex(8)}"
            session = {"id": sid, "object": "checkout.session", "mode": "payment", "currency": "usd",
                       "amount_total": cents, "client_reference_id": None,
                       "metadata": {"slug": signup["slug"], "cents": str(cents)}}
            print(f"      session {sid}: amount_total {cents} usd (fake)", flush=True)

        if cid is None:
            # No Checkout metadata to read it from: the signup answer doesn't carry the id, but new congregations
            # are numbered from 1 in this empty database.
            cid = "1"

        # 4 ------------------------------------------------------------ simulate the payment (signed webhook)
        if not whsec:
            r.step("4 STRIPE_WEBHOOK_SECRET present", False, "missing in the env file: can't sign the webhook")
            return
        # Checkout makes the payment intent when the card is paid; this one stands in for it (the refund uses it too)
        pi = f"pi_e2e_{secrets.token_hex(8)}"
        paid = {**session, "status": "complete", "payment_status": "paid", "payment_intent": pi, "mode": "payment",
                "currency": "usd", "amount_total": cents,
                "client_reference_id": session.get("client_reference_id") or cid,
                "metadata": {**(session.get("metadata") or {}), "congregation_id": str(cid)}}
        body = event("checkout.session.completed", paid)
        hook = "/api/stripe/webhook"
        bad = await c.post(hook, content=body, headers={"Stripe-Signature": sign(body, whsec + "x"),
                                                        "Content-Type": "application/json"})
        r.step("4 a wrongly signed event is refused", bad.status_code == 400, f"HTTP {bad.status_code}")
        first = await c.post(hook, content=body, headers={"Stripe-Signature": sign(body, whsec),
                                                          "Content-Type": "application/json"})
        r.step("4 signed checkout.session.completed credits", first.status_code == 200 and first.json().get("credited")
               is True, f"HTTP {first.status_code} {first.json() if first.status_code == 200 else ''}")
        again = await c.post(hook, content=body, headers={"Stripe-Signature": sign(body, whsec),
                                                          "Content-Type": "application/json"})
        r.step("4 the same event again is acknowledged, not credited", again.status_code == 200
               and again.json().get("credited") is False, f"HTTP {again.status_code} {again.json() if again.status_code == 200 else ''}")

        # 5 ------------------------------------------------------------ the balance went up once
        b1 = await billing()
        r.step(f"5 balance up by exactly ${usd:.2f}, once", round(b1["balance_usd"] - b0["balance_usd"], 2) == usd
               and b1["balance"]["bought_usd"] == usd,
               f"${b0['balance_usd']:.2f} → ${b1['balance_usd']:.2f}, bought ${b1['balance']['bought_usd']:.2f}, "
               f"{b1['language_hours_left']} language-hours left")

        # 6 ------------------------------------------------------------ metered speech over the engine's link
        ws = await websockets.connect(f"{ws_base}/ws/engine", additional_headers=auth)

        async def recv(kind: str) -> dict:
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if m.get("type") == kind or m.get("type") == "session_refused":
                    return m

        sys.path.insert(0, str(ROOT))
        from app.config import Settings
        st = Settings().stream   # the hub's own format (it refuses an engine that streams another)
        try:
            await ws.send(json.dumps({"type": "hello", "name": "E2E Mac", "languages": ["fr", "es"],
                                      "stream": {"sample_rate": st.sample_rate, "bitrate_kbps": st.bitrate_kbps}}))
            await ws.send(json.dumps({"type": "session_start", "languages": ["fr", "es"]}))
            started = await recv("session_started")
            if not r.step("6 a meeting starts on the hub", started.get("type") == "session_started",
                          started.get("message", "")):
                return
            code = started["session"]
            # 45 one-minute chunks heard in fr and es = 90 language-minutes: 60 free, 30 billed = $2.50
            items = [{"seq": i, "s": 60.0, "langs": ["fr", "es"]} for i in range(1, 46)]
            await ws.send(json.dumps({"type": "meter", "session": code, "items": items}))
            ok1 = await recv("meter_ok")
            await ws.send(json.dumps({"type": "meter", "session": code, "items": items}))   # resent after a drop
            ok2 = await recv("meter_ok")
            r.step("6 the hub acknowledges the meter report (and the resend)",
                   sorted(ok1.get("seqs", [])) == list(range(1, 46)) and ok2.get("unknown") is False,
                   f"{len(ok1.get('seqs', []))} chunks acknowledged")
            await ws.send(json.dumps({"type": "session_stop"}))
            await asyncio.sleep(0.3)
        finally:
            await ws.close()
        b2 = await billing()
        billed_min = 90 - FREE_MIN
        cost = round(billed_min / 60 * USD_PER_LANGUAGE_HOUR, 2)
        r.step(f"6 90 language-minutes: free hour used, then ${cost:.2f} at ${USD_PER_LANGUAGE_HOUR:.0f}/language-hour",
               b2["balance"]["used_min"] == 90 and b2["balance"]["free_left_min"] == 0
               and round(b1["balance_usd"] - b2["balance_usd"], 2) == cost,
               f"used {b2['balance']['used_min']} min, free left {b2['balance']['free_left_min']}, "
               f"${b1['balance_usd']:.2f} → ${b2['balance_usd']:.2f}")

        # 7 ------------------------------------------------------------ partial refund
        refund_cents = cents * 2 // 5          # 40% of the top-up ($4 of $10)
        charge = {"id": f"ch_e2e_{secrets.token_hex(8)}", "object": "charge", "payment_intent": pi,
                  "amount": cents, "amount_captured": cents, "amount_refunded": refund_cents, "currency": "usd",
                  "refunded": False, "livemode": False}
        body = event("charge.refunded", charge)
        res = await c.post(hook, content=body, headers={"Stripe-Signature": sign(body, whsec),
                                                        "Content-Type": "application/json"})
        res2 = await c.post(hook, content=body, headers={"Stripe-Signature": sign(body, whsec),
                                                         "Content-Type": "application/json"})
        r.step("7 signed charge.refunded taken off once", res.status_code == 200 and res.json().get("refunded") is True
               and res2.status_code == 200 and res2.json().get("refunded") is False,
               f"HTTP {res.status_code}/{res2.status_code} refunded_cents={res.json().get('refunded_cents')}")
        b3 = await billing()
        r.step(f"7 balance down by the refunded ${refund_cents / 100:.2f}",
               round(b2["balance_usd"] - b3["balance_usd"], 2) == refund_cents / 100
               and b3["balance"]["bought_usd"] == (cents - refund_cents) / 100,
               f"${b2['balance_usd']:.2f} → ${b3['balance_usd']:.2f}, bought ${b3['balance']['bought_usd']:.2f}; "
               f"{b3['message']}")

        # 8 ------------------------------------------------------------ a "this Wi-Fi only" meeting on credits
        ws = await websockets.connect(f"{ws_base}/ws/engine", additional_headers=auth)
        try:
            await ws.send(json.dumps({"type": "hello", "name": "E2E Mac", "languages": ["fr", "es"],
                                      "stream": {"sample_rate": st.sample_rate, "bitrate_kbps": st.bitrate_kbps}}))
            await ws.send(json.dumps({"type": "session_start", "languages": ["fr", "es"], "private": True}))
            started = await recv_on(ws, "session_started")
            if not r.step("8 a Wi-Fi-only meeting starts a private billing session",
                          started.get("type") == "session_started", started.get("message", "")):
                return
            info = (await c.get(f"/api/hc/c/{signup['slug']}")).json()
            found = (await c.get("/api/hc/search", params={"q": f"E2E Credits {tag}"})).json()["results"]
            async with websockets.connect(f"{ws_base}/c/{signup['slug']}/ws/listen") as phone:
                await phone.send(json.dumps({"type": "hello", "lang": "fr"}))
                init = await recv_on(phone, "init")
            r.step("8 nothing public: the page, the search and a phone all see no meeting",
                   info.get("live") is False and all(not x["live"] for x in found) and init.get("live") is False,
                   f"page live={info.get('live')}, search live={[x['live'] for x in found]}, phone live={init.get('live')}")
            items = [{"seq": i, "s": 60.0, "langs": ["fr"]} for i in range(1, 13)]   # fr heard on the Mac's Wi-Fi
            await ws.send(json.dumps({"type": "meter", "session": started["session"], "items": items}))
            ok = await recv_on(ws, "meter_ok")
            await ws.send(json.dumps({"type": "session_stop"}))
            await asyncio.sleep(0.3)
        finally:
            await ws.close()
        b4 = await billing()
        r.step("8 its 12 language-minutes are billed like an online meeting's ($1.00)",
               len(ok.get("seqs", [])) == 12 and b4["balance"]["used_min"] == 102
               and round(b3["balance_usd"] - b4["balance_usd"], 2) == 1.0,
               f"used {b4['balance']['used_min']} min, ${b3['balance_usd']:.2f} → ${b4['balance_usd']:.2f}")

        if real:
            await real_translations(c, base, ws_base, auth, signup, st, tmp, r)


# ------------------------------------------------------------------ step 9: real translations through the hub

# Short, so the whole step costs a few cents. Each is translated by the engine's own hub backend.
LIVE_SENTENCES = [("fr", "Brothers and sisters, we are grateful that you could join us this morning."),
                  ("es", "Please remember that the youth activity on Wednesday starts at seven.")]
WIFI_SENTENCE = ("fr", "The closing prayer will be offered by Sister Johnson.")
BROKE_SENTENCE = "Thank you for coming, and have a good week."
GLOSSARIES = {"fr": "glossary.json", "es": "glossary.es.json"}   # the code's defaults (the Mac copies them)


class Engine:
    """The Mac's translation side for one congregation: one HubSession per language, built like the router builds
    them (system prompt + scripture rule, model and effort from config.toml, the seed first), talking to the hub
    with the device token through HubClient. This process has no Anthropic key: only the hub can call Claude."""

    def __init__(self, base: str, token: str):
        import tomllib

        from app.config import Settings
        self.cfg = Settings.model_validate(tomllib.loads((ROOT / "config.toml").read_text())).translate
        self.base, self.token = base, token
        self.sessions: dict = {}

    async def translate(self, code: str, english: str) -> str | None:
        from app.config import load_glossary
        from app.translate.hub_session import HubClient, HubSession
        from app.translate.prompts import SCRIPTURE_RULE, build_system_prompt, clean_output, seed_message, user_message
        s = self.sessions.get(code)
        if s is None:
            prompt = build_system_prompt(load_glossary(ROOT / GLOSSARIES[code]), self.cfg.audience, code, "en")
            s = HubSession(prompt + SCRIPTURE_RULE, self.cfg.claude_model, self.cfg.claude_effort,
                           client=HubClient(self.base, self.token))
            await s.start()
            await s.ask(seed_message([]))     # kept by HubSession: it goes out with the first sentence
            self.sessions[code] = s
        return clean_output(await asyncio.wait_for(s.ask(user_message(english, False)), 90))

    @property
    def cost_usd(self) -> float:
        return sum(s.usage.cost_usd for s in self.sessions.values())

    async def close(self) -> None:
        for s in self.sessions.values():
            await s.close()


def hub_error(e: BaseException):
    from app.translate.hub_session import HubError
    while e is not None and not isinstance(e, HubError):
        e = e.__cause__
    return e


async def meeting(ws_base: str, auth: dict, st, langs: list[str], private: bool = False):
    ws = await websockets.connect(f"{ws_base}/ws/engine", additional_headers=auth)
    await ws.send(json.dumps({"type": "hello", "name": "E2E Mac", "languages": langs,
                              "stream": {"sample_rate": st.sample_rate, "bitrate_kbps": st.bitrate_kbps}}))
    await ws.send(json.dumps({"type": "session_start", "languages": langs, **({"private": True} if private else {})}))
    return ws, await recv_on(ws, "session_started")


async def stop(ws) -> None:
    await ws.send(json.dumps({"type": "session_stop"}))
    await asyncio.sleep(0.3)
    await ws.close()


async def real_translations(c: httpx.AsyncClient, base: str, ws_base: str, auth: dict, signup: dict, st, tmp: Path,
                            r: Report) -> None:
    import sqlite3

    seen: list[str] = []                       # every text that must not be stored or logged
    eng = Engine(base, signup["device_token"])
    model_effort = f"{eng.cfg.claude_model}/{eng.cfg.claude_effort}"
    try:
        # live, listed meeting: fr and es
        ws, started = await meeting(ws_base, auth, st, ["fr", "es"])
        try:
            if not r.step("9 a meeting starts for the real translations", started.get("type") == "session_started",
                          started.get("message", "")):
                return
            for code, english in LIVE_SENTENCES:
                seen.append(english)
                try:
                    out = await eng.translate(code, english)
                except Exception as e:
                    he = hub_error(e)
                    r.step(f"9 en→{code} through POST /api/hc/translate", False,
                           f"HTTP {he.status} {he.code}: {he.message}" if he else f"{type(e).__name__}: {str(e)[:200]}")
                    continue
                if out:
                    seen.append(out)
                print(f"      en: {english}\n      {code}: {out}", flush=True)
                r.step(f"9 en→{code} through POST /api/hc/translate ({model_effort})",
                       bool(out) and out.strip() != english, f"{len(out or '')} chars back")
        finally:
            await stop(ws)

        # "this Wi-Fi only": a private billing session
        ws, started = await meeting(ws_base, auth, st, ["fr"], private=True)
        try:
            code, english = WIFI_SENTENCE
            seen.append(english)
            try:
                out = await eng.translate(code, english)
                err = ""
            except Exception as e:
                he = hub_error(e)
                out, err = None, (f"HTTP {he.status} {he.code}" if he else f"{type(e).__name__}: {str(e)[:200]}")
            if out:
                seen.append(out)
            print(f"      en: {english}\n      {code}: {out}", flush=True)
            r.step("9 en→fr in a Wi-Fi-only (private) meeting", started.get("type") == "session_started"
                   and bool(out) and out.strip() != english, err or f"{len(out or '')} chars back")
        finally:
            await stop(ws)
        print(f"      Anthropic cost of these translations: ${eng.cost_usd:.4f} (the app's own price table)", flush=True)
    finally:
        await eng.close()

    # a congregation with nothing left: 402 once its meeting is over
    tag = secrets.token_hex(3)
    res = await c.post("/api/hc/signup", json={
        "church": "E2E Test Church", "name": f"E2E Broke {tag}", "unit_type": "ward",
        "email": f"e2e-broke-{tag}@example.invalid", "meeting_times": [{"day": "sun", "time": "10:00"}],
        "device_name": "E2E Mac 2"})
    if not r.step("9 sign up a second congregation", res.status_code == 200, f"HTTP {res.status_code}"):
        return
    broke = res.json()
    bauth = {"Authorization": f"Bearer {broke['device_token']}"}
    ws, started = await meeting(ws_base, bauth, st, ["fr"])
    try:
        items = [{"seq": i, "s": 60.0, "langs": ["fr"]} for i in range(1, FREE_MIN + 2)]   # the free hour and a minute
        await ws.send(json.dumps({"type": "meter", "session": started.get("session"), "items": items}))
        await recv_on(ws, "meter_ok")
    finally:
        await stop(ws)
    bb = (await c.get("/api/hc/billing", headers=bauth)).json()
    # The proxy still serves a Mac's last sentences for TRAILING_S after its meeting ends; move the end back past it.
    with sqlite3.connect(tmp / "hearchurch.db") as db:
        db.execute("UPDATE sessions SET ended_at = ended_at - 600 WHERE congregation_id ="
                   " (SELECT id FROM congregations WHERE slug = ?)", (broke["slug"],))
    beng = Engine(base, broke["device_token"])
    try:
        out = await beng.translate("fr", BROKE_SENTENCE)
        he, detail = None, f"translated: {out!r}"
    except Exception as e:
        he = hub_error(e)
        detail = f"HTTP {he.status} {he.code}: {he.message}" if he else f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        await beng.close()
    seen.append(BROKE_SENTENCE)
    r.step("9 out of credit (meeting over) → 402 out_of_credit, no Anthropic call",
           he is not None and he.status == 402 and he.code == "out_of_credit" and beng.cost_usd == 0,
           f"balance ${bb.get('balance_usd', 0):.2f}, free left {bb.get('balance', {}).get('free_left_min')} min; {detail}")

    # nothing of the speech or its translation stayed on the hub
    await asyncio.sleep(0.3)
    log = (tmp / "hub.log").read_text(errors="replace")
    files = [f for f in tmp.rglob("*") if f.is_file()]
    found = []
    for f in files:
        data = f.read_bytes()
        for t in seen:
            needles = {t.encode(), t[:24].encode(), json.dumps(t)[1:25].encode()}
            if any(n in data for n in needles):
                found.append(f"{f.name}: {t[:20]}…")
    lines = log.count(f"translate for {signup['slug']}")
    r.step("9 the hub log and database hold neither the English nor the translations",
           not found and lines >= 3,
           f"{len(seen)} texts checked in {len(files)} files ({', '.join(sorted(f.name for f in files))}); "
           f"the log has {lines} proxy lines for this congregation" + (f"; FOUND {found}" if found else ""))


async def recv_on(ws, kind: str) -> dict:
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), 5))
        if m.get("type") == kind or m.get("type") == "session_refused":
            return m


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", type=Path, default=DEFAULT_ENV, help=f"env file with {', '.join(STRIPE_VARS)}")
    ap.add_argument("--usd", type=int, default=10, help="top-up in whole dollars (5–500)")
    ap.add_argument("--real-anthropic", action="store_true",
                    help="also step 9: real translations through the hub with the real Anthropic key (a few cents)")
    ap.add_argument("--hub-env", type=Path, default=DEFAULT_HUB_ENV, help="env file with ANTHROPIC_API_KEY")
    ap.add_argument("--serve", nargs=2, metavar=("PORT", "HOME"), help=argparse.SUPPRESS)  # the hub child
    a = ap.parse_args()
    if a.serve:
        serve(int(a.serve[0]), Path(a.serve[1]))
        return 0

    env = load_env(a.env.expanduser())
    key = env.get("STRIPE_SECRET_KEY", "")
    if key and not key.startswith(("sk_test_", "rk_test_")):
        print("FAIL  STRIPE_SECRET_KEY is not a test key: this script only runs against Stripe test mode.")
        return 1
    print(f"env {a.env}: " + ", ".join(f"{k} {'set' if env.get(k) else 'MISSING'}" for k in STRIPE_VARS), flush=True)
    claude: dict[str, str] = {}
    if a.real_anthropic:
        claude = load_env(a.hub_env.expanduser(), ("ANTHROPIC_API_KEY",))
        if not claude.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-"):
            print(f"FAIL  --real-anthropic: no ANTHROPIC_API_KEY in {a.hub_env}")
            return 1
        print(f"hub env {a.hub_env}: ANTHROPIC_API_KEY set (for the hub process only)", flush=True)
    for k in CLAUDE_VARS:   # this process plays the engine: it never has a Claude key
        os.environ.pop(k, None)

    tmp = Path(tempfile.mkdtemp(prefix="hc-e2e-"))
    port = free_port()
    child_env = {k: v for k, v in os.environ.items() if k not in STRIPE_VARS and k not in
                 ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")}   # no Claude key: nothing can reach Anthropic
    child_env.update(env)
    if claude:
        child_env.update(claude)
        child_env[PROD_LOG] = "1"
    child_env["HEARCHURCH_HOME"] = str(tmp)
    log = open(tmp / "hub.log", "w")
    hub = subprocess.Popen([sys.executable, __file__, "--serve", str(port), str(tmp)], cwd=ROOT, env=child_env,
                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    r = Report()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(200):
            if hub.poll() is not None:
                break
            try:
                if httpx.get(f"{base}/api/hc/billing", timeout=1).status_code == 403:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        up = hub.poll() is None
        if r.step("1 hub started on a free port with a throwaway database", up, f"{base}, data in {tmp}"):
            asyncio.run(run(base, env, a.usd, r, a.real_anthropic, tmp))
    except Exception as e:  # a crash in the test itself counts as a failure, never a traceback with secrets
        r.step("unexpected error", False, f"{type(e).__name__}: {str(e)[:200]}")
    finally:
        if hub.poll() is None:
            os.killpg(hub.pid, signal.SIGTERM)
            try:
                hub.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(hub.pid, signal.SIGKILL)
                hub.wait()
        log.close()
        if r.failed:
            text = (tmp / "hub.log").read_text()
            for s in [*env.values(), *claude.values()]:
                text = text.replace(s, "<secret>")
            print("---- hub log (last 30 lines) ----\n" + "\n".join(text.splitlines()[-30:]))
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"hub stopped, {tmp} {'deleted' if not tmp.exists() else 'NOT deleted'}")
    print(f"\n{'PASS' if not r.failed else 'FAIL'}: {len(r.rows) - r.failed} of {len(r.rows)} checks passed")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
