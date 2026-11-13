"""End-to-end test of the paid credits chain against a real local hub, Stripe in TEST mode only.

    uv run python scripts/e2e_credits.py            [--env ~/.config/hearchurch/stripe-test.env] [--usd 10]

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
8. PASS/FAIL per step, exit 1 on any failure; the hub is stopped and the temp dir deleted.

No Anthropic call (the translation proxy isn't part of this), no live keys, nothing deployed. Stripe is called
only in step 3 (create a test Checkout session, then read it back).
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
STRIPE_VARS = ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET")
USD_PER_LANGUAGE_HOUR = 5.0
FREE_MIN = 60


# ------------------------------------------------------------------ the hub (child process)

def serve(port: int, home: Path) -> None:
    """Run the hub in this process: the real app and routes with lifespan off (no Whisper, voices or tunnel),
    the Hear registry opened on a database in `home`, like the tests' `server` fixture."""
    sys.path.insert(0, str(ROOT))
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

def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.removeprefix("export ").split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in STRIPE_VARS and v:
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

async def run(base: str, env: dict[str, str], usd: int, r: Report) -> None:
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
            r.step("3 the session is test mode, card only, for the right amount and congregation",
                   s.get("livemode") is False and s.get("amount_total") == cents and s.get("currency") == "usd"
                   and s.get("payment_method_types") == ["card"] and s.get("mode") == "payment"
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", type=Path, default=DEFAULT_ENV, help=f"env file with {', '.join(STRIPE_VARS)}")
    ap.add_argument("--usd", type=int, default=10, help="top-up in whole dollars (5–500)")
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

    tmp = Path(tempfile.mkdtemp(prefix="hc-e2e-"))
    port = free_port()
    child_env = {k: v for k, v in os.environ.items() if k not in STRIPE_VARS and k not in
                 ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")}   # no Claude key: nothing can reach Anthropic
    child_env.update(env)
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
            asyncio.run(run(base, env, a.usd, r))
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
            for s in env.values():
                text = text.replace(s, "<secret>")
            print("---- hub log (last 30 lines) ----\n" + "\n".join(text.splitlines()[-30:]))
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"hub stopped, {tmp} {'deleted' if not tmp.exists() else 'NOT deleted'}")
    print(f"\n{'PASS' if not r.failed else 'FAIL'}: {len(r.rows) - r.failed} of {len(r.rows)} checks passed")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
