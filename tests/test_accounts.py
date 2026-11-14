"""V3.2 accounts on the hub: several e-mails per congregation (one main, for billing), add-a-Mac pairing codes,
"Email me a code" through Resend (mocked: never the network), device revoke in the admin API, the main e-mail on
Stripe Checkout, and the V3 wire format (targets / speakers / pass-through) on the listen pages."""
import asyncio
import json
import logging
import sqlite3
import time

import httpx
import pytest

from app.hear import api as hear_api
from app.hear.db import Database
from app.hear.mail import RESEND_FROM, RESEND_URL, Resend

from test_hear import SIGNUP, client, engine, hear, listener, make_cong, recv, server, start  # noqa: F401
from test_stripe import fake_stripe, keys  # noqa: F401  (fixtures)

K = "?key=boss"


def auth(token):
    return {"Authorization": f"Bearer {token}"}


class FakeResend:
    """Stands in for app.hear.mail.Resend: remembers what it would have sent."""

    def __init__(self, key="re_test_key"):
        self.key, self.sent = key, []

    def configured(self):
        return bool(self.key)

    async def send(self, to, subject, text):
        self.sent.append((to, subject, text))
        return True


@pytest.fixture
def resend(hear, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    fake = FakeResend()
    hear.resend = fake
    return fake


def code_in(text):
    return text.split("\n\n")[1].strip()


# ------------------------------------------------------------------ e-mails

def test_signup_main_email_and_more(client, hear):
    r = client.post("/api/hc/signup", json={**SIGNUP, "emails": ["Clerk@Example.org", "bishop@example.org"]})
    assert r.status_code == 200, r.text
    assert r.json()["emails"][0] == {"email": "bishop@example.org", "main": True, "added_at": pytest.approx(time.time(), abs=60)}
    assert [(e["email"], e["main"]) for e in r.json()["emails"]] == [("bishop@example.org", True),
                                                                     ("clerk@example.org", False)]
    cong = hear.db.by_slug("dakar-ward")
    assert hear.db.main_email(cong["id"]) == cong["email"] == "bishop@example.org"
    assert client.post("/api/hc/signup", json={**SIGNUP, "emails": ["nope"]}).status_code == 422


def test_old_mac_signup_still_works_with_one_email(client, hear):
    r = client.post("/api/hc/signup", json=SIGNUP)          # 0.1.x / 0.2.x: only "email"
    assert r.status_code == 200 and [e["email"] for e in r.json()["emails"]] == ["bishop@example.org"]


def test_a_device_edits_the_emails(client, hear):
    cong, token = make_cong(hear)
    h = auth(token)
    assert client.get("/api/hc/account").status_code == 403
    assert client.post("/api/hc/emails", json={"email": "x@example.org"}).status_code == 403
    acc = client.get("/api/hc/account", headers=h).json()
    assert acc["main_email"] == "a@example.org" and acc["devices"][0]["this"] is True
    r = client.post("/api/hc/emails", json={"email": "Clerk@Example.org"}, headers=h).json()
    assert r["added"] and [(e["email"], e["main"]) for e in r["emails"]] == [("a@example.org", True),
                                                                            ("clerk@example.org", False)]
    assert not client.post("/api/hc/emails", json={"email": "clerk@example.org"}, headers=h).json()["added"]
    assert client.post("/api/hc/emails", json={"email": "not an address"}, headers=h).status_code == 422
    # change main
    assert client.post("/api/hc/emails/main", json={"email": "nobody@example.org"}, headers=h).status_code == 404
    r = client.post("/api/hc/emails/main", json={"email": "clerk@example.org"}, headers=h).json()
    assert r["main_email"] == "clerk@example.org" and hear.db.congregation(cong["id"])["email"] == "clerk@example.org"
    # the main one can't be removed; another can
    r = client.post("/api/hc/emails/remove", json={"email": "clerk@example.org"}, headers=h)
    assert r.status_code == 409 and r.json()["error"] == "main"
    assert client.post("/api/hc/emails/remove", json={"email": "z@example.org"}, headers=h).status_code == 404
    r = client.post("/api/hc/emails/remove", json={"email": "a@example.org"}, headers=h).json()
    assert [e["email"] for e in r["emails"]] == ["clerk@example.org"]
    # add straight in as the main one
    r = client.post("/api/hc/emails", json={"email": "new@example.org", "main": True}, headers=h).json()
    assert r["main_email"] == "new@example.org" and len(r["emails"]) == 2


def test_at_most_ten_emails(client, hear):
    cong, token = make_cong(hear)
    for i in range(9):
        assert client.post("/api/hc/emails", json={"email": f"e{i}@example.org"}, headers=auth(token)).status_code == 200
    r = client.post("/api/hc/emails", json={"email": "eleven@example.org"}, headers=auth(token))
    assert r.status_code == 409 and r.json()["error"] == "too_many"


def test_old_database_gets_emails_and_revocable_devices(tmp_path):
    """A hub database from before V3.2: each congregation's sign-up e-mail becomes its main one, devices keep
    working, and opening it again changes nothing."""
    path = tmp_path / "old.db"
    db = Database(path)
    a = db.create_congregation(church="LDS", name="Riga", unit_type="ward", email="Riga@Example.org")
    token = db.add_device(a["id"], "Mac")
    db._x("DROP TABLE congregation_emails")
    db._x("DROP TABLE device_codes")
    db._db.close()
    raw = sqlite3.connect(path)   # and devices without revoked_at, as before
    raw.execute("CREATE TABLE d2 AS SELECT id, congregation_id, name, token_hash, created_at, last_seen, app_version"
                " FROM devices")
    raw.execute("DROP TABLE devices")
    raw.execute("ALTER TABLE d2 RENAME TO devices")
    raw.commit()
    raw.close()
    db = Database(path)
    assert db.emails(a["id"]) == [{"email": "riga@example.org", "main": True, "added_at": a["created_at"]}]
    assert db.device_by_token(token)["congregation_id"] == a["id"]
    db.add_email(a["id"], "two@example.org")
    db._db.close()
    assert len(Database(path).emails(a["id"])) == 2   # not seeded twice


# ------------------------------------------------------------------ pairing codes ("Add device")

def test_add_device_code_connects_a_new_mac_once(client, hear):
    cong, token = make_cong(hear)
    assert client.post("/api/hc/pairing-code").status_code == 403
    r = client.post("/api/hc/pairing-code", headers=auth(token)).json()
    code = r["code"]
    assert len(code) == 9 and code[4] == "-" and 890 <= r["expires_in"] <= 900
    p = client.post("/api/hc/pair", json={"code": code.lower().replace("-", " "), "device_name": "Clerk's Mac"})
    assert p.status_code == 200, p.text
    j = p.json()
    assert j["slug"] == "dakar-ward" and j["billing"] == "credits" and j["device_token"].startswith("hcd_")
    assert client.get("/api/hc/billing", headers=auth(j["device_token"])).status_code == 200
    assert sorted(d["name"] for d in hear.db.devices(cong["id"])) == ["Clerk's Mac", "Mac"]
    again = client.post("/api/hc/pair", json={"code": code})                     # single use
    assert again.status_code == 403 and again.json()["error"] == "bad_code"


def test_pairing_code_expires_and_a_new_one_replaces_it(client, hear, monkeypatch):
    cong, token = make_cong(hear)
    old = client.post("/api/hc/pairing-code", headers=auth(token)).json()["code"]
    new = client.post("/api/hc/pairing-code", headers=auth(token)).json()["code"]
    assert client.post("/api/hc/pair", json={"code": old}).status_code == 403
    later = time.time() + 16 * 60
    monkeypatch.setattr("app.hear.db.time.time", lambda: later)
    assert client.post("/api/hc/pair", json={"code": new}).status_code == 403


def test_pairing_code_of_a_hidden_congregation_is_refused(client, hear):
    cong, token = make_cong(hear)
    code = client.post("/api/hc/pairing-code", headers=auth(token)).json()["code"]
    hear.db.set_hidden(cong["id"], True)
    assert client.post("/api/hc/pair", json={"code": code}).status_code == 403


def test_wrong_codes_lock_the_ip(client, hear):
    cong, token = make_cong(hear)
    code = client.post("/api/hc/pairing-code", headers=auth(token)).json()["code"]
    for _ in range(10):
        assert client.post("/api/hc/pair", json={"code": "AAAA-AAAA"}).status_code == 403
    r = client.post("/api/hc/pair", json={"code": code})                          # even the right one, while locked
    assert r.status_code == 429 and r.json()["error"] == "locked" and int(r.headers["retry-after"]) > 0


# ------------------------------------------------------------------ "Email me a code"

def test_email_code_without_resend_key_is_not_configured(client, hear, monkeypatch, caplog):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    make_cong(hear)
    r = client.post("/api/hc/email-code", json={"email": "a@example.org"})
    assert r.status_code == 503 and r.json()["error"] == "not_configured"
    assert not hear.db._q("SELECT 1 FROM device_codes")                            # no code made, nothing sent


def test_email_code_goes_to_every_address_and_connects(client, hear, resend, caplog):
    caplog.set_level(logging.INFO)
    cong, _ = make_cong(hear)
    hear.db.add_email(cong["id"], "clerk@example.org")
    r = client.post("/api/hc/email-code", json={"email": "Clerk@Example.org"})
    assert r.status_code == 200 and r.json()["ok"]
    assert sorted(t for t, _, _ in resend.sent) == ["a@example.org", "clerk@example.org"]
    (_, subject, text), _ = resend.sent
    code = code_in(text)
    assert subject == "Your Hear Church code" and "Dakar Ward" in text and "15 minutes" in text
    assert {code_in(t) for _, _, t in resend.sent} == {code}                       # one code, to all
    assert code not in caplog.text and "re_test_key" not in caplog.text
    p = client.post("/api/hc/pair", json={"code": code, "device_name": "New Mac"})
    assert p.status_code == 200 and p.json()["slug"] == "dakar-ward"
    assert client.post("/api/hc/pair", json={"code": code}).status_code == 403


def test_email_code_answers_the_same_for_unknown_addresses(client, hear, resend):
    cong, _ = make_cong(hear)
    known = client.post("/api/hc/email-code", json={"email": "a@example.org"})
    unknown = client.post("/api/hc/email-code", json={"email": "stranger@example.org"})
    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())
    assert [t for t, _, _ in resend.sent] == ["a@example.org"]
    hear.db.set_hidden(cong["id"], True)                                           # hidden: nothing sent either
    assert client.post("/api/hc/email-code", json={"email": "a@example.org"}).status_code == 200
    assert len(resend.sent) == 1


