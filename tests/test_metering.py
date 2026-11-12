"""Metering and the credit ledger: speech minutes per heard language, the free hour first, idempotent reports and
top-ups, the negative-balance rule (never cut off, next meeting refused) and own-key congregations never debited."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from app import main
from app.engine import PublishClient
from app.hear import live as hear_live
from app.hear.db import Database

from test_hear import client, engine, hear, listener, make_cong, recv, server, start  # noqa: F401  (fixtures)


def cong_db(billing="credits"):
    db = Database(":memory:")
    c = db.create_congregation(church="LDS", name="Dakar Ward", unit_type="ward", email="a@x.org")
    db._x("UPDATE congregations SET billing = ? WHERE id = ?", (billing, c["id"]))
    return db, db.congregation(c["id"])


def speak(db, cid, code, minutes, langs=("fr",), first_seq=1):
    """`minutes` of speech in 10-second chunks, reported for session `code`."""
    items = [{"seq": first_seq + i, "s": 10.0, "langs": list(langs)} for i in range(int(minutes * 6))]
    return db.record_speech(cid, code, items)


# ------------------------------------------------------------------ ledger (database)

def test_the_free_hour_is_used_first_then_bought_credit():
    db, c = cong_db()
    b = db.balance(c["id"])
    assert b["free_left_min"] == 60 and b["balance_usd"] == 0 and b["left_min"] == 60 and b["can_start"]
    db.start_session(c["id"], ["fr", "es"], code="s1", billing="credits")
    speak(db, c["id"], "s1", 20, ("fr", "es"))                      # 20 min × 2 languages = 40 language-minutes
    b = db.balance(c["id"])
    assert b["used_min"] == 40 and b["free_left_min"] == 20 and b["balance_usd"] == 0 and b["left_min"] == 20
    db.add_credit(c["id"], "pi_1", 1000)                            # $10 = 2 language-hours
    b = db.balance(c["id"])
    assert b["balance_min"] == 120 and b["balance_usd"] == 10 and b["left_min"] == 140
    speak(db, c["id"], "s1", 15, ("fr", "es"), first_seq=1000)     # 30 more: 20 from the free hour, 10 bought
    b = db.balance(c["id"])
    assert b["free_left_min"] == 0 and b["balance_min"] == 110 and b["balance_usd"] == pytest.approx(9.17)
    assert b["bought_usd"] == 10 and b["metered_min"] == 70


def test_a_report_sent_twice_counts_once():
    db, c = cong_db()
    db.start_session(c["id"], ["fr"], code="s1", billing="credits")
    items = [{"seq": 1, "s": 9.5, "langs": ["fr"]}, {"seq": 2, "s": 4.0, "langs": ["fr", "es"]}]
    assert db.record_speech(c["id"], "s1", items) == [1, 2]
    assert db.record_speech(c["id"], "s1", items) == [1, 2]          # acknowledged again, stored once
    assert db.speech_by_language(c["id"]) == {"fr": 13.5, "es": 4.0}
    # junk is skipped (and not acknowledged), absurd lengths are capped, other sessions are unknown
    assert db.record_speech(c["id"], "s1", [{"seq": "x", "s": 1, "langs": ["fr"]}, {"seq": 3},
                                            {"seq": 4, "s": 9999, "langs": ["fr"]}]) == [4]
    assert db.speech_by_language(c["id"])["fr"] == 73.5
    assert db.record_speech(c["id"], "nope", items) is None
    other = db.create_congregation(church="LDS", name="Thies", unit_type="branch", email="b@x.org")
    assert db.record_speech(other["id"], "s1", items) is None       # a session code is per congregation


def test_a_top_up_is_credited_once_per_payment():
    db, c = cong_db()
    credit, created = db.add_credit(c["id"], "pi_abc", 2500)
    assert created and credit["cents"] == 2500 and credit["seconds"] == 5 * 3600
    again, created = db.add_credit(c["id"], "pi_abc", 2500)         # Stripe retries its webhook
    assert not created and again["id"] == credit["id"] and len(db.credits(c["id"])) == 1
    assert db.balance(c["id"])["balance_usd"] == 25
    with pytest.raises(ValueError):
        db.add_credit(c["id"], "pi_abc", 9900)                     # same payment, other amount
    other = db.create_congregation(church="LDS", name="Thies", unit_type="branch", email="b@x.org")
    with pytest.raises(ValueError):
        db.add_credit(other["id"], "pi_abc", 2500)                 # same payment, other congregation
    for bad in ("", " "):
        with pytest.raises(ValueError):
            db.add_credit(c["id"], bad, 100)
    with pytest.raises(ValueError):
        db.add_credit(c["id"], "pi_neg", -5)


def test_the_balance_goes_negative_and_only_the_next_meeting_waits():
    db, c = cong_db()
    db.start_session(c["id"], ["fr", "es"], code="s1", billing="credits")
    speak(db, c["id"], "s1", 45, ("fr", "es"))                      # 90 language-minutes: 30 past the free hour
    b = db.balance(c["id"])
    assert b["balance_min"] == -30 and b["balance_usd"] == -2.5 and not b["can_start"] and not db.can_start(c)
    assert speak(db, c["id"], "s1", 1, first_seq=5000)              # the meeting's speech is still taken
    db.add_credit(c["id"], "pi_1", 500)                             # $5 = 60 min: back above zero
    assert db.balance(c["id"])["left_min"] == 29 and db.can_start(c)


def test_own_key_is_metered_but_never_debited():
    db, c = cong_db("own_key")
    db.start_session(c["id"], ["fr"], code="s1", billing="own_key")
    speak(db, c["id"], "s1", 300)
    b = db.balance(c["id"])
    assert b["metered_min"] == 300 and b["used_min"] == 0 and b["free_left_min"] == 60 and db.can_start(c)
    # a congregation that switches to credits later isn't charged for its own-key meetings
    db._x("UPDATE congregations SET billing = 'credits' WHERE id = ?", (c["id"],))
    assert db.can_start(db.congregation(c["id"])) and db.balance(c["id"])["used_min"] == 0


def test_an_old_database_gets_session_codes_and_its_sessions_are_never_debited(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE congregations (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE, church TEXT NOT NULL,
            name TEXT NOT NULL, unit_type TEXT NOT NULL, stake TEXT, address TEXT NOT NULL DEFAULT '', lat REAL, lng REAL,
            meeting_times TEXT NOT NULL DEFAULT '[]', timezone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL,
            email_verified INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, hidden INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, congregation_id INTEGER NOT NULL,
            languages TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, trial INTEGER NOT NULL DEFAULT 0);
        INSERT INTO congregations (slug, church, name, unit_type, email, created_at) VALUES ('w', 'LDS', 'W', 'ward', 'a@x', 0);
        INSERT INTO sessions (congregation_id, languages, started_at, ended_at) VALUES (1, '["fr"]', 0, 1);
    """)
    old.commit()
    old.close()
    db = Database(path)
    s = db.sessions(1)[0]
    assert s["billing"] == "own_key" and s["code"] is None
    assert db.congregation(1)["billing"] == "own_key"


