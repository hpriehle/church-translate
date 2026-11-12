"""Buying credit: Stripe Checkout (a mocked Stripe client, never the network), the signed webhook that credits the
ledger once per payment, the balance on the billing answer and the Mac's speak page proxy, and the 6-hour resume
allowed only from the Mac that started the meeting."""
import asyncio
import json
import time
from types import SimpleNamespace as NS

import pytest
import stripe

from app import main
from app.engine import PublishClient
from app.hear import api as hear_api

from test_hear import client, engine, hear, make_cong, recv, server, start  # noqa: F401  (fixtures)
from test_metering import cong_db

WHSEC = "whsec_test_secret"


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WHSEC)


class FakeStripe:
    """Stands in for stripe.StripeClient: remembers the Checkout session parameters, answers like Stripe."""

    def __init__(self):
        self.created, self.keys = [], []
        self.v1 = NS(checkout=NS(sessions=NS(create=self._create)))

    def _create(self, params):
        self.created.append(params)
        return NS(id=f"cs_test_{len(self.created)}", url=f"https://checkout.stripe.com/c/pay/cs_test_{len(self.created)}")


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(hear_api, "_stripe_client", lambda key: fake.keys.append(key) or fake)
    return fake


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ Checkout

def test_checkout_makes_a_card_only_session_for_the_congregation(client, hear, keys, fake_stripe):
    cong, token = make_cong(hear)
    r = client.post("/api/hc/checkout", json={"usd": 25}, headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"url": "https://checkout.stripe.com/c/pay/cs_test_1", "id": "cs_test_1", "usd": 25}
    p, = fake_stripe.created
    assert fake_stripe.keys == ["sk_test_dummy"]
    assert p["mode"] == "payment" and p["payment_method_types"] == ["card"]
    item, = p["line_items"]
    assert item["quantity"] == 1 and item["price_data"]["currency"] == "usd" and item["price_data"]["unit_amount"] == 2500
    assert "5 language-hours" in item["price_data"]["product_data"]["description"]
    assert p["client_reference_id"] == str(cong["id"]) and p["metadata"]["congregation_id"] == str(cong["id"])
    assert p["payment_intent_data"]["metadata"]["cents"] == "2500"
    assert p["success_url"] == "https://hear.test/api/hc/checkout/done"
    assert p["cancel_url"] == "https://hear.test/api/hc/checkout/cancelled"
    assert client.get("/api/hc/checkout/done").status_code == 200


def test_checkout_refuses_bad_amounts_strangers_and_own_key(client, hear, keys, fake_stripe):
    cong, token = make_cong(hear)
    for usd in (0, 4, 501, "ten"):
        assert client.post("/api/hc/checkout", json={"usd": usd}, headers=auth(token)).status_code == 422
    assert client.post("/api/hc/checkout", json={"usd": 10}).status_code == 403
    assert client.post("/api/hc/checkout", json={"usd": 10}, headers=auth("nope")).status_code == 403
    hear.db._x("UPDATE congregations SET billing = 'own_key' WHERE id = ?", (cong["id"],))
    assert client.post("/api/hc/checkout", json={"usd": 10}, headers=auth(token)).status_code == 409
    assert fake_stripe.created == []


def test_without_a_stripe_key_buying_says_not_configured(client, hear, monkeypatch, fake_stripe):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    cong, token = make_cong(hear)
    r = client.post("/api/hc/checkout", json={"usd": 10}, headers=auth(token))
    assert r.status_code == 503 and r.json()["error"] == "not_configured"
    assert client.post("/api/stripe/webhook", content=b"{}").status_code == 503
    b = client.get("/api/hc/billing", headers=auth(token)).json()
    assert b["can_buy"] is False and b["out_of_credit"] is False and b["language_hours_left"] == 1
    assert fake_stripe.created == []


def test_stripe_failing_is_a_502_not_a_crash(client, hear, keys, monkeypatch):
    def boom(key):
        raise stripe.APIConnectionError("no network here")
    monkeypatch.setattr(hear_api, "_stripe_client", boom)
    _, token = make_cong(hear)
    assert client.post("/api/hc/checkout", json={"usd": 10}, headers=auth(token)).status_code == 502


# ------------------------------------------------------------------ the webhook

def completed(cid, cents=2500, pi="pi_test_1", **over):
    obj = {"id": "cs_test_1", "object": "checkout.session", "mode": "payment", "payment_status": "paid",
           "status": "complete", "currency": "usd", "amount_total": cents, "payment_intent": pi,
           "client_reference_id": str(cid), "metadata": {"congregation_id": str(cid), "cents": str(cents)}, **over}
    return json.dumps({"id": "evt_test_1", "object": "event", "type": "checkout.session.completed",
                       "data": {"object": obj}})


def signed(payload: str, secret=WHSEC, at=None):
    """The Stripe-Signature header Stripe would send (the SDK's own signing, no network)."""
    at = int(at or time.time())
    sig = stripe.WebhookSignature._compute_signature(f"{at}.{payload}", secret)
    return {"Stripe-Signature": f"t={at},v1={sig}", "Content-Type": "application/json"}


def hook(client, payload, headers):
    return client.post("/api/stripe/webhook", content=payload.encode(), headers=headers)