def test_email_code_is_rate_limited_per_address_and_ip(client, hear, resend):
    make_cong(hear)
    for _ in range(3):
        assert client.post("/api/hc/email-code", json={"email": "a@example.org"}).status_code == 200
    assert client.post("/api/hc/email-code", json={"email": "A@example.org"}).status_code == 429
    assert len(resend.sent) == 3
    for i in range(6):   # the IP's 10 per hour (the refused 4th counted too), whatever the address
        assert client.post("/api/hc/email-code", json={"email": f"x{i}@example.org"}).status_code == 200
    assert client.post("/api/hc/email-code", json={"email": "y@example.org"}).status_code == 429


def test_only_the_newest_emailed_code_works(client, hear, resend):
    make_cong(hear)
    client.post("/api/hc/email-code", json={"email": "a@example.org"})
    client.post("/api/hc/email-code", json={"email": "a@example.org"})
    first, second = (code_in(t) for _, _, t in resend.sent)
    assert client.post("/api/hc/pair", json={"code": first}).status_code == 403
    assert client.post("/api/hc/pair", json={"code": second}).status_code == 200


async def test_resend_posts_the_message_and_never_logs_secrets(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"id": "em_1"})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("RESEND_API_KEY", "re_secret_123")
    r = Resend()
    assert r.configured() and await r.send("a@example.org", "Your Hear Church code", "code ABCD-EFGH")
    req, = seen
    assert str(req.url) == RESEND_URL and req.headers["authorization"] == "Bearer re_secret_123"
    assert json.loads(req.content) == {"from": "Hear Church <no-reply@hearchurch.com>", "to": ["a@example.org"],
                                       "subject": "Your Hear Church code", "text": "code ABCD-EFGH"}
    assert RESEND_FROM == "Hear Church <no-reply@hearchurch.com>"
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(lambda q: httpx.Response(401)), **kw))
    assert not await r.send("a@example.org", "s", "code ABCD-EFGH")
    assert "re_secret_123" not in caplog.text and "ABCD-EFGH" not in caplog.text and "HTTP 401" in caplog.text
    monkeypatch.delenv("RESEND_API_KEY")
    assert not Resend().configured() and not await Resend().send("a@example.org", "s", "t")


