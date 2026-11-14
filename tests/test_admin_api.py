"""The operator JSON for Siteboard (app/hear/admin.py): stats, the ledger, refunds through Stripe (mocked: never the
network), voiding own-key codes, one congregation, and the app version a Mac reports."""
import asyncio
import json
import time
from types import SimpleNamespace as NS

import pytest
import stripe

from app.hear import api as hear_api

from test_hear import client, engine, hear, make_cong, recv, server  # noqa: F401  (fixtures)
from test_metering import speak
from test_refunds import refunded
from test_stripe import completed, hook, keys, signed  # noqa: F401  (fixtures)

K = "?key=boss"


class FakeRefunds:
    """Stands in for stripe.StripeClient: remembers each refund asked for, answers like Stripe (or raises)."""

    def __init__(self):
        self.created, self.raises, self.status = [], None, "succeeded"
        self.v1 = NS(refunds=NS(create=self._create))

    def _create(self, params, options=None):
        if self.raises:
            raise self.raises
        self.created.append((params, options))
        return NS(id=f"re_test_{len(self.created)}", status=self.status, amount=params["amount"])


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeRefunds()
    monkeypatch.setattr(hear_api, "_stripe_client", lambda key: fake)
    return fake


def meeting(hear, cong, code, minutes, langs=("fr",), billing="credits", first_seq=1):
    sid = hear.db.start_session(cong["id"], list(langs), code=code, billing=billing)
    speak(hear.db, cong["id"], code, minutes, langs, first_seq)
    hear.db.end_session(sid)
    return sid


def test_admin_routes_need_the_operator(client, hear):
    cong, _ = make_cong(hear)
    for path in ("/api/admin/stats", "/api/admin/transactions", f"/api/admin/congregations/{cong['id']}"):
        assert client.get(path).status_code == 403, path
        assert client.get(path + K).status_code == 200, path
    assert client.post("/api/admin/refunds", json={"payment_id": "pi_1"}).status_code == 403
    assert client.post("/api/admin/own-key-codes/1/void").status_code == 403


# ------------------------------------------------------------------ transactions

def test_transactions_list_the_whole_ledger_newest_first(client, hear):
    a, _ = make_cong(hear)
    b, _ = make_cong(hear, "Thiès Branch", "b@example.org")
    hear.db.add_credit(a["id"], "pi_a1", 2500)
    hear.db.add_credit(b["id"], "goodwill-nov", 500)                      # added by hand: a manual credit
    meeting(hear, a, "m1", 90, langs=("fr", "es"))                         # 180 language-minutes: 60 free, 120 paid
    hear.db.refund_credit("pi_a1", 500)
    r = client.get("/api/admin/transactions" + K).json()
    kinds = [t["kind"] for t in r["transactions"]]
    assert sorted(kinds) == ["manual_credit", "refund", "top_up", "usage"] and r["next_before"] is None
    assert [t["at"] for t in r["transactions"]] == sorted((t["at"] for t in r["transactions"]), reverse=True)
    t = {x["kind"]: x for x in r["transactions"]}
    assert t["top_up"]["payment_id"] == "pi_a1" and t["top_up"]["amount_usd"] == 25 and t["top_up"]["refundable"]
    assert t["top_up"]["congregation"] == {"id": a["id"], "slug": "dakar-ward", "name": "Dakar Ward"}
    assert t["manual_credit"]["congregation"]["slug"] == "thies-branch" and not t["manual_credit"]["refundable"]
    assert t["refund"]["cents"] == 500 and t["refund"]["amount_cents"] == -500 and t["refund"]["source"] == "stripe"
    u = t["usage"]
    assert u["language_minutes"] == {"es": 90, "fr": 90} and u["minutes"] == 180
    assert u["free_minutes"] == 60 and u["paid_minutes"] == 120 and u["cost_usd"] == 10 and u["amount_cents"] == -1000
    # the ledger agrees with the balance: 25 - 5 refunded - 10 used
    assert hear.db.balance(a["id"])["balance_usd"] == sum(x["amount_usd"] for x in r["transactions"]
                                                          if x["congregation"]["id"] == a["id"]) == 10


