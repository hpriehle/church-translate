"""Stripe refunds (charge.refunded) taking the refunded amount back off the top-up on the ledger, once."""
import json

import pytest

from app.hear.db import Database

from test_hear import client, hear, make_cong  # noqa: F401  (fixtures)
from test_stripe import completed, hook, keys, signed  # noqa: F401  (fixtures)


def auth(token):
    return {"Authorization": f"Bearer {token}"}

def test_refunds_come_off_the_top_up_once():
    db = Database(":memory:")
    c = db.create_congregation(church="LDS", name="Dakar Ward", unit_type="ward", email="a@x.org")
    db.add_credit(c["id"], "pi_1", 2500)
    assert db.balance(c["id"])["balance_usd"] == 25
    credit, changed = db.refund_credit("pi_1", 1000)              # $10 of $25 back
    assert changed and credit["refunded_cents"] == 1000 and credit["refunded_at"]
    b = db.balance(c["id"])
    assert b["balance_usd"] == 15 and b["bought_usd"] == 15 and b["left_min"] == 60 + 180
    assert db.refund_credit("pi_1", 1000)[1] is False             # the same event again
    assert db.refund_credit("pi_1", 500)[1] is False              # an older event, late
    assert db.refund_credit("pi_1", 9999)[0]["refunded_cents"] == 2500   # never more than was paid
    assert db.balance(c["id"])["balance_usd"] == 0
    with pytest.raises(KeyError):
        db.refund_credit("pi_unknown", 100)
    with pytest.raises(ValueError):
        db.refund_credit("pi_1", -1)


def test_refunding_used_credit_leaves_the_balance_negative():
    db = Database(":memory:")
    c = db.create_congregation(church="LDS", name="Dakar Ward", unit_type="ward", email="a@x.org")
    db.add_credit(c["id"], "pi_1", 1000)
    db.start_session(c["id"], ["fr"], code="s1")
    db.record_speech(c["id"], "s1", [{"seq": i, "s": 60.0, "langs": ["fr"]} for i in range(90)])
    db.refund_credit("pi_1", 1000)
    b = db.balance(c["id"])
    assert b["balance_usd"] == -2.5 and not b["can_start"]


def test_old_databases_get_the_refund_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    db = Database(path)
    c = db.create_congregation(church="LDS", name="Dakar Ward", unit_type="ward", email="a@x.org")
    db.add_credit(c["id"], "pi_1", 500)
    db._db.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE credits DROP COLUMN refunded_at")
    con.execute("ALTER TABLE credits DROP COLUMN refunded_cents")
    con.commit()
    con.close()
    db = Database(path)
    assert db.credits(c["id"])[0]["refunded_cents"] == 0 and db.balance(c["id"])["balance_usd"] == 5


def refunded(pi="pi_test_1", amount=2500, refunded_cents=1000, currency="usd", evt="evt_r1"):
    obj = {"id": "ch_test_1", "object": "charge", "amount": amount, "amount_refunded": refunded_cents,
           "currency": currency, "payment_intent": pi, "refunded": refunded_cents == amount}
    return json.dumps({"id": evt, "object": "event", "type": "charge.refunded", "data": {"object": obj}})


def test_the_webhook_takes_a_refund_back_off_the_ledger(client, hear, keys):
    cong, token = make_cong(hear)
    paid = completed(cong["id"])
    assert hook(client, paid, signed(paid)).json()["credited"] is True
    p = refunded()
    r = hook(client, p, signed(p))
    assert r.status_code == 200 and r.json()["refunded"] is True and r.json()["refunded_cents"] == 1000
    assert hook(client, p, signed(p)).json()["refunded"] is False  # resent: once
    assert client.get("/api/hc/billing", headers=auth(token)).json()["balance_usd"] == 15
    full = refunded(refunded_cents=2500, evt="evt_r2")              # the rest, later: the charge's total
    assert hook(client, full, signed(full)).json()["refunded_cents"] == 2500
    assert client.get("/api/hc/billing", headers=auth(token)).json()["balance_usd"] == 0
    credit, = hear.db.credits(cong["id"])
    assert credit["payment_id"] == "pi_test_1" and credit["cents"] == 2500 and credit["refunded_cents"] == 2500


def test_refunds_that_arent_top_ups_are_acknowledged_and_ignored(client, hear, keys):
    cong, _ = make_cong(hear)
    paid = completed(cong["id"])
    hook(client, paid, signed(paid))
    for p in (refunded(pi="pi_someone_else"), refunded(pi=None), refunded(currency="eur")):
        r = hook(client, p, signed(p))
        assert r.status_code == 200 and r.json()["refunded"] is False
    p = refunded()
    assert hook(client, p, {}).status_code == 400                   # unsigned: refused
    assert hear.db.credits(cong["id"])[0]["refunded_cents"] == 0
