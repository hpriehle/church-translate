"""Trial lifecycle e-mails (app/hear/trial_emails.py, docs/trial-emails.md): each trigger, once only, the cutoff,
dry run and off. Resend is faked: nothing leaves the machine."""
import asyncio
import logging
import time

import pytest

from app.hear import trial_emails as te

from test_hear import SIGNUP, client, hear, make_cong, verify_link  # noqa: F401  (fixtures)
from test_free_trial import meeting

pytestmark = pytest.mark.free_trial
DAY = 86400


class FakeResend:
    def __init__(self, ok=True, key="re_test"):
        self.sent, self.ok, self.key = [], ok, key

    def configured(self):
        return bool(self.key)

    async def send(self, to, subject, text, reply_to=None):
        self.sent.append({"to": to, "subject": subject, "text": text, "reply_to": reply_to})
        return self.ok


@pytest.fixture
def on(hear, monkeypatch):
    """Turned on, with the cutoff stored a minute ago (so congregations made in the test are in scope)."""
    monkeypatch.setenv("HC_TRIAL_EMAILS", "1")
    hear.resend = FakeResend()
    hear.db.setting(te.CUTOFF_KEY, repr(time.time() - 60))
    return hear


def check(**kw):
    return asyncio.run(te.check(**kw))


def test_off_by_default_sends_nothing(hear):
    hear.resend = FakeResend()
    make_cong(hear)
    assert te.mode() == "off" and check() == [] and hear.resend.sent == []
    assert hear.db.setting(te.CUTOFF_KEY) is None   # off never stores the cutoff


def test_welcome_once_to_the_signup_address_signed_maddie(on):
    c, _ = make_cong(on, email="Clerk@Example.org")
    assert check() == [(c["id"], "welcome")]
    m = on.resend.sent[0]
    assert m["to"] == "clerk@example.org" and m["reply_to"] == "maddie@hearchurch.com"
    assert "https://hearchurch.com/setup" in m["text"] and "14 days, up to 20 language-hours" in m["text"]
    assert m["text"].endswith("Maddie\nHear Church\n")
    assert check() == [] and len(on.resend.sent) == 1   # once only


def test_unverified_signups_wait(on):
    c = on.db.create_congregation(church="LDS", name="Unverified Ward", unit_type="ward", email="u@example.org")
    assert check() == []
    on.db.set_verified(c["id"])
    assert check() == [(c["id"], "welcome")]


def test_day3_only_without_a_meeting(on):
    a, _ = make_cong(on, "Quiet Ward", "q@example.org")
    b, _ = make_cong(on, "Busy Ward", "b@example.org")
    check()                                                 # the two welcomes
    meeting(on.db, b["id"], "m1", 10)
    assert check(now=time.time() + 2 * DAY) == []
    assert check(now=time.time() + 3 * DAY + 60) == [(a["id"], "day3")]
    assert "before Sunday" in on.resend.sent[-1]["subject"]
    assert check(now=time.time() + 4 * DAY) == []


def test_trial75_on_day_11(on):
    c, _ = make_cong(on)
    meeting(on.db, c["id"], "m1", 30)
    check()
    assert check(now=time.time() + 10 * DAY) == []
    assert check(now=time.time() + 10.6 * DAY) == [(c["id"], "trial75")]
    text = on.resend.sent[-1]["text"]
    assert "about 3 days and 20 language-hours left" in text and "https://hearchurch.com/account" in text
    assert "$20 for 4 hours, $50 for 11, $100 for 25 or $250 for 70" in text
    assert check(now=time.time() + 12 * DAY) == []


def test_trial75_at_15_hours_then_ended_at_20(on):
    c, _ = make_cong(on)
    meeting(on.db, c["id"], "m1", 15 * 60)
    assert check() == [(c["id"], "welcome"), (c["id"], "trial75")]
    meeting(on.db, c["id"], "m2", 5 * 60, first_seq=10000)
    assert check() == [(c["id"], "ended")]
    assert "has ended" in on.resend.sent[-1]["text"] and check() == []