# ------------------------------------------------------------------ hub: start check, meter messages, top-up route

async def test_the_hub_refuses_a_new_meeting_without_credit_but_never_a_resume(server, hear):
    cong, token = make_cong(hear)
    a = await engine(server, token)
    code = (await start(a, ["fr"]))["session"]
    used = [{"seq": i, "s": 60.0, "langs": ["fr"]} for i in range(61)]          # 61 language-minutes
    await a.send(json.dumps({"type": "meter", "session": code, "items": used}))
    ok = await recv(a, "meter_ok")
    assert ok["session"] == code and sorted(ok["seqs"]) == list(range(61))
    assert hear.db.balance(cong["id"])["balance_min"] == -1
    assert hear.live.get(cong["id"]) is not None                               # never cut off mid-meeting
    await a.close()                                                            # the link drops ...
    await asyncio.sleep(0.1)
    a2 = await engine(server, token)
    assert (await start(a2, ["fr"], resume=code))["resumed"]                   # ... and the meeting resumes
    await a2.send(json.dumps({"type": "session_stop"}))
    await asyncio.sleep(0.2)
    # a report for the ended meeting still counts (sent late after a reconnect)
    await a2.send(json.dumps({"type": "meter", "session": code, "items": [{"seq": 99, "s": 60, "langs": ["fr"]}]}))
    assert (await recv(a2, "meter_ok"))["seqs"] == [99]
    refused = await start(a2, ["fr"])
    assert refused["type"] == "session_refused" and "credit" in refused["message"]
    assert len(hear.db.sessions(cong["id"])) == 1
    await a2.send(json.dumps({"type": "meter", "session": "19990101-000000", "items": [{"seq": 1, "s": 1, "langs": ["fr"]}]}))
    assert (await recv(a2, "meter_ok"))["unknown"] is True                     # the Mac drops what can't be placed
    hear.db.add_credit(cong["id"], "pi_topup", 500)
    assert (await start(a2, ["fr"]))["type"] == "session_started"


