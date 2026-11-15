"""Pricing from Nov 14: one free language-hour every calendar month (UTC, no carry-over) and credit bundles
($20 = 4 h, $50 = 11 h, $100 = 25 h, $250 = 70 h) stored as the minutes bought. Paid time comes off the oldest top-up
first; a refund of unused credit is pro rata at that top-up's own rate. Top-ups from before the bundles keep $5/h."""
import calendar
import time

import pytest

from app.hear.db import BUNDLES, Database, credit_seconds, month_of, next_month_start

from test_hear import client, engine, hear, make_cong  # noqa: F401  (fixtures)
from test_admin_api import fake_stripe  # noqa: F401  (fixtures)
from test_stripe import keys  # noqa: F401  (fixtures)

K = "?key=boss"


def utc(y, m, d=2, h=12):
    return float(calendar.timegm((y, m, d, h, 0, 0)))


def cong_db():
    db = Database(":memory:")
    c = db.create_congregation(church="LDS", name="Riga Branch", unit_type="branch", email="a@x.org")
    return db, db.congregation(c["id"])


def meeting(db, cid, code, minutes, at):
    """A credit-billed meeting started at `at` with `minutes` language-minutes of speech."""
    sid = db.start_session(cid, ["fr"], code=code, billing="credits")
    db._x("UPDATE sessions SET started_at = ? WHERE id = ?", (at, sid))
    db.record_speech(cid, code, [{"seq": i, "s": 60.0, "langs": ["fr"]} for i in range(int(minutes))])
    db.end_session(sid)
    return sid


def test_bundle_hours_and_legacy_rate():
    assert [(c, h) for c, h in BUNDLES] == [(2000, 4), (5000, 11), (10000, 25), (25000, 70)]
    assert credit_seconds(2000) == 4 * 3600 and credit_seconds(25000) == 70 * 3600
    assert credit_seconds(2500) == 5 * 3600 and credit_seconds(1000) == 2 * 3600   # not a bundle: $5 per hour
    db, c = cong_db()
    credit, _ = db.add_credit(c["id"], "pi_1", 5000)
    assert credit["seconds"] == 11 * 3600
    credit, _ = db.add_credit(c["id"], "goodwill", 100, seconds=3 * 3600)   # by hand: any time
    assert credit["seconds"] == 3 * 3600


def test_months_are_utc_and_reset_on_the_first():
    assert month_of(utc(2026, 11, 30, 23) + 3599) == "2026-11" and month_of(utc(2026, 12, 1, 0)) == "2026-12"
    assert next_month_start(utc(2026, 11, 15)) == utc(2026, 12, 1, 0)
    assert next_month_start(utc(2026, 12, 31, 23)) == utc(2027, 1, 1, 0)


def test_one_free_hour_every_month_no_carry_over():
    db, c = cong_db()
    cid = c["id"]
    now = utc(2026, 11, 20)
    assert db.balance(cid, now)["free_left_min"] == 60 and db.balance(cid, now)["can_start"]
    meeting(db, cid, "sep", 20, utc(2026, 9, 6))           # September: 20 of its free hour; 40 unused, lost
    meeting(db, cid, "oct", 60, utc(2026, 10, 4))          # October: exactly its free hour
    b = db.balance(cid, now)
    assert b["free_left_min"] == 60 and b["balance_min"] == 0 and b["used_min"] == 80 and b["month_used_min"] == 0
    assert b["free_month"] == "2026-11" and b["free_resets_at"] == utc(2026, 12, 1, 0)
    meeting(db, cid, "nov1", 45, utc(2026, 11, 1, 0))      # November: 45 + 30, so 15 beyond the free hour
    meeting(db, cid, "nov2", 30, utc(2026, 11, 8))
    b = db.balance(cid, now)
    assert b["free_left_min"] == 0 and b["balance_min"] == -15 and b["balance_usd"] == -1.25 and not b["can_start"]
    # December: a new free hour, though the balance is still 15 minutes overdrawn
    b = db.balance(cid, utc(2026, 12, 1, 0))
    assert b["free_left_min"] == 60 and b["left_min"] == 45 and b["can_start"]
    w = db._walk(cid)["sessions"]
    assert [(round(v["free"] / 60), round(v["paid"] / 60)) for v in w.values()] == [(20, 0), (60, 0), (45, 0), (15, 15)]


def test_paid_time_comes_off_the_oldest_top_up_first_at_its_own_rate():
    db, c = cong_db()
    cid = c["id"]
    now = utc(2026, 11, 20)
    db.add_credit(cid, "pi_old", 2500)                     # before the bundles: 5 h at $5/h
    db.add_credit(cid, "pi_50", 5000)                      # 11 h at $4.545/h
    meeting(db, cid, "m1", 60 + 6 * 60, now - 3600)        # free hour, then 6 h: 5 h old + 1 h of the bundle
    b = db.balance(cid, now)
    assert b["balance_min"] == 600 and b["bought_min"] == 16 * 60 and b["bought_usd"] == 75
    assert b["balance_usd"] == pytest.approx(10 * 50 / 11, abs=0.01)    # 10 h left of the $50 bundle
    cost = db._walk(cid)["sessions"]
    (m,) = cost.values()
    assert m["cost_cents"] == pytest.approx(2500 + 5000 / 11)
    assert db.unused_cents(db.credit_by_payment("pi_old")) == 0
    assert db.unused_cents(db.credit_by_payment("pi_50")) == round(10 * 5000 / 11)


def test_a_refund_takes_time_back_pro_rata_at_the_bundles_rate():
    db, c = cong_db()
    cid = c["id"]
    db.add_credit(cid, "pi_250", 25000)                    # 70 h
    db.refund_credit("pi_250", 12500)                      # half the money back: half the hours, 35 h
    b = db.balance(cid)
    assert b["balance_min"] == 35 * 60 and b["balance_usd"] == 125 and b["bought_usd"] == 125


def test_admin_refund_default_is_the_unused_part_of_that_bundle(client, hear, keys, fake_stripe):
    cong, _ = make_cong(hear)
    cid = cong["id"]
    hear.db.add_credit(cid, "pi_20", 2000)                 # 4 h, used first
    hear.db.add_credit(cid, "pi_50", 5000)                 # 11 h
    meeting(hear.db, cid, "m1", 60 + 5 * 60, utc(*time.gmtime()[:2], 1, 0) + 60)   # this month: 1 h + 5 h
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_20"})
    assert r.status_code == 409 and r.json()["error"] == "nothing_unused"
    r = client.post("/api/admin/refunds" + K, json={"payment_id": "pi_50"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["unused_cents_before"] == round(10 * 5000 / 11) == j["refund"]["cents"] and "warning" not in j
    assert j["balance"]["balance_min"] < 0.1 and j["balance"]["balance_usd"] == 0   # whole cents: seconds stay


def test_admin_credit_by_hand_may_say_the_minutes(client, hear):
    cong, _ = make_cong(hear)
    r = client.post(f"/api/admin/congregations/{cong['id']}/credits" + K,
                    json={"payment_id": "goodwill-dec", "cents": 1, "minutes": 120})
    assert r.status_code == 200 and r.json()["credit"]["seconds"] == 7200
    r = client.post(f"/api/admin/congregations/{cong['id']}/credits" + K, json={"payment_id": "pi_x", "cents": 10000})
    assert r.json()["credit"]["seconds"] == 25 * 3600
    rows = client.get(f"/api/admin/transactions{K}&kind=top_up").json()["transactions"]
    assert rows[0]["bundle"] == 100 and rows[0]["minutes"] == 1500
