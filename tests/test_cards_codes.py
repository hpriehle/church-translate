"""A saved card, 1-click and automatic top-ups, and codes (own key, free hours, % off). Stripe is a fake that
answers like Stripe (never the network); e-mail goes to a fake Resend."""
import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace as NS

import pytest
import stripe

from app.hear import api as hear_api
from app.hear import payments
from app.hear.db import Database, hash_token, normalize_code

from test_hear import client, hear, make_cong  # noqa: F401  (fixtures)
from test_stripe import WHSEC, hook, keys, signed  # noqa: F401
from test_admin_api import meeting

K = "?key=boss"


def auth(token):
    return {"Authorization": f"Bearer {token}"}


class FakeStripe:
    """stripe.StripeClient as far as the hub uses it: remembers every call, answers like Stripe."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.pi_status = "succeeded"
        self.decline: str | None = None
        self.n = 0
        v1 = NS()
        v1.checkout = NS(sessions=NS(create=self._session))
        v1.customers = NS(create=self._customer, update=lambda cid, params: self.calls.append(("customer.update", cid, params)))
        v1.payment_intents = NS(create=self._pi)
        v1.payment_methods = NS(retrieve=self._pm, detach=lambda pm: self.calls.append(("pm.detach", pm)))
        v1.setup_intents = NS(retrieve=lambda si: NS(id=si, status="succeeded", payment_method="pm_card_visa",
                                                     customer="cus_1"))
        self.v1 = v1

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]

    def _session(self, params):
        self.n += 1
        self.calls.append(("session", params))
        return NS(id=f"cs_test_{self.n}", url=f"https://checkout.stripe.com/c/pay/cs_test_{self.n}")

    def _customer(self, params, options=None):
        self.calls.append(("customer", params, options))
        return NS(id="cus_1")

    def _pm(self, pm):
        self.calls.append(("pm", pm))
        return NS(id=pm, type="card", customer="cus_1",
                  card=NS(brand="visa", last4="4242", exp_month=12, exp_year=2030))

    def _pi(self, params, options=None):
        self.n += 1
        self.calls.append(("pi", params, options))
        if self.decline:
            e = stripe.CardError(self.decline, None, "card_declined")
            e.error = NS(payment_intent=NS(id=f"pi_declined_{self.n}"))
            raise e
        na = NS(type="redirect_to_url", redirect_to_url=NS(url="https://hooks.stripe.com/3ds/x")) \
            if self.pi_status == "requires_action" else None
        return NS(id=f"pi_{self.n}", status=self.pi_status, next_action=na)


class FakeResend:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to, subject, text):
        self.sent.append((to, subject, text))
        return True


@pytest.fixture
def fake(monkeypatch, keys):
    f = FakeStripe()
    monkeypatch.setattr(hear_api, "_stripe_client", lambda key: f)
    return f


@pytest.fixture
def mail(hear):
    m = FakeResend()
    hear.resend = m
    return m


def event(kind, obj):
    return json.dumps({"id": f"evt_{kind}", "object": "event", "type": kind, "data": {"object": obj}})


def post_event(client, kind, obj):
    p = event(kind, obj)
    return hook(client, p, signed(p))


def pi_succeeded(cid, pi="pi_1", cents=2000, minutes=240, meta=None, **more):
    return {"id": pi, "object": "payment_intent", "status": "succeeded", "currency": "usd", "amount": cents,
            "amount_received": cents, "payment_method": "pm_card_visa",
            "metadata": meta or {"congregation_id": str(cid), "cents": str(cents), "minutes": str(minutes),
                                 "bundle": "20", **more}}


def new_code(client, **body):
    r = client.post("/api/admin/codes" + K, json=body)
    assert r.status_code == 200, r.text
    return r.json()["code"]


def save_card(client, hear, fake, cong):
    s = {"id": "cs_setup", "object": "checkout.session", "mode": "setup", "status": "complete",
         "setup_intent": "seti_1", "client_reference_id": str(cong["id"]),
         "metadata": {"congregation_id": str(cong["id"])}}
    r = post_event(client, "checkout.session.completed", s)
    assert r.status_code == 200 and r.json()["card_saved"] is True, r.text


# ------------------------------------------------------------------ saved card

def test_save_a_card_through_stripe_setup_and_remove_it(client, hear, fake):
    cong, token = make_cong(hear)
    b = client.get("/api/hc/billing", headers=auth(token)).json()
    assert b["card"] is None and b["can_save_card"] and b["auto_topup"]["enabled"] is False and b["discount"] is None
    r = client.post("/api/hc/card/setup", headers=auth(token))
    assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.com/")
    (_, cparams, copts), = fake.of("customer")
    assert cparams["email"] == "a@example.org" and cparams["metadata"]["congregation_id"] == str(cong["id"])
    assert copts["idempotency_key"].startswith(f"hc-customer-{cong['id']}-")
    (_, p), = fake.of("session")
    assert p["mode"] == "setup" and p["customer"] == "cus_1" and p["currency"] == "usd"
    assert p["setup_intent_data"]["metadata"]["congregation_id"] == str(cong["id"])
    assert p["success_url"] == "https://hear.test/api/hc/card/done"
    assert client.get("/api/hc/card/done").status_code == 200
    # a second setup reuses the customer
    client.post("/api/hc/card/setup", headers=auth(token))
    assert len(fake.of("customer")) == 1
    save_card(client, hear, fake, cong)
    assert ("customer.update", "cus_1", {"invoice_settings": {"default_payment_method": "pm_card_visa"}}) in fake.calls
    card = client.get("/api/hc/billing", headers=auth(token)).json()["card"]
    assert card["brand"] == "visa" and card["last4"] == "4242" and card["exp_year"] == 2030
    assert "4242" not in json.dumps(hear.db.payment_profile(cong["id"])["payment_method_id"])
    # setup_intent.succeeded (if that event is on the webhook too) is the same, once more changes nothing
    si = {"id": "seti_1", "status": "succeeded", "payment_method": "pm_card_visa", "customer": "cus_1",
          "metadata": {"congregation_id": str(cong["id"])}}
    assert post_event(client, "setup_intent.succeeded", si).json()["card_saved"] is True
    client.post("/api/hc/auto-topup", json={"enabled": True, "usd": 50}, headers=auth(token))
    r = client.post("/api/hc/card/remove", headers=auth(token))
    assert r.status_code == 200 and r.json()["card"] is None and r.json()["auto_topup"]["enabled"] is False
    assert ("pm.detach", "pm_card_visa") in fake.calls
    b = client.get("/api/hc/billing", headers=auth(token)).json()
    assert b["card"] is None and b["auto_topup"]["enabled"] is False


def test_card_routes_need_a_device_and_stripe(client, hear, monkeypatch):
    cong, token = make_cong(hear)
    for path in ("/api/hc/card/setup", "/api/hc/card/remove"):
        assert client.post(path).status_code == 403
    assert client.post("/api/hc/topup", json={"usd": 20}).status_code == 403
    r = client.post("/api/hc/card/setup", headers=auth(token))
    assert r.status_code == 503 and r.json()["error"] == "not_configured"
    assert client.post("/api/hc/topup", json={"usd": 20}, headers=auth(token)).status_code == 503


def test_main_email_change_moves_receipts(client, hear, fake):
    cong, token = make_cong(hear)
    client.post("/api/hc/card/setup", headers=auth(token))
    client.post("/api/hc/emails", json={"email": "clerk@example.org", "main": True}, headers=auth(token))
    assert ("customer.update", "cus_1", {"email": "clerk@example.org"}) in fake.calls


def test_checkout_can_save_the_card(client, hear, fake):
    cong, token = make_cong(hear)
    r = client.post("/api/hc/checkout", json={"usd": 20, "save_card": True}, headers=auth(token))
    assert r.status_code == 200
    (_, p), = fake.of("session")
    assert p["customer"] == "cus_1" and "customer_email" not in p
    assert p["payment_intent_data"]["setup_future_usage"] == "off_session"
    assert p["payment_intent_data"]["metadata"]["save_card"] == "1"
    # Checkout's completed event credits; its payment intent's event then saves the card (and credits nothing more)
    s = {"id": "cs_test_1", "mode": "payment", "payment_status": "paid", "currency": "usd", "amount_total": 2000,
         "payment_intent": "pi_co", "metadata": p["metadata"]}
    assert post_event(client, "checkout.session.completed", s).json()["credited"] is True
    assert hear.db.payment_profile(cong["id"])["payment_method_id"] is None
    r = post_event(client, "payment_intent.succeeded", {**pi_succeeded(cong["id"], "pi_co"),
                                                        "metadata": p["payment_intent_data"]["metadata"]})
    assert r.json()["credited"] is False
    assert hear.db.payment_profile(cong["id"])["card_last4"] == "4242"
    assert len(hear.db.credits(cong["id"])) == 1
    # an old app's Checkout (no save_card) saves nothing and uses customer_email
    client.post("/api/hc/checkout", json={"usd": 20}, headers=auth(token))
    (_, p2) = fake.of("session")[-1]
    assert "setup_future_usage" not in p2["payment_intent_data"] and p2["customer"] == "cus_1"


# ------------------------------------------------------------------ 1-click top-up

def test_one_click_charges_the_saved_card_and_credits_on_the_webhook(client, hear, fake):
    cong, token = make_cong(hear)
    r = client.post("/api/hc/topup", json={"usd": 50}, headers=auth(token))
    assert r.status_code == 409 and r.json()["error"] == "no_card"
    client.post("/api/hc/card/setup", headers=auth(token))
    save_card(client, hear, fake, cong)
    r = client.post("/api/hc/topup", json={"usd": 30}, headers=auth(token))
    assert r.status_code == 400 and r.json()["error"] == "not_a_bundle"
    r = client.post("/api/hc/topup", json={"usd": 50, "request_id": "click-1"}, headers=auth(token))
    assert r.status_code == 200 and r.json()["status"] == "succeeded" and r.json()["hours"] == 11, r.text
    (_, p, opts), = fake.of("pi")
    assert p["amount"] == 5000 and p["currency"] == "usd" and p["confirm"] is True and "off_session" not in p
    assert p["customer"] == "cus_1" and p["payment_method"] == "pm_card_visa" and p["receipt_email"] == "a@example.org"
    assert p["metadata"]["minutes"] == "660" and p["metadata"]["kind"] == "one_click"
    assert p["return_url"] == "https://hear.test/api/hc/topup/done"
    assert opts == {"idempotency_key": f"hc-topup-{cong['id']}-click-1"}
    assert hear.db.credits(cong["id"]) == []   # only the webhook credits
    pi = pi_succeeded(cong["id"], r.json()["id"], 5000, 660, meta=p["metadata"])
    assert post_event(client, "payment_intent.succeeded", pi).json()["credited"] is True
    assert post_event(client, "payment_intent.succeeded", pi).json()["credited"] is False
    assert client.get("/api/hc/billing", headers=auth(token)).json()["paid_hours_left"] == 11


def test_one_click_3d_secure_and_declines(client, hear, fake):
    cong, token = make_cong(hear)
    client.post("/api/hc/card/setup", headers=auth(token))
    save_card(client, hear, fake, cong)
    fake.pi_status = "requires_action"
    r = client.post("/api/hc/topup", json={"usd": 20}, headers=auth(token))
    assert r.status_code == 200 and r.json()["status"] == "requires_action"
    assert r.json()["url"] == "https://hooks.stripe.com/3ds/x" and r.json()["ok"] is False
    fake.decline = "Your card has insufficient funds."
    r = client.post("/api/hc/topup", json={"usd": 20}, headers=auth(token))
    assert r.status_code == 402 and r.json() == {"error": "card_declined", "message": "Your card has insufficient funds."}


def test_payments_without_our_metadata_are_ignored(client, hear, keys):
    cong, _ = make_cong(hear)
    obj = {"id": "pi_other", "status": "succeeded", "currency": "usd", "amount": 999, "metadata": {}}
    assert post_event(client, "payment_intent.succeeded", obj).json() == {"ok": True, "credited": False}
    assert hear.db.credits() == []


# ------------------------------------------------------------------ auto top-up

def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_auto_top_up_after_a_meeting_once_a_day_and_off_on_failure(client, hear, fake, mail):
    cong, token = make_cong(hear)
    r = client.post("/api/hc/auto-topup", json={"enabled": True, "usd": 20}, headers=auth(token))
    assert r.status_code == 409 and r.json()["error"] == "no_card"
    client.post("/api/hc/card/setup", headers=auth(token))
    save_card(client, hear, fake, cong)
    assert client.post("/api/hc/auto-topup", json={"enabled": True, "usd": 30}, headers=auth(token)).status_code == 400
    r = client.post("/api/hc/auto-topup", json={"enabled": True, "usd": 50}, headers=auth(token))
    assert r.json()["auto_topup"] == {"enabled": True, "usd": 50, "below_hours": 1.0, "last_at": None}
    hear.db.add_credit(cong["id"], "pi_before", 2000)          # 4 paid hours: plenty
    assert run(payments.auto_topup(cong["id"])) == "skipped: enough left"
    meeting(hear, cong, "m1", 60 + 200)                       # the free hour, then 3 h 20 min of the 4 paid ones
    assert run(payments.auto_topup(cong["id"])) == "charged"
    (_, p, opts), = fake.of("pi")
    assert p["off_session"] is True and p["confirm"] is True and p["amount"] == 5000
    assert p["metadata"]["kind"] == "auto" and opts["idempotency_key"].startswith(f"hc-auto-{cong['id']}-")
    assert run(payments.auto_topup(cong["id"])) == "skipped: one per 24 h"
    assert len(fake.of("pi")) == 1
    # the webhook credits it and e-mails the main address, once
    pi = pi_succeeded(cong["id"], hear.db.payment_profile(cong["id"])["auto_last_pi"], 5000, 660, kind="auto")
    assert post_event(client, "payment_intent.succeeded", pi).json()["credited"] is True
    post_event(client, "payment_intent.succeeded", pi)
    (to, subject, text), = mail.sent
    assert to == "a@example.org" and "automatically" in subject and "$50.00" in text and "11 language-hours" in text
    # a day later, a declined card: auto top-up goes off and the main address is told
    hear.db._x("UPDATE payment_profiles SET auto_last_at = ?", (time.time() - 25 * 3600,))
    meeting(hear, cong, "m2", 11 * 60, first_seq=5000)
    fake.decline = "Your card was declined."
    assert run(payments.auto_topup(cong["id"])) == "failed"
    assert hear.db.payment_profile(cong["id"])["auto_topup"] is False
    assert "didn't go through" in mail.sent[-1][1] and "Your card was declined." in mail.sent[-1][2]
    # Stripe's payment_failed for it afterwards doesn't e-mail again
    failed = {"id": "pi_declined_2", "metadata": {"congregation_id": str(cong["id"]), "kind": "auto"},
              "last_payment_error": {"message": "declined"}}
    post_event(client, "payment_intent.payment_failed", failed)
    assert len(mail.sent) == 2


def test_auto_top_up_turns_off_on_a_late_failure(client, hear, fake, mail):
    cong, token = make_cong(hear)
    client.post("/api/hc/card/setup", headers=auth(token))
    save_card(client, hear, fake, cong)
    client.post("/api/hc/auto-topup", json={"enabled": True}, headers=auth(token))
    assert hear.db.payment_profile(cong["id"])["auto_topup_cents"] == 2000
    meeting(hear, cong, "m1", 70)
    fake.pi_status = "processing"
    assert run(payments.auto_topup(cong["id"])) == "charged"
    failed = {"id": hear.db.payment_profile(cong["id"])["auto_last_pi"],
              "metadata": {"congregation_id": str(cong["id"]), "kind": "auto"},
              "last_payment_error": {"message": "The bank said no."}}
    post_event(client, "payment_intent.payment_failed", failed)
    assert hear.db.payment_profile(cong["id"])["auto_topup"] is False
    assert "The bank said no." in mail.sent[-1][2]


def test_a_meeting_ending_starts_the_auto_top_up_check(hear, monkeypatch):
    seen = []
    monkeypatch.setattr(payments, "auto_topup", lambda cid: seen.append(cid) or asyncio.sleep(0))

    async def go():
        hear.live.after_end(7)
        await asyncio.sleep(0)
    run(go())
    assert seen == [7]


def test_auto_top_up_is_never_for_own_key_or_without_a_card(hear, fake):
    cong, _ = make_cong(hear)
    assert run(payments.auto_topup(cong["id"])) == "skipped: off"
    hear.db.set_billing(cong["id"], "own_key")
    assert run(payments.auto_topup(cong["id"])) == "skipped: not on credits"


# ------------------------------------------------------------------ codes

def test_admin_makes_lists_and_voids_codes(client, hear):
    assert client.post("/api/admin/codes", json={"type": "free_hours", "value": 2}).status_code == 403
    c = new_code(client, type="free_hours", value=5, label="Easter gift", max_uses=3)
    assert len(c["code"]) == 9 and c["code"][4] == "-" and c["status"] == "active" and c["uses"] == 0
    assert c["message"] == "5 free language-hours added. They never expire."
    for bad in ({"type": "nope"}, {"type": "percent_off", "value": 0}, {"type": "percent_off", "value": 100},
                {"type": "free_hours", "value": 0}, {"type": "own_key", "expires_at": 5}):
        assert client.post("/api/admin/codes" + K, json=bad).status_code == 400, bad
    assert client.post("/api/admin/codes" + K, json={"type": "own_key", "congregation_id": 999}).status_code == 404
    p = new_code(client, type="percent_off", value=20, label="Launch")
    listed = client.get("/api/admin/codes" + K).json()["codes"]
    assert [x["id"] for x in listed] == [p["id"], c["id"]] and listed[1]["code"] == c["code"]
    assert [x["id"] for x in client.get("/api/admin/codes?type=percent_off&key=boss").json()["codes"]] == [p["id"]]
    assert [x["id"] for x in client.get("/api/admin/codes?q=easter&key=boss").json()["codes"]] == [c["id"]]
    r = client.post(f"/api/admin/codes/{p['id']}/void" + K)
    assert r.status_code == 200 and r.json()["code"]["status"] == "void"
    assert client.get("/api/admin/codes?status=void&key=boss").json()["codes"][0]["id"] == p["id"]
    assert client.post("/api/admin/codes/999/void" + K).status_code == 404


def test_codes_are_never_logged(client, hear, caplog):
    caplog.set_level("INFO")
    cong, token = make_cong(hear)
    c = new_code(client, type="free_hours", value=1)
    client.post("/api/hc/codes/redeem", json={"code": c["code"]}, headers=auth(token))
    assert c["code"] not in caplog.text and c["code"].replace("-", "") not in caplog.text


def test_free_hours_code_adds_hours_that_never_expire(client, hear):
    cong, token = make_cong(hear)
    c = new_code(client, type="free_hours", value=3)
    r = client.post("/api/hc/codes/redeem", json={"code": c["code"].lower().replace("-", " ")}, headers=auth(token))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["type"] == "free_hours" and j["value"] == 3 and j["already"] is False and j["paid_hours_left"] == 3
    assert j["message"].startswith("3 free language-hours added")
    # a retry: nothing more
    j = client.post("/api/hc/codes/redeem", json={"code": c["code"]}, headers=auth(token)).json()
    assert j["already"] is True and j["paid_hours_left"] == 3
    # next month: still there (the free hour is per month, these aren't)
    later = time.time() + 40 * 86400
    assert hear.db.balance(cong["id"], now=later)["balance_min"] == 180
    rows = client.get(f"/api/admin/transactions?congregation={cong['id']}&key=boss").json()["transactions"]
    assert rows[0]["kind"] == "free_hours" and rows[0]["amount_usd"] == 0 and rows[0]["minutes"] == 180
    assert rows[0]["refundable"] is False
    charges = client.get("/api/hc/usage", headers=auth(token)).json()["charges"]
    assert charges[0]["kind"] == "free_hours" and charges[0]["hours"] == 3
    # used by a meeting like paid hours, at no cost
    meeting(hear, cong, "m1", 90)
    b = hear.db.balance(cong["id"])
    assert b["balance_min"] == 150 and b["balance_usd"] == 0


def test_own_key_code_from_the_new_table(client, hear):
    cong, token = make_cong(hear)
    c = new_code(client, type="own_key", label="Riga")
    r = client.post("/api/hc/codes/redeem", json={"code": c["code"]}, headers=auth(token))
    assert r.json()["billing"] == "own_key" and r.json()["type"] == "own_key"
    # the old app's routes see it too
    old = client.get("/api/admin/own-key-codes" + K).json()["codes"]
    assert old[0]["status"] == "used" and old[0]["used_by"] == cong["slug"]


def test_one_error_for_every_bad_code(client, hear):
    a, ta = make_cong(hear, "Dakar Ward", "a@x.org")
    b, tb = make_cong(hear, "Thies Branch", "b@x.org")
    used = new_code(client, type="free_hours", value=1)
    client.post("/api/hc/codes/redeem", json={"code": used["code"]}, headers=auth(ta))
    expired = new_code(client, type="free_hours", value=1, expires_at=time.time() + 60)
    hear.db._x("UPDATE codes SET expires_at = ? WHERE id = ?", (time.time() - 1, expired["id"]))
    void = new_code(client, type="free_hours", value=1)
    client.post(f"/api/admin/codes/{void['id']}/void" + K)
    theirs = new_code(client, type="free_hours", value=1, congregation_id=a["id"])
    for code in ("ZZZZ-ZZZZ", used["code"], expired["code"], void["code"], theirs["code"], "not a code"):
        r = client.post("/api/hc/codes/redeem", json={"code": code}, headers=auth(tb))
        assert r.status_code == 403 and r.json() == {"error": "bad_code", "message": "That code isn't valid."}, code
    assert client.get("/api/admin/codes?status=expired&key=boss").json()["codes"][0]["id"] == expired["id"]
    # the assigned one works for its congregation
    assert client.post("/api/hc/codes/redeem", json={"code": theirs["code"]}, headers=auth(ta)).status_code == 200


def test_max_uses_and_the_failure_limit(client, hear):
    congs = [make_cong(hear, f"Ward {i}", f"w{i}@x.org") for i in range(3)]
    c = new_code(client, type="free_hours", value=1, max_uses=2)
    assert [client.post("/api/hc/codes/redeem", json={"code": c["code"]}, headers=auth(t)).status_code
            for _, t in congs] == [200, 200, 403]
    listed, = client.get("/api/admin/codes" + K).json()["codes"]
    assert listed["uses"] == 2 and listed["status"] == "used" and len(listed["redemptions"]) == 2
    _, t = congs[2]
    for _ in range(9):
        client.post("/api/hc/codes/redeem", json={"code": "ZZZZ-ZZZZ"}, headers=auth(t))
    r = client.post("/api/hc/codes/redeem", json={"code": "ZZZZ-ZZZZ"}, headers=auth(t))
    assert r.status_code == 429 and r.json()["error"] == "locked"


def test_percent_off_waits_for_the_next_bundle_and_is_used_once(client, hear, fake):
    cong, token = make_cong(hear)
    c = new_code(client, type="percent_off", value=20, max_uses=5)
    j = client.post("/api/hc/codes/redeem", json={"code": c["code"]}, headers=auth(token)).json()
    assert j["message"] == "20% off your next bundle." and j["discount"] == {"percent": 20, "label": ""}
    other = new_code(client, type="percent_off", value=10)
    r = client.post("/api/hc/codes/redeem", json={"code": other["code"]}, headers=auth(token))
    assert r.status_code == 409 and r.json()["error"] == "pending_discount"
    assert client.get("/api/admin/codes?type=percent_off&key=boss").json()["codes"][0]["uses"] == 0   # not used up
    # Checkout: 20% off the $50 bundle, the bundle's hours; an abandoned session uses nothing up
    r = client.post("/api/hc/checkout", json={"usd": 50}, headers=auth(token))
    assert r.json()["charged_usd"] == 40 and r.json()["percent_off"] == 20 and r.json()["hours"] == 11
    (_, p), = fake.of("session")
    assert p["line_items"][0]["price_data"]["unit_amount"] == 4000
    assert "20% off" in p["line_items"][0]["price_data"]["product_data"]["description"]
    assert p["metadata"]["minutes"] == "660" and p["metadata"]["cents"] == "4000"
    # 1-click gets it too while it waits
    client.post("/api/hc/card/setup", headers=auth(token))
    save_card(client, hear, fake, cong)
    r = client.post("/api/hc/topup", json={"usd": 100}, headers=auth(token))
    assert r.json()["charged_usd"] == 80
    (_, pp, _), = fake.of("pi")
    # the 1-click payment succeeds: credited with the bundle's hours at the price paid, the discount used up
    pi = pi_succeeded(cong["id"], "pi_disc", 8000, 1500, meta=pp["metadata"])
    assert post_event(client, "payment_intent.succeeded", pi).json()["credited"] is True
    assert hear.db.credit_by_payment("pi_disc")["seconds"] == 25 * 3600
    assert hear.db.pending_discount(cong["id"]) is None
    # the next purchase is full price; the Checkout made earlier, paid late, still credits
    client.post("/api/hc/checkout", json={"usd": 50}, headers=auth(token))
    assert fake.of("session")[-1][1]["line_items"][0]["price_data"]["unit_amount"] == 5000
    red, = hear.db.code_redemptions(cid=cong["id"])
    assert red["status"] == "consumed" and red["payment_id"] == "pi_disc"


def test_check_a_code_before_sign_up_and_redeem_it_at_sign_up(client, hear):
    hear.verify_email = False
    c = new_code(client, type="free_hours", value=2)
    r = client.post("/api/hc/codes/check", json={"code": c["code"]})
    assert r.json() == {"ok": True, "type": "free_hours", "value": 2,
                        "message": "2 free language-hours added. They never expire."}
    signup = {"church": "LDS", "name": "Dakar Ward", "unit_type": "ward", "email": "clerk@x.org"}
    r = client.post("/api/hc/signup", json={**signup, "code": "WRONG-CODE"})
    assert r.status_code == 403 and r.json()["message"] == "That code isn't valid."
    assert hear.db.list_congregations() == []        # nothing made: the app can fix the code and send again
    r = client.post("/api/hc/signup", json={**signup, "code": c["code"]})
    assert r.status_code == 200, r.text
    assert r.json()["code"]["type"] == "free_hours" and r.json()["code"]["already"] is False
    b = client.get("/api/hc/billing", headers=auth(r.json()["device_token"])).json()
    assert b["paid_hours_left"] == 2
    own = new_code(client, type="own_key")
    r = client.post("/api/hc/signup", json={**signup, "name": "Riga Ward", "code": own["code"]})
    assert r.json()["billing"] == "own_key"
    # the card-save page works right after sign-up (onboarding): it only needs the new device token


def test_old_own_key_codes_move_to_the_codes_table(tmp_path, client, hear):
    path = tmp_path / "old.db"
    db = Database(path)
    c = db.create_congregation(church="LDS", name="Riga Ward", unit_type="ward", email="r@x.org")
    old_unused, old_used = db.mint_own_key_code("Connecticut Ward"), db.mint_own_key_code("Riga")
    assert db.redeem_own_key_code(old_used, c["id"])
    db._db.close()
    # make it look like a database from before the codes table
    con = sqlite3.connect(path)
    rows = con.execute("SELECT id, code_hash, label, created_at, uses FROM codes").fetchall()
    con.execute("DROP TABLE code_redemptions")
    con.execute("DROP TABLE codes")
    for kid, h, label, at, uses in rows:
        con.execute("INSERT INTO own_key_codes (id, code_hash, label, created_at, used_at, congregation_id)"
                    " VALUES (?,?,?,?,?,?)", (kid, h, label, at, at if uses else None, c["id"] if uses else None))
    con.commit()
    con.close()
    db = Database(path)
    assert db.own_key_code_ok(old_unused) and not db.own_key_code_ok(old_used)
    listed = {x["label"]: x for x in db.own_key_codes()}
    assert listed["Riga"]["used_by"] == "riga-ward" and listed["Connecticut Ward"]["status"] == "unused"
    assert db.codes()[0]["code"] is None   # only the hash was ever kept for those
    assert normalize_code(old_unused) and hash_token(normalize_code(old_unused))