async def test_a_meeting_resumed_after_the_grace_or_a_hub_restart_is_never_refused(server, hear):
    cong, token = make_cong(hear)
    a = await engine(server, token)
    code = (await start(a, ["fr"]))["session"]
    await a.send(json.dumps({"type": "meter", "session": code,
                             "items": [{"seq": i, "s": 60.0, "langs": ["fr"]} for i in range(70)]}))
    await recv(a, "meter_ok")
    hear.live.end(hear.live.get(cong["id"]), notify=False)                    # the hub lost the live session
    again = await start(a, ["fr"], resume=code)                                # the Mac asks to carry on
    assert again["type"] == "session_started" and not again["resumed"]
    await a.send(json.dumps({"type": "session_stop"}))
    await asyncio.sleep(0.2)
    assert (await start(a, ["fr"], resume="20000101-000000"))["type"] == "session_refused"   # not a real one


async def test_own_key_congregations_are_never_refused(server, hear):
    cong, token = make_cong(hear)
    hear.db._x("UPDATE congregations SET billing = 'own_key' WHERE id = ?", (cong["id"],))
    a = await engine(server, token)
    code = (await start(a, ["fr"]))["session"]
    await a.send(json.dumps({"type": "meter", "session": code,
                             "items": [{"seq": i, "s": 60.0, "langs": ["fr"]} for i in range(120)]}))
    await recv(a, "meter_ok")
    await a.send(json.dumps({"type": "session_stop"}))
    await asyncio.sleep(0.2)
    assert (await start(a, ["fr"]))["type"] == "session_started"
    b = hear.db.balance(cong["id"])
    assert b["metered_min"] == 120 and b["used_min"] == 0


def test_top_up_route_is_operator_only_and_idempotent(client, hear):
    cong, token = make_cong(hear)
    url = f"/api/admin/congregations/{cong['id']}/credits"
    assert client.post(url, json={"payment_id": "pi_1", "cents": 1000}).status_code in (401, 403)
    r = client.post(url + "?key=boss", json={"payment_id": "pi_1", "cents": 1000})
    assert r.status_code == 200 and r.json()["created"] and r.json()["balance"]["balance_usd"] == 10
    r = client.post(url + "?key=boss", json={"payment_id": "pi_1", "cents": 1000})
    assert r.status_code == 200 and not r.json()["created"] and r.json()["balance"]["balance_usd"] == 10
    assert client.post(url + "?key=boss", json={"payment_id": "pi_1", "cents": 2000}).status_code == 409
    assert client.post(url + "?key=boss", json={"payment_id": "pi_2", "cents": 0}).status_code == 422
    assert client.post("/api/admin/congregations/999/credits?key=boss",
                       json={"payment_id": "pi_3", "cents": 100}).status_code == 404
    b = client.get("/api/hc/billing", headers={"Authorization": f"Bearer {token}"}).json()
    assert b["billing"] == "credits" and b["balance"]["left_min"] == 180 and b["balance"]["can_start"]
    listed = client.get("/api/admin/congregations?key=boss").json()["congregations"][0]
    assert listed["balance"]["balance_usd"] == 10