def test_ended_by_days_skips_the_75_mail(on):
    c, _ = make_cong(on)
    meeting(on.db, c["id"], "m1", 30)
    assert check(now=time.time() + 15 * DAY) == [(c["id"], "welcome"), (c["id"], "ended")]


def test_cutoff_is_stored_at_first_enable_and_older_congregations_are_left_alone(hear, monkeypatch):
    hear.resend = FakeResend()
    old, _ = make_cong(hear, "Old Ward", "o@example.org")
    hear.db._x("UPDATE congregations SET created_at = created_at - 5 WHERE id = ?", (old["id"],))
    monkeypatch.setenv("HC_TRIAL_EMAILS", "1")
    assert check() == []                                    # made before the first enable
    stored = hear.db.setting(te.CUTOFF_KEY)
    new, _ = make_cong(hear, "New Ward", "n@example.org")
    assert check() == [(new["id"], "welcome")]
    assert hear.db.setting(te.CUTOFF_KEY) == stored         # kept, not moved


def test_no_trial_no_mail(on):
    c = on.db.create_congregation(church="LDS", name="Own Key Ward", unit_type="ward", email="k@example.org",
                                  free_trial=False)
    on.db.set_verified(c["id"])
    assert check() == []


def test_never_without_resend_and_a_failed_send_is_retried(on):
    c, _ = make_cong(on)
    on.resend.key = ""
    assert check() == [] and on.resend.sent == []
    on.resend.key, on.resend.ok = "re_test", False
    assert check() == [] and on.db.trial_emails_sent(c["id"]) == {}
    on.resend.ok = True
    assert check() == [(c["id"], "welcome")]


def test_dry_run_logs_once_and_records_nothing(hear, monkeypatch, caplog):
    monkeypatch.setenv("HC_TRIAL_EMAILS", "dry")
    monkeypatch.setattr(te, "_dry_logged", set())
    hear.resend = FakeResend(key="")                        # dry needs no Resend
    c, _ = make_cong(hear)
    with caplog.at_level(logging.INFO, "hear"):
        assert check() == [(c["id"], "welcome")]
        assert check() == []                                # logged once
    assert "dry run" in caplog.text and hear.resend.sent == []
    assert hear.db.trial_emails_sent(c["id"]) == {} and hear.db.setting(te.CUTOFF_KEY) is None


def test_signup_and_meeting_end_check_right_away(client, on, monkeypatch):
    asked = []
    monkeypatch.setattr(te, "soon", asked.append)
    r = client.post("/api/hc/signup", json={**SIGNUP, "email": "w@example.org"})
    assert r.status_code == 200 and asked == []            # not before the e-mail is verified
    assert client.get(verify_link(on)).status_code == 200
    cid = on.db.by_slug(r.json()["slug"])["id"]
    on.verify_email = False                                # the hub's setting today: verified at sign-up
    r = client.post("/api/hc/signup", json={**SIGNUP, "name": "Second Ward", "email": "x@example.org"})
    cid2 = on.db.by_slug(r.json()["slug"])["id"]
    on.live.after_end(cid)
    assert asked == [cid, cid2, cid]


def test_soon_sends_in_the_background(on):
    c, _ = make_cong(on)

    async def go():
        te.soon(c["id"])
        await asyncio.sleep(0.1)
    asyncio.run(go())
    assert [m["subject"] for m in on.resend.sent] == ["Welcome to Hear Church"]


def test_admin_lists_what_was_sent_and_what_is_due(client, on):
    a, _ = make_cong(on, "Sent Ward", "s@example.org")
    check()
    b, _ = make_cong(on, "Due Ward", "d@example.org")
    assert client.get("/api/admin/trial-emails").status_code == 403
    r = client.get("/api/admin/trial-emails?key=boss").json()
    assert r["mode"] == "on" and r["resend_configured"] and r["cutoff"] is not None
    rows = {x["id"]: x for x in r["congregations"]}
    assert list(rows[a["id"]]["sent"]) == ["welcome"] and rows[a["id"]]["due"] == []
    assert rows[b["id"]]["sent"] == {} and rows[b["id"]]["due"] == ["welcome"]