def test_transactions_filter_and_page(client, hear):
    a, _ = make_cong(hear)
    b, _ = make_cong(hear, "Thiès Branch", "b@example.org")
    for i in range(5):
        hear.db.add_credit(a["id"], f"pi_{i}", 500)
    hear.db.add_credit(b["id"], "pi_b", 500)
    own = meeting(hear, b, "m1", 10, billing="own_key")
    rows = client.get(f"/api/admin/transactions{K}&congregation={a['id']}&limit=2").json()
    assert [t["payment_id"] for t in rows["transactions"]] == ["pi_4", "pi_3"]
    seen = [t["payment_id"] for t in rows["transactions"]]
    while rows["next_before"]:
        rows = client.get("/api/admin/transactions", params={"key": "boss", "congregation": a["id"], "limit": 2,
                                                             "before": rows["next_before"]}).json()
        seen += [t["payment_id"] for t in rows["transactions"]]
    assert seen == ["pi_4", "pi_3", "pi_2", "pi_1", "pi_0"]
    u, = client.get(f"/api/admin/transactions{K}&kind=usage").json()["transactions"]
    assert u["session_id"] == own and u["billing"] == "own_key" and u["cost_usd"] == 0 and u["minutes"] == 10
    assert client.get(f"/api/admin/transactions{K}&kind=gift").status_code == 400
    assert client.get(f"/api/admin/transactions{K}&congregation=999").status_code == 404
    assert client.get(f"/api/admin/transactions{K}&before=junk").status_code == 400


def test_the_free_hour_is_spent_by_the_first_meetings(client, hear):
    a, _ = make_cong(hear)
    meeting(hear, a, "m1", 40)
    meeting(hear, a, "m2", 40, first_seq=1000)
    meeting(hear, a, "m3", 10, billing="own_key", first_seq=2000)       # metered, never billed, doesn't use the hour
    u = {t["code"]: t for t in client.get(f"/api/admin/transactions{K}&kind=usage").json()["transactions"]}
    assert (u["m1"]["free_minutes"], u["m1"]["paid_minutes"]) == (40, 0)
    assert (u["m2"]["free_minutes"], u["m2"]["paid_minutes"], u["m2"]["cost_usd"]) == (20, 20, 1.67)
    assert u["m3"]["cost_usd"] == 0 and u["m3"]["free_minutes"] == 0


# ------------------------------------------------------------------ stats

def test_stats(client, hear):
    a, ta = make_cong(hear)
    b, _ = make_cong(hear, "Thiès Branch", "b@example.org")
    c, _ = make_cong(hear, "Old Ward", "c@example.org")
    hear.db._x("UPDATE congregations SET billing = 'own_key', created_at = ? WHERE id = ?", (time.time() - 40 * 86400, c["id"]))
    hear.db.add_credit(a["id"], "pi_a", 2500)
    hear.db.add_credit(b["id"], "by-hand", 1000)
    hear.db.refund_credit("pi_a", 500)
    meeting(hear, a, "m1", 90)                           # 60 free + 30 paid ($2.50)
    meeting(hear, c, "m2", 30, billing="own_key")
    old = hear.db.start_session(b["id"], ["fr"], code="m3")
    hear.db._x("UPDATE sessions SET started_at = ? WHERE id = ?", (time.time() - 10 * 86400, old))
    dev = hear.db.device_by_token(ta)
    hear.db.touch_device(dev["id"], "1.4.0")
    s = client.get("/api/admin/stats" + K).json()
    assert s["congregations"]["by_billing"] == {"credits": 2, "own_key": 1} and s["congregations"]["total"] == 3
    assert s["last_7d"]["signups"] == 2 and s["last_30d"]["signups"] == 2
    assert s["last_7d"]["active_congregations"] == 2 and s["last_7d"]["meetings"] == 2
    assert s["last_30d"]["active_congregations"] == 3 and s["last_30d"]["meetings"] == 3
    assert s["all_time"]["language_hours"] == {"total": 2.0, "paid": 0.5, "free": 1.0, "own_key": 0.5, "paid_usd": 2.5}
    assert s["credit"]["sold_usd"] == 25 and s["credit"]["manual_usd"] == 10 and s["credit"]["refunded_usd"] == 5
    assert s["credit"]["outstanding_usd"] == 20 - 2.5 + 10 and s["credit"]["overdrawn_usd"] == 0
    i = s["installs"]
    assert i["devices"] == 3 and i["active_7d"] == 1 and i["never_connected"] == 2
    assert i["by_version"] == {"1.4.0": {"devices": 1, "active_7d": 1, "active_30d": 1},
                               "unknown": {"devices": 2, "active_7d": 0, "active_30d": 0}}


async def test_the_mac_reports_its_app_version(server, hear):
    cong, token = make_cong(hear)
    ws = await engine(server, token)
    from app import main
    await ws.send(json.dumps({"type": "hello", "name": "Test Mac", "languages": ["fr"], "app_version": "1.5.2",
                              "stream": {"sample_rate": main.S.settings.stream.sample_rate,
                                         "bitrate_kbps": main.S.settings.stream.bitrate_kbps}}))
    for _ in range(50):
        if hear.db.devices(cong["id"])[0]["app_version"]:
            break
        await asyncio.sleep(0.05)
    await ws.close()
    d, = hear.db.devices(cong["id"])
    assert d["app_version"] == "1.5.2" and d["last_seen"]
    hear.db.touch_device(d["id"])                       # a hello without a version keeps the one it knows
    assert hear.db.devices(cong["id"])[0]["app_version"] == "1.5.2"


