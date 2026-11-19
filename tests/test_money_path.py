"""The money path a new church walks (docs/money-path.md), the edges the other billing tests don't pin: the trial
ending mid-meeting (never cut off), not restarted by a new Mac or a repeated sign-up, a webhook arriving out of order,
buying at 0 hours, auto top-up once the trial ends by hours, the free hour across a month boundary during the trial,
and metering only the languages listened to. Stripe and Anthropic are fakes; nothing goes to the network."""
import time

import pytest

from app.hear import payments, proxy

from test_hear import SIGNUP, client, hear, make_cong  # noqa: F401  (fixtures)
from test_metering import speak
from test_stripe import auth, keys  # noqa: F401  (fixtures)
from test_cards_codes import fake, mail, pi_succeeded, post_event, run, save_card  # noqa: F401  (fixtures)
from test_proxy import claude, go_live, turn  # noqa: F401  (fixtures)
from test_free_trial import meeting, new_db
from test_bundles import utc

pytestmark = pytest.mark.free_trial
DAY = 86400


def test_the_trial_ending_by_hours_mid_meeting_never_cuts_it_off(client, hear, claude):
    cong, token = make_cong(hear)
    did = go_live(hear, cong, token)
    hear.db.start_session(cong["id"], ["fr", "es"], code="m1", device_id=did)
    speak(hear.db, cong["id"], "m1", 10.5 * 60, ("fr", "es"))       # 21 language-hours: 20 trial, the free hour
    b = hear.db.balance(cong["id"])
    assert b["trial"]["active"] is False and b["free_left_min"] == 0 and b["balance_min"] == 0 and not b["can_start"]
    assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).status_code == 200   # goes on
    del hear.live.sessions[cong["id"]]                                # it ended a while ago: the next one waits
    hear.db._x("UPDATE sessions SET ended_at = ? WHERE congregation_id = ?", (time.time() - 3600, cong["id"]))
    r = client.post("/api/hc/translate", json=turn(), headers=auth(token))
    assert r.status_code == 402 and r.json()["error"] == "out_of_credit"
    bill = client.get("/api/hc/billing", headers=auth(token)).json()
    assert bill["free_trial"] is None and bill["out_of_credit"] and bill["message"].startswith("Out of credit")


def test_a_new_mac_doesnt_restart_the_trial(client, hear):
    cong, _ = make_cong(hear)
    meeting(hear.db, cong["id"], "m1", 5 * 60, at=time.time() - 3 * DAY)
    before = hear.db.balance(cong["id"])["trial"]
    second = hear.db.add_device(cong["id"], "Second Mac")
    assert hear.db.device_by_token(second)["congregation_id"] == cong["id"]
    assert hear.db.balance(cong["id"])["trial"] == before and before["hours_left"] == 15


def test_signing_up_again_doesnt_restart_the_trial(client, hear):
    hear.verify_email = False
    first = client.post("/api/hc/signup", json=SIGNUP).json()
    assert hear.db.by_slug(first["slug"])["free_trial"] is True
    again = client.post("/api/hc/signup", json={**SIGNUP, "email": "bishop@example.org", "name": " dakar ward"}).json()
    assert again["slug"] != first["slug"] and hear.db.by_slug(again["slug"])["free_trial"] is False
    b = client.get("/api/hc/billing", headers=auth(again["device_token"])).json()
    assert b["free_trial"] is None and b["free_hours_left"] == 1 and not b["out_of_credit"]
    # another unit on the same e-mail (a clerk with two wards) still gets its own trial
    ward2 = client.post("/api/hc/signup", json={**SIGNUP, "name": "Dakar 2nd Ward"}).json()
    assert hear.db.by_slug(ward2["slug"])["free_trial"] is True
    # the known gap (docs/money-path.md): the same church under another e-mail is a new trial
    other = client.post("/api/hc/signup", json={**SIGNUP, "email": "clerk@example.org"}).json()
    assert hear.db.by_slug(other["slug"])["free_trial"] is True


def test_a_payment_event_before_its_checkout_event_credits_once(client, hear, fake):
    cong, token = make_cong(hear)
    client.post("/api/hc/checkout", json={"usd": 20}, headers=auth(token))
    (_, p), = fake.of("session")
    pi = {**pi_succeeded(cong["id"], "pi_co"), "metadata": p["payment_intent_data"]["metadata"]}
    s = {"id": "cs_test_1", "mode": "payment", "payment_status": "paid", "currency": "usd", "amount_total": 2000,
         "payment_intent": "pi_co", "metadata": p["metadata"]}
    assert post_event(client, "payment_intent.succeeded", pi).json()["credited"] is True
    for kind, obj in (("checkout.session.completed", s), ("checkout.session.async_payment_succeeded", s),
                      ("payment_intent.succeeded", pi), ("checkout.session.completed", s)):
        r = post_event(client, kind, obj)
        assert r.status_code == 200 and r.json()["credited"] is False, kind
    credit, = hear.db.credits(cong["id"])
    assert credit["seconds"] == 4 * 3600 and credit["cents"] == 2000