# ------------------------------------------------------------------ billing e-mail on Checkout

def test_checkout_sends_receipts_to_the_main_email(client, hear, keys, fake_stripe):
    cong, token = make_cong(hear)
    hear.db.add_email(cong["id"], "treasurer@example.org", main=True)
    assert client.post("/api/hc/checkout", json={"usd": 25}, headers=auth(token)).status_code == 200
    p, = fake_stripe.created
    assert p["customer_email"] == "treasurer@example.org"


# ------------------------------------------------------------------ admin: e-mails, devices, revoke

def test_admin_lists_emails_and_devices_and_revokes(client, hear):
    cong, token = make_cong(hear)
    hear.db.add_email(cong["id"], "clerk@example.org")
    other = hear.db.add_device(cong["id"], "Old Mac")
    assert client.get(f"/api/admin/congregations/{cong['id']}/account").status_code == 403
    r = client.get(f"/api/admin/congregations/{cong['id']}/account" + K).json()
    assert r["main_email"] == "a@example.org" and [e["email"] for e in r["emails"]] == ["a@example.org",
                                                                                      "clerk@example.org"]
    old_id = next(d["id"] for d in r["devices"] if d["name"] == "Old Mac")
    assert client.post(f"/api/admin/devices/{old_id}/revoke").status_code == 403
    v = client.post(f"/api/admin/devices/{old_id}/revoke" + K)
    assert v.status_code == 200 and v.json()["device"]["revoked_at"]
    assert client.post(f"/api/admin/devices/{old_id}/revoke" + K).status_code == 200   # again: fine
    assert client.post("/api/admin/devices/9999/revoke" + K).status_code == 404
    assert client.get("/api/hc/billing", headers=auth(other)).status_code == 403
    assert client.get("/api/hc/billing", headers=auth(token)).status_code == 200
    one = client.get(f"/api/admin/congregations/{cong['id']}" + K).json()
    assert [d["name"] for d in one["devices"] if d["revoked_at"]] == ["Old Mac"] and len(one["emails"]) == 2
    assert client.get("/api/hc/account", headers=auth(token)).json()["devices"][0]["name"] == "Mac"
    assert client.get("/api/admin/congregations/9999/account" + K).status_code == 404