def test_engine_hello_carries_the_app_version():
    import inspect

    from app import engine as eng
    assert '"app_version": os.environ.get("HEARCHURCH_APP_VERSION"' in inspect.getsource(eng)


# ------------------------------------------------------------------ refunds

def test_refund_defaults_to_the_unused_part_and_the_webhook_changes_nothing_after(client, hear, keys, fake_stripe):
    cong, token = make_cong(hear)
    paid = completed(cong["id"])                         # $25 top-up, pi_test_1
    assert hook(client, paid, signed(paid)).json()["credited"] is True
    meeting(hear, cong, "m1", 120)                       # 60 free + 60 paid: $5 used
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_test_1"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["refund"] == {"id": "re_test_1", "status": "succeeded", "cents": 2000, "usd": 20}
    assert j["unused_cents_before"] == 2000 and "warning" not in j and j["balance"]["balance_usd"] == 0
    (params, options), = fake_stripe.created
    assert params["payment_intent"] == "pi_test_1" and params["amount"] == 2000
    assert options == {"idempotency_key": "hc-refund-pi_test_1-0-2000"}
    # Stripe's charge.refunded arrives later with the charge's refunded total: nothing changes
    p = refunded(refunded_cents=2000)
    assert hook(client, p, signed(p)).json()["refunded"] is False
    assert hear.db.balance(cong["id"])["balance_usd"] == 0
    rows = client.get(f"/api/admin/transactions{K}&kind=refund").json()["transactions"]
    assert len(rows) == 1 and rows[0]["source"] == "admin" and rows[0]["stripe_refund_id"] == "re_test_1"
    # nothing unused is left: a refund without an amount is refused, Stripe isn't asked
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_test_1"})
    assert r.status_code == 409 and r.json()["error"] == "nothing_unused" and len(fake_stripe.created) == 1


def test_refund_of_used_credit_is_allowed_with_a_warning_never_beyond_the_top_up(client, hear, keys, fake_stripe):
    cong, _ = make_cong(hear)
    hear.db.add_credit(cong["id"], "pi_x", 1000)
    meeting(hear, cong, "m1", 90)                        # $2.50 of the $10 used
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_x", "cents": 1001})
    assert r.status_code == 409 and r.json()["error"] == "too_much" and r.json()["refundable_cents"] == 1000
    assert r.json()["unused_cents"] == 750 and not fake_stripe.created
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_x", "cents": 1000}).json()
    assert r["refund"]["cents"] == 1000 and "already used" in r["warning"] and r["balance"]["balance_usd"] == -2.5
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_x", "cents": 1})
    assert r.status_code == 409 and r.json()["error"] == "fully_refunded"


def test_partial_refunds_add_up(client, hear, keys, fake_stripe):
    cong, _ = make_cong(hear)
    hear.db.add_credit(cong["id"], "pi_x", 2500)
    assert client.post("/api/admin/refunds" + K, json={"payment_id": "pi_x", "cents": 500}).status_code == 200
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_x"}).json()
    assert r["refund"]["cents"] == 2000 and r["credit"]["refunded_cents"] == 2500
    assert [o["idempotency_key"] for _, o in fake_stripe.created] == ["hc-refund-pi_x-0-500", "hc-refund-pi_x-500-2000"]


def test_refund_errors(client, hear, keys, fake_stripe, monkeypatch):
    cong, _ = make_cong(hear)
    hear.db.add_credit(cong["id"], "pi_x", 1000)
    hear.db.add_credit(cong["id"], "by-hand", 1000)
    assert client.post("/api/admin/refunds" + K, json={"payment_id": "pi_nope"}).status_code == 404
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "by-hand"})
    assert r.status_code == 409 and r.json()["error"] == "not_stripe"
    assert client.post("/api/admin/refunds" + K, json={"payment_id": "pi_x", "cents": 0}).status_code == 422
    fake_stripe.raises = stripe.InvalidRequestError("Charge ch_1 has already been refunded.", None,
                                                    code="charge_already_refunded", http_status=400)
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_x"})
    assert r.status_code == 409 and r.json() == {"error": "stripe", "message": "Charge ch_1 has already been refunded.",
                                                 "stripe_code": "charge_already_refunded", "stripe_status": 400}
    fake_stripe.raises = stripe.APIConnectionError("network down")
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_x"})
    assert r.status_code == 502 and r.json()["error"] == "stripe"
    assert hear.db.credit_by_payment("pi_x")["refunded_cents"] == 0       # a failed refund leaves the ledger alone
    fake_stripe.raises = None
    fake_stripe.status = "failed"
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_x"})
    assert r.status_code == 409 and hear.db.credit_by_payment("pi_x")["refunded_cents"] == 0
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_x"})
    assert r.status_code == 503 and r.json()["error"] == "not_configured"


