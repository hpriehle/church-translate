"""The free trial (Nov 16): every new congregation gets 14 days from its first metered meeting, up to 20
language-hours, whichever ends first. Trial use is metered but free and leaves the month's free hour alone; older
congregations and own-key ones never get it."""
import sqlite3
import time

import pytest

from app.hear import api as hear_api
from app.hear.db import Database

from test_hear import SIGNUP, client, hear, make_cong  # noqa: F401  (fixtures)
from test_metering import speak
from test_stripe import auth
from test_cards_codes import fake, mail, new_code, run, save_card  # noqa: F401  (fixtures)
from test_stripe import keys  # noqa: F401
from app.hear import payments

pytestmark = pytest.mark.free_trial
DAY = 86400


def new_db():
    db = Database(":memory:")
    c = db.create_congregation(church="LDS", name="Dakar Ward", unit_type="ward", email="a@x.org")
    return db, c


def meeting(db, cid, code, minutes, langs=("fr",), at=None, billing="credits", first_seq=1):
    """A metered meeting that started at `at` (default: now)."""
    sid = db.start_session(cid, list(langs), code=code, billing=billing)
    if at is not None:
        db._x("UPDATE sessions SET started_at = ? WHERE id = ?", (at, sid))
    speak(db, cid, code, minutes, langs, first_seq)
    db.end_session(sid)
    return sid


def test_a_new_congregation_has_the_trial_before_any_meeting():
    db, c = new_db()
    assert c["free_trial"] is True
    t = db.balance(c["id"])["trial"]
    assert t == {"active": True, "days": 14, "cap_hours": 20, "started_at": None, "ends_at": None, "used_min": 0,
                 "days_left": 14, "hours_left": 20}


def test_existing_congregations_get_no_trial(tmp_path):
    """A database from before the trial: its congregations keep what they had (free hour, credit)."""
    path = tmp_path / "old.db"
    db = Database(path)
    c = db.create_congregation(church="LDS", name="Riga 2", unit_type="branch", email="r@x.org")
    db._db.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE congregations DROP COLUMN free_trial")
    con.commit()
    con.close()
    db = Database(path)                       # the migration adds the column, off for every row there
    old = db.congregation(c["id"])
    assert old["free_trial"] is False and db.balance(c["id"])["trial"] is None
    meeting(db, c["id"], "m1", 30)
    assert db.balance(c["id"])["free_left_min"] == 30          # the free hour is used as before
    new = db.create_congregation(church="LDS", name="Riga 3", unit_type="branch", email="s@x.org")
    assert new["free_trial"] is True                            # made after the deploy: it gets one


def test_the_clock_starts_at_the_first_metered_meeting():
    db, c = new_db()
    first = time.time() - 3 * DAY
    db._x("UPDATE congregations SET created_at = ? WHERE id = ?", (first - 30 * DAY, c["id"]))   # signed up long ago
    db.start_session(c["id"], ["fr"], code="empty", billing="credits")    # a meeting with no speech doesn't count
    assert db.balance(c["id"])["trial"]["started_at"] is None
    meeting(db, c["id"], "m1", 30, at=first)
    t = db.balance(c["id"])["trial"]
    assert t["active"] and t["started_at"] == first and t["ends_at"] == first + 14 * DAY
    assert t["days_left"] == pytest.approx(11, abs=0.01) and t["hours_left"] == 19.5 and t["used_min"] == 30


def test_trial_use_is_free_and_leaves_the_free_hour_alone():
    db, c = new_db()
    meeting(db, c["id"], "m1", 90, ("fr", "es"))              # 3 language-hours
    b = db.balance(c["id"])
    assert b["free_left_min"] == 60 and b["balance_min"] == 0 and b["balance_usd"] == 0 and b["can_start"]
    assert b["used_min"] == 180 and b["month_used_min"] == 0 and b["trial"]["hours_left"] == 17
    s = db._walk(c["id"])["sessions"]
    assert list(s.values()) == [{"secs": 10800, "trial": 10800, "free": 0, "paid": 0, "cost_cents": 0}]