def test_a_church_at_zero_hours_can_buy_and_start_again(client, hear, fake):
    cong, token = make_cong(hear)
    hear.db.set_free_trial(cong["id"], False)
    meeting(hear.db, cong["id"], "m1", 90)                            # the free hour and 30 minutes more
    b = client.get("/api/hc/billing", headers=auth(token)).json()
    assert b["out_of_credit"] and b["can_buy"] and b["language_hours_left"] == -0.5
    r = client.post("/api/hc/checkout", json={"usd": 20}, headers=auth(token))
    assert r.status_code == 200 and r.json()["hours"] == 4
    (_, p), = fake.of("session")
    s = {"id": "cs_test_1", "mode": "payment", "payment_status": "paid", "currency": "usd", "amount_total": 2000,
         "payment_intent": "pi_zero", "metadata": p["metadata"]}
    assert post_event(client, "checkout.session.completed", s).json()["credited"] is True
    b = client.get("/api/hc/billing", headers=auth(token)).json()
    assert not b["out_of_credit"] and b["paid_hours_left"] == 3.5     # the 30 minutes owed come off the bundle
    assert hear.db.can_start(hear.db.congregation(cong["id"]))


def test_hours_bought_during_the_trial_wait_for_its_end(client, hear, fake):
    cong, token = make_cong(hear)
    post_event(client, "payment_intent.succeeded", pi_succeeded(cong["id"], "pi_early"))
    meeting(hear.db, cong["id"], "m1", 5 * 60)
    b = client.get("/api/hc/billing", headers=auth(token)).json()
    assert b["paid_hours_left"] == 4 and b["free_trial"]["hours_left"] == 15


def test_auto_top_up_charges_once_the_trial_ends_by_hours(client, hear, fake, mail):
    cong, token = make_cong(hear)
    client.post("/api/hc/card/setup", headers=auth(token))
    save_card(client, hear, fake, cong)
    client.post("/api/hc/auto-topup", json={"enabled": True, "usd": 20}, headers=auth(token))
    meeting(hear.db, cong["id"], "m1", 10 * 60)                       # 10 of the 20 trial hours
    assert run(payments.auto_topup(cong["id"])) == "skipped: free trial" and fake.of("pi") == []
    meeting(hear.db, cong["id"], "m2", 11 * 60, first_seq=10_000)     # 10 more trial hours, then the free hour
    assert hear.db.balance(cong["id"])["trial"]["active"] is False
    assert run(payments.auto_topup(cong["id"])) == "charged"
    (_, params, _), = fake.of("pi")
    assert params["amount"] == 2000 and params["off_session"] is True
    post_event(client, "payment_intent.succeeded", pi_succeeded(cong["id"], "pi_auto", meta=params["metadata"]))
    assert client.get("/api/hc/billing", headers=auth(token)).json()["paid_hours_left"] == 4


def test_a_trial_across_a_month_boundary_leaves_both_free_hours():
    db, c = new_db()
    cid = c["id"]
    meeting(db, cid, "nov", 120, at=utc(2026, 11, 25))
    meeting(db, cid, "dec", 120, at=utc(2026, 12, 3), first_seq=1000)  # day 8: still the trial
    assert db.balance(cid, utc(2026, 12, 5))["free_left_min"] == 60
    meeting(db, cid, "dec2", 30, at=utc(2026, 12, 10), first_seq=2000)  # day 15: the trial is over
    assert db.balance(cid, utc(2026, 12, 11))["free_left_min"] == 30
    b = db.balance(cid, utc(2027, 1, 1, 0))                           # January: a whole free hour again
    assert b["free_left_min"] == 60 and b["balance_min"] == 0 and b["trial"]["active"] is False


def test_only_the_languages_listened_to_are_metered_once_per_chunk():
    db, c = new_db()
    db.set_free_trial(c["id"], False)
    db.start_session(c["id"], ["fr", "es", "pt"], code="m1")           # three offered
    assert db.record_speech(c["id"], "m1", [{"seq": 1, "s": 600.0, "langs": ["fr", "fr", "es"]},
                                            {"seq": 2, "s": 30.0, "langs": []}]) == [1]
    assert db.speech_by_language(c["id"]) == {"es": 60.0, "fr": 60.0}   # a chunk is capped at 60 s; pt unheard
    assert db.balance(c["id"])["used_min"] == 2