async def test_revoking_a_live_mac_ends_its_meeting(server, hear):
    cong, token = make_cong(hear)
    eng = await engine(server, token)
    assert (await start(eng, ["fr"]))["type"] == "session_started"
    did = hear.db.device_by_token(token)["id"]
    async with httpx.AsyncClient() as http:
        r = await http.post(f"http://127.0.0.1:{server}/api/admin/devices/{did}/revoke" + K)
    assert r.status_code == 200
    assert (await recv(eng, "session_ended"))["message"].startswith("This Mac was removed")
    assert hear.live.get(cong["id"]) is None
    with pytest.raises(Exception):   # the link is closed and a new one is refused
        for _ in range(5):
            await recv(eng, timeout=2)
    import websockets
    st = hear_api.H.live.stream
    with pytest.raises(Exception):   # the revoked token opens no new link
        ws = await websockets.connect(f"ws://127.0.0.1:{server}/ws/engine", additional_headers=auth(token))
        await ws.send(json.dumps({"type": "hello", "languages": ["fr"],
                                  "stream": {"sample_rate": st.sample_rate, "bitrate_kbps": st.bitrate_kbps}}))
        await asyncio.wait_for(ws.recv(), 3)


# ------------------------------------------------------------------ V3 wire format on the listen pages

async def test_v3_targets_are_the_pills_and_pass_through_works(server, hear, client):
    """Riga: speakers ru + lv, targets en + lv. The pills are exactly the targets, in order; a Latvian listener
    (a speaker language) is not told to listen in the room."""
    cong, token = make_cong(hear, "Riga Ward")
    eng = await engine(server, token, languages=("fr", "en", "ru", "lv"))
    started = await start(eng, ["lv", "en"], targets=["en", "lv"], speakers=["ru", "lv"], speaker="ru",
                          passthrough=True)
    assert started["type"] == "session_started"
    _, init = await listener(server, "riga-ward", "lv")
    assert [l["code"] for l in init["languages"]] == ["en", "lv"]
    assert [l["name"] for l in init["languages"]] == ["English", "Latviešu"]
    assert init["lang"] == "lv" and init["speakers"] == ["ru", "lv"] and init["passthrough"] is True
    nearby = client.get("/api/hc/search?q=riga").json()["results"][0]
    assert [l["code"] for l in nearby["languages"]] == ["en", "lv"]
    assert [l["code"] for l in client.get("/api/hc/c/riga-ward").json()["languages"]] == ["en", "lv"]
    # the meeting adds Russian listeners and a French speaker: pills and speakers follow
    ws_l, _ = await listener(server, "riga-ward", "en")
    await eng.send(json.dumps({"type": "languages", "languages": ["en", "lv", "ru"], "targets": ["en", "lv", "ru"],
                               "speakers": ["ru", "lv", "fr"]}))
    sp = await recv(ws_l, "speaker")
    assert sp["speakers"] == ["ru", "lv", "fr"] and sp["passthrough"] is True
    assert [l["code"] for l in (await recv(ws_l, "languages"))["languages"]] == ["en", "lv", "ru"]
    # a speaker change relayed from the engine keeps pass-through on for the phones
    await eng.send(json.dumps({"type": "hub", "lang": "en", "msg": {"type": "speaker", "speaker": "lv"}}))
    sp = await recv(ws_l, "speaker")
    assert sp["speaker"] == "lv" and sp["passthrough"] is True


async def test_old_mac_format_keeps_working(server, hear):
    """0.1.x / 0.2.x: languages + speaker only. Pills = languages; the speaker's language is paused as before."""
    cong, token = make_cong(hear)
    eng = await engine(server, token, languages=("fr", "en", "es"))
    await start(eng, ["fr", "en"], speaker="en")
    _, init = await listener(server, "dakar-ward", "en")
    assert [l["code"] for l in init["languages"]] == ["fr", "en"]
    assert init["speaker"] == "en" and init["passthrough"] is False and init["speakers"] == ["en"]
    ws_l, _ = await listener(server, "dakar-ward", "fr")
    await eng.send(json.dumps({"type": "languages", "languages": ["fr", "es"]}))
    assert [l["code"] for l in (await recv(ws_l, "languages"))["languages"]] == ["fr", "es"]