def test_the_trial_ends_after_14_days_then_the_free_hour_counts():
    db, c = new_db()
    now = time.time()
    first = now - 15 * DAY
    meeting(db, c["id"], "m1", 60, at=first)                  # in the trial
    meeting(db, c["id"], "m2", 60, at=first + 13.9 * DAY, first_seq=1000)   # still in it (started before day 14)
    b = db.balance(c["id"])
    assert b["trial"]["active"] is False and b["trial"]["used_min"] == 120
    assert b["trial"]["days_left"] == 0 and b["trial"]["hours_left"] == 0
    assert b["balance_min"] == 0 and b["can_start"]           # this month's free hour is still whole
    meeting(db, c["id"], "m3", 40, first_seq=2000)            # after the trial: the month's free hour, then nothing
    s = db._walk(c["id"])["sessions"]
    m3 = s[max(s)]
    assert m3["trial"] == 0 and m3["free"] == 40 * 60 and m3["paid"] == 0
    b = db.balance(c["id"])
    assert b["free_left_min"] == 20 and b["can_start"]
    meeting(db, c["id"], "m4", 30, first_seq=3000)            # 20 free, 10 overdrawn
    b = db.balance(c["id"])
    assert b["balance_min"] == -10 and not b["can_start"] and not db.can_start(db.congregation(c["id"]))


def test_a_meeting_on_day_15_is_billed_normally():
    db, c = new_db()
    first = time.time() - 20 * DAY
    meeting(db, c["id"], "m1", 10, at=first)
    meeting(db, c["id"], "m2", 10, at=first + 14 * DAY + 60, first_seq=1000)
    s = db._walk(c["id"])["sessions"]
    assert [v["trial"] for v in s.values()] == [600, 0]


def test_the_trial_ends_at_20_language_hours_and_the_overflow_is_billed():
    db, c = new_db()
    meeting(db, c["id"], "m1", 9 * 60, ("fr", "es"))          # 18 language-hours
    b = db.balance(c["id"])
    assert b["trial"]["active"] and b["trial"]["hours_left"] == 2
    # one meeting runs across the cap: 3 language-hours, 2 in the trial; the third goes to the free hour
    meeting(db, c["id"], "m2", 90, ("fr", "es"), first_seq=10000)
    s = db._walk(c["id"])["sessions"]
    m2 = s[max(s)]
    assert m2["trial"] == 2 * 3600 and m2["free"] == 3600 and m2["paid"] == 0
    b = db.balance(c["id"])
    assert b["trial"]["active"] is False and b["trial"]["used_min"] == 20 * 60
    assert b["free_left_min"] == 0 and b["balance_min"] == 0 and not b["can_start"]
    # a bigger overflow than the free hour is overdrawn like any meeting that runs past the balance
    db2, c2 = new_db()
    meeting(db2, c2["id"], "m1", 11 * 60, ("fr", "es"))      # 22 language-hours: 20 trial, 1 free, 1 overdrawn
    b = db2.balance(c2["id"])
    assert b["trial"]["active"] is False and b["free_left_min"] == 0 and b["balance_min"] == -60
    assert b["balance_usd"] == -5 and not b["can_start"]


def test_the_trial_lets_a_meeting_start_with_nothing_else_left():
    db, c = new_db()
    db._x("UPDATE congregations SET free_trial = 0 WHERE id = ?", (c["id"],))
    meeting(db, c["id"], "m1", 70)                            # the free hour gone, 10 min overdrawn
    assert not db.can_start(db.congregation(c["id"]))
    db.set_free_trial(c["id"], True)                          # same use, as trial: free, so it may start
    cong = db.congregation(c["id"])
    assert db.can_start(cong) and db.balance(c["id"])["balance_min"] == 0


def test_own_key_congregations_never_get_a_trial():
    db, c = new_db()
    db.set_billing(c["id"], "own_key")
    cong = db.congregation(c["id"])
    assert cong["free_trial"] is False and db.balance(c["id"])["trial"] is None
    meeting(db, c["id"], "m1", 30, billing="own_key")
    db.set_billing(c["id"], "credits")                        # locked again later: still no trial
    assert db.balance(c["id"])["trial"] is None
    meeting(db, c["id"], "m2", 30, first_seq=1000)
    assert db.balance(c["id"])["free_left_min"] == 30


def test_own_key_code_at_sign_up_means_no_trial(client, hear):
    hear.verify_email = False
    own = new_code(client, type="own_key")
    r = client.post("/api/hc/signup", json={**SIGNUP, "code": own["code"]})
    assert r.json()["billing"] == "own_key"
    cong = hear.db.by_slug(r.json()["slug"])
    assert cong["free_trial"] is False
    b = client.get("/api/hc/billing", headers=auth(r.json()["device_token"])).json()
    assert b["free_trial"] is None and b["balance"]["trial"] is None