# ------------------------------------------------------------------ the Mac: listener gating, batching, resending

def mac_client():
    c = PublishClient("ws://unused", "t", "Mac")
    sent = []
    c._send = sent.append
    c.running, c.session_code = True, "s1"
    return c, sent


def test_the_mac_meters_only_heard_languages_and_only_while_live():
    c, _ = mac_client()
    c.heard = {"fr"}
    c._speech(1, 8.0, ["fr", "es"])                                  # es is translated but nobody listens
    c._speech(2, 6.0, ["es"])                                        # nobody hears anything: not metered
    c.heard = {"fr", "es"}
    c._speech(3, 5.0, ["fr", "es"])
    c._speech(4, 0.0, ["fr"])                                        # no speech
    assert c.meter == {"s1": {1: {"seq": 1, "s": 8.0, "langs": ["fr"]},
                              3: {"seq": 3, "s": 5.0, "langs": ["es", "fr"]}}}
    c.running = False                                                # refused, or the link is down
    c._speech(5, 5.0, ["fr"])
    assert 5 not in c.meter["s1"]


def test_the_mac_resends_until_acknowledged_and_bounds_its_backlog(monkeypatch):
    c, sent = mac_client()
    c.heard = {"fr"}
    for seq in range(1, 6):
        c._speech(seq, 5.0, ["fr"])
    monkeypatch.setattr(PublishClient, "METER_BATCH", 2)
    c._send_meter()
    assert [len(m["items"]) for m in sent] == [2, 2, 1] and all(m["type"] == "meter" for m in sent)
    c._metered({"type": "meter_ok", "session": "s1", "seqs": [1, 2]})  # one answer came back before the drop
    sent.clear()
    c._send_meter()                                                  # after reconnecting
    assert [i["seq"] for m in sent for i in m["items"]] == [3, 4, 5]
    c._metered({"type": "meter_ok", "session": "s1", "seqs": [3, 4, 5]})
    assert c.meter == {}
    monkeypatch.setattr(PublishClient, "METER_KEEP", 3)
    for seq in range(10, 15):
        c._speech(seq, 5.0, ["fr"])
    assert list(c.meter["s1"]) == [12, 13, 14]                       # the oldest go first


async def test_the_mac_reports_heard_speech_to_the_hub(server, hear, monkeypatch):
    """End to end: a Mac meeting in fr + es with a listener on fr only bills fr only, through the real link."""
    monkeypatch.setattr(hear_live, "LISTENERS_EVERY_S", 0.1)
    monkeypatch.setattr(PublishClient, "METER_EVERY_S", 0.2)
    cong, token = make_cong(hear)
    chans = {c: NS(hub=NS(taps=[]), broadcaster=NS(taps=[])) for c in ("fr", "es")}
    pipe = NS(running=False, started_at=0.0, channels=chans, on_channels=dict(chans), speech_taps=[])
    main.S.pipeline = pipe
    mac = PublishClient(f"ws://127.0.0.1:{server}/ws/engine", token, "Test Mac")
    task = asyncio.create_task(mac.run_forever())

    async def until(cond):
        for _ in range(100):
            if cond():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("timed out")

    try:
        await until(lambda: mac.connected)
        pipe.running, pipe.started_at = True, 1.0
        await until(lambda: mac.running)
        ws, _ = await listener(server, "dakar-ward", "fr")
        await until(lambda: mac.heard == {"fr"})
        for tap in pipe.speech_taps:
            tap(1, 30.0, ["fr", "es"])
        await until(lambda: not mac.meter and hear.db.speech_by_language(cong["id"]))
        assert hear.db.speech_by_language(cong["id"]) == {"fr": 30.0}
        pipe.running = False
        await until(lambda: hear.live.get(cong["id"]) is None)
        assert hear.db.balance(cong["id"])["used_min"] == 0.5
    finally:
        task.cancel()
        del main.S.pipeline