# ------------------------------------------------------------------ own-key codes

def test_a_voided_code_stops_working(client, hear, caplog):
    caplog.set_level("DEBUG")
    code = client.post("/api/admin/own-key-codes" + K, json={"label": "Dakar Ward"}).json()["code"]
    other = client.post("/api/admin/own-key-codes" + K, json={"label": "Thiès Branch"}).json()["code"]
    listed = client.get("/api/admin/own-key-codes" + K).json()["codes"]
    assert all(c["status"] == "unused" for c in listed) and code not in json.dumps(listed)   # never shown again
    kid = next(c["id"] for c in listed if c["label"] == "Dakar Ward")
    r = client.post(f"/api/admin/own-key-codes/{kid}/void{K}")
    assert r.status_code == 200 and r.json()["code"]["status"] == "void" and r.json()["code"]["voided_at"]
    assert client.post(f"/api/admin/own-key-codes/{kid}/void{K}").status_code == 200       # again: fine
    assert client.post("/api/hc/own-key-code/check", json={"code": code}).status_code == 403
    cong, token = make_cong(hear)
    assert client.post("/api/hc/own-key-code", json={"code": code},
                       headers={"Authorization": f"Bearer {token}"}).status_code == 403
    assert hear.db.congregation(cong["id"])["billing"] == "credits"
    # a used code can't be voided; the list says who used it
    assert client.post("/api/hc/own-key-code", json={"code": other},
                       headers={"Authorization": f"Bearer {token}"}).status_code == 200
    oid = next(c["id"] for c in listed if c["label"] == "Thiès Branch")
    r = client.post(f"/api/admin/own-key-codes/{oid}/void{K}")
    assert r.status_code == 409 and r.json()["error"] == "used"
    used = {c["label"]: c for c in client.get("/api/admin/own-key-codes" + K).json()["codes"]}
    assert used["Thiès Branch"]["status"] == "used" and used["Thiès Branch"]["used_by"] == "dakar-ward"
    assert used["Thiès Branch"]["used_by_name"] == "Dakar Ward" and used["Dakar Ward"]["status"] == "void"
    assert client.post(f"/api/admin/own-key-codes/999/void{K}").status_code == 404
    assert code not in caplog.text and other not in caplog.text          # codes never reach the logs


# ------------------------------------------------------------------ one congregation

def test_one_congregation(client, hear):
    cong, token = make_cong(hear)
    hear.db.touch_device(hear.db.device_by_token(token)["id"], "1.4.0")
    hear.db.add_credit(cong["id"], "pi_a", 1000)
    for i in range(3):
        meeting(hear, cong, f"m{i}", 30, first_seq=i * 1000)
    j = client.get(f"/api/admin/congregations/{cong['id']}{K}&meetings=2").json()
    assert j["congregation"]["slug"] == "dakar-ward" and j["congregation"]["billing"] == "credits"
    assert j["billing"]["balance"]["free_left_min"] == 0 and j["billing"]["balance_usd"] == 7.5
    d, = j["devices"]
    assert d["app_version"] == "1.4.0" and d["last_seen"] and "token_hash" not in d
    assert [m["code"] for m in j["meetings"]] == ["m2", "m1"] and j["meetings_total"] == 3
    assert j["meetings"][0]["paid_minutes"] == 30 and j["meetings"][0]["cost_usd"] == 2.5
    assert [t["kind"] for t in j["transactions"]][-1] == "top_up" and len(j["transactions"]) == 4
    assert j["own_key_codes"] == [] and j["live"] is False
    assert client.get(f"/api/admin/congregations/999{K}").status_code == 404


def test_old_databases_get_the_new_columns(tmp_path):
    import sqlite3

    from app.hear.db import Database
    path = tmp_path / "old.db"
    db = Database(path)
    db._db.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE devices DROP COLUMN app_version")
    con.execute("ALTER TABLE own_key_codes DROP COLUMN voided_at")
    con.execute("DROP TABLE refunds")
    con.commit()
    con.close()
    db = Database(path)
    assert db.all_devices() == [] and db.own_key_codes() == [] and db.refunds() == []


def test_the_operator_password_works_in_a_header(client):
    """Siteboard calls from a server: X-Operator-Key instead of ?key= keeps the password out of URLs."""
    from app import main
    want = dict(main._passwords()).get(main.OPERATOR)
    assert want, "the test hub has an operator password"
    r = client.get("/api/admin/stats", headers={"X-Operator-Key": want, "X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 200
    assert client.get("/api/admin/stats", headers={"X-Operator-Key": "wrong", "X-Forwarded-For": "1.2.3.4"}).status_code == 403