def test_a_signed_paid_checkout_credits_once(client, hear, keys):
    cong, token = make_cong(hear)
    payload = completed(cong["id"])
    r = hook(client, payload, signed(payload))
    assert r.status_code == 200 and r.json()["credited"] is True
    credit, = hear.db.credits(cong["id"])
    assert credit["payment_id"] == "pi_test_1" and credit["cents"] == 2500
    # Stripe retries the same event (or the same signed request is replayed in time): credited once
    r = hook(client, payload, signed(payload))
    assert r.status_code == 200 and r.json()["credited"] is False
    assert len(hear.db.credits(cong["id"])) == 1
    b = client.get("/api/hc/billing", headers=auth(token)).json()
    assert b["balance_usd"] == 25 and b["language_hours_left"] == 6 and not b["out_of_credit"] and b["can_buy"]


def test_bad_and_replayed_signatures_are_refused(client, hear, keys):
    cong, _ = make_cong(hear)
    payload = completed(cong["id"])
    assert hook(client, payload, {}).status_code == 400                                     # unsigned
    assert hook(client, payload, signed(payload, secret="whsec_someone_else")).status_code == 400
    tampered = completed(cong["id"], cents=999_900)
    assert hook(client, tampered, signed(payload)).status_code == 400                       # body changed
    old = signed(payload, at=time.time() - 3600)                                           # a captured request,
    assert hook(client, payload, old).status_code == 400                                    # replayed an hour later
    assert hear.db.credits(cong["id"]) == []


def test_only_paid_checkouts_credit(client, hear, keys):
    cong, _ = make_cong(hear)
    for payload in (completed(cong["id"], payment_status="unpaid"),        # a delayed method: never offered, but
                    completed(cong["id"], currency="eur"),                  # if one slips through it isn't counted
                    completed(cong["id"], mode="subscription"),
                    json.dumps({"id": "evt_2", "type": "payment_intent.succeeded", "data": {"object": {}}}),
                    completed(9999, pi="pi_ghost")):                       # no such congregation: logged, acked
        r = hook(client, payload, signed(payload))
        assert r.status_code == 200 and r.json()["credited"] is False
    assert hear.db.credits(cong["id"]) == []


# ------------------------------------------------------------------ balance on the billing answer and /speak

def test_billing_answer_says_out_of_credit(client, hear, keys):
    cong, token = make_cong(hear)
    hear.db.start_session(cong["id"], ["fr"], code="s1", billing="credits")
    hear.db.record_speech(cong["id"], "s1", [{"seq": i, "s": 60.0, "langs": ["fr"]} for i in range(90)])
    b = client.get("/api/hc/billing", headers=auth(token)).json()
    assert b["out_of_credit"] and b["balance_usd"] == -2.5 and b["language_hours_left"] == -0.5
    assert "Out of credit (balance -$2.50). Buy credit to start the next meeting." == b["message"]
    assert b["buy_usd"] == [10, 25, 50, 100] and b["usd_per_language_hour"] == 5


def test_the_speak_page_reads_credit_and_buys_through_the_macs_hub_link(client, hear, monkeypatch):
    calls = []

    async def hub_api(method, path, body=None):
        calls.append((method, path, body))
        return (200, {"billing": "credits", "out_of_credit": True}) if method == "GET" else (200, {"url": "https://x"})

    pc = PublishClient("wss://hear.test/ws/engine", "tok", "Mac")
    monkeypatch.setattr(pc, "hub_api", hub_api)
    monkeypatch.setattr(main.S, "engine_client", pc, raising=False)
    assert client.get("/api/billing").status_code in (401, 403)                   # signed-in pages only
    assert client.get("/api/billing?key=boss").json() == {"billing": "credits", "out_of_credit": True}
    assert client.post("/api/buy-credit?key=boss", json={"usd": 25}).json() == {"url": "https://x"}
    assert client.post("/api/buy-credit?key=boss", json={"usd": "lots"}).status_code == 400
    assert calls == [("GET", "/api/hc/billing", None), ("POST", "/api/hc/checkout", {"usd": 25})]
    monkeypatch.setattr(main.S, "engine_client", None, raising=False)
    assert client.get("/api/billing?key=boss").json() == {"billing": None}         # not a Hear Church Mac


# ------------------------------------------------------------------ the resume loophole

def test_recent_session_matches_only_the_mac_that_started_it():
    db, c = cong_db()
    db.start_session(c["id"], ["fr"], code="s1", billing="credits", device_id=7)
    assert db.recent_session(c["id"], "s1", 7)
    assert not db.recent_session(c["id"], "s1", 8)
    assert not db.recent_session(c["id"], "s2", 7)


async def test_another_mac_cant_resume_a_meeting_to_skip_the_credit_check(server, hear):
    cong, token = make_cong(hear)
    other = hear.db.add_device(cong["id"], "Second Mac")
    a = await engine(server, token)
    code = (await start(a, ["fr"]))["session"]
    await a.send(json.dumps({"type": "meter", "session": code,
                             "items": [{"seq": i, "s": 60.0, "langs": ["fr"]} for i in range(70)]}))
    await recv(a, "meter_ok")
    hear.live.end(hear.live.get(cong["id"]), notify=False)       # the meeting dropped past the grace / hub restart
    b = await engine(server, other)
    refused = await start(b, ["fr"], resume=code)                 # the second Mac borrows the code: no
    assert refused["type"] == "session_refused" and "credit" in refused["message"]
    again = await start(a, ["fr"], resume=code)                   # the Mac that started it carries on
    assert again["type"] == "session_started"
    assert len(hear.db.sessions(cong["id"])) == 2
    await a.close()
    await b.close()
    await asyncio.sleep(0.05)