def test_a_sign_up_gets_the_trial_and_the_billing_answer_shows_it(client, hear):
    hear.verify_email = False
    r = client.post("/api/hc/signup", json=SIGNUP)
    assert r.status_code == 200, r.text
    token = r.json()["device_token"]
    cong = hear.db.by_slug(r.json()["slug"])
    assert cong["free_trial"] is True
    b = client.get("/api/hc/billing", headers=auth(token)).json()
    assert b["free_trial"] == {"days_left": 14, "hours_left": 20, "ends_at": None,
                               "text": "Free trial: 14 days and 20 hours left"}
    assert b["message"] == "Free trial: 14 days and 20 hours left" and not b["out_of_credit"]
    meeting(hear.db, cong["id"], "m1", 90, ("fr", "es"), at=time.time() - 2.5 * DAY)
    b = client.get("/api/hc/billing", headers=auth(token)).json()
    assert b["free_trial"]["text"] == "Free trial: 12 days and 17 hours left"
    assert b["free_hours_left"] == 1                          # the free hour waits for the end of the trial
    u = client.get("/api/hc/usage", headers=auth(token)).json()["meetings"][0]
    assert u["trial_minutes"] == 180 and u["free_minutes"] == 0 and u["cost_usd"] == 0
    # ended: today's display, no trial in the answer
    hear.db._x("UPDATE sessions SET started_at = ? WHERE congregation_id = ?", (time.time() - 15 * DAY, cong["id"]))
    b = client.get("/api/hc/billing", headers=auth(token)).json()
    assert b["free_trial"] is None and b["message"] == "1 free hour left this month · 0 paid hours left."
    assert b["balance"]["trial"]["active"] is False


def test_trial_text():
    t = lambda d, h: hear_api.trial_text({"days_left": d, "hours_left": h})  # noqa: E731
    assert t(14, 20) == "Free trial: 14 days and 20 hours left"
    assert t(0.2, 1) == "Free trial: 1 day and 1 hour left"
    assert t(3.01, 12.49) == "Free trial: 4 days and 12.4 hours left"
    assert t(5, 0.04) == "Free trial: 5 days and 0 hours left"


def test_siteboard_sees_the_trial_per_congregation(client, hear):
    K = "?key=boss"
    new, _ = make_cong(hear)
    old, _ = make_cong(hear, name="Riga 2", email="r@x.org")
    hear.db.set_free_trial(old["id"], False)
    meeting(hear.db, new["id"], "m1", 60)
    rows = {c["id"]: c for c in client.get("/api/admin/congregations" + K).json()["congregations"]}
    assert rows[old["id"]]["free_trial"] is None and rows[old["id"]]["balance"]["trial"] is None
    t = rows[new["id"]]["free_trial"]
    assert t["status"] == "running" and t["used_hours"] == 1 and t["hours_left"] == 19
    assert "balance" in rows[new["id"]] and "billing" in rows[new["id"]]   # the old fields are all still there
    j = client.get(f"/api/admin/congregations/{new['id']}{K}").json()
    assert j["congregation"]["free_trial"]["status"] == "running"
    assert j["meetings"][0]["trial_minutes"] == 60 and j["meetings"][0]["cost_usd"] == 0
    hear.db.set_free_trial(old["id"], True)
    j = client.get(f"/api/admin/congregations/{old['id']}{K}").json()
    assert j["congregation"]["free_trial"]["status"] == "not_started"


def test_auto_top_up_never_charges_during_the_trial(client, hear, fake, mail):
    cong, token = make_cong(hear)
    client.post("/api/hc/card/setup", headers=auth(token))
    save_card(client, hear, fake, cong)
    assert client.post("/api/hc/auto-topup", json={"enabled": True, "usd": 20}, headers=auth(token)).status_code == 200
    meeting(hear.db, cong["id"], "m1", 120)                   # paid balance 0, but the trial runs
    assert run(payments.auto_topup(cong["id"])) == "skipped: free trial" and fake.of("pi") == []
    hear.db._x("UPDATE sessions SET started_at = ? WHERE congregation_id = ?", (time.time() - 15 * DAY, cong["id"]))
    assert run(payments.auto_topup(cong["id"])) == "charged"  # ended: it works as before
