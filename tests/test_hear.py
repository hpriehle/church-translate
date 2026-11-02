"""Hear Church: database, sign-up, device tokens, engine-published sessions, /c/<slug> listeners, admin,
and that the legacy ward (engine token, /, /ws/engine) still works."""
import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
import websockets
from fastapi.testclient import TestClient

from app import main
from app.broadcast.hub import Hub
from app.config import Settings
from app.hear import api as hear_api
from app.hear.api import Hear, next_meeting
from app.hear.db import Database, hash_token, slugify
from app.hear.live import Live
from app.hear.mail import Mailer, RateLimiter

SIGNUP = {"church": "Latter-day Saints", "name": "Dakar Ward", "unit_type": "ward", "stake": "Dakar Stake",
          "address": "1 Rue Exemple, Dakar", "email": "Bishop@Example.org",
          "meeting_times": [{"day": "sun", "time": "10:00"}], "timezone": "Africa/Dakar"}


@pytest.fixture
def hear(tmp_path):
    main.S.settings = Settings.model_validate({"server": {"operator_pin": "boss", "public_url": "https://hear.test/"},
                                               "engine": {"token": "legacy-secret"}})
    main.S.local_addrs = set()
    main.S.operator = Hub("operator")
    db = Database(tmp_path / "t.db")
    hear_api.H = Hear(db, Mailer(tmp_path / "outbox.log"), Live(db, main.S.settings.stream, grace_s=0.4))
    yield hear_api.H
    hear_api.H = None


@pytest.fixture
def client(hear):
    return TestClient(main.app)


def verify_link(hear) -> str:
    text = hear.mailer.outbox.read_text()
    return "/api/hc/verify?" + text.rsplit("/api/hc/verify?", 1)[1].split()[0]


def make_cong(hear, name="Dakar Ward", email="a@example.org"):
    c = hear.db.create_congregation(church="LDS", name=name, unit_type="ward", email=email,
                                    meeting_times=[{"day": "sun", "time": "10:00"}])
    hear.db.set_verified(c["id"])
    return c, hear.db.add_device(c["id"], "Mac")


# ------------------------------------------------------------------ database

def test_db_slugs_hashes_and_sessions(tmp_path):
    db = Database(tmp_path / "x.db")
    assert slugify("Dakar Ward") == "dakar-ward" and slugify("Église d'Évry!") == "eglise-d-evry"
    a = db.create_congregation(church="c", name="Dakar Ward", unit_type="ward", email="A@x.org", lat=14.7, lng=-17.4)
    b = db.create_congregation(church="c", name="Dakar Ward", unit_type="ward", email="b@x.org")
    assert (a["slug"], b["slug"]) == ("dakar-ward", "dakar-ward-2")
    assert (a["lat"], a["lng"], b["lat"]) == (14.7, -17.4, None)
    assert db.by_email("a@x.org", verified_only=False)[0]["id"] == a["id"] and not db.by_email("a@x.org")
    token = db.add_device(a["id"], "Mac")
    assert token not in (tmp_path / "x.db").read_bytes().decode("latin-1")     # only the hash is stored
    assert hash_token(token) in (tmp_path / "x.db").read_bytes().decode("latin-1")
    assert db.device_by_token(token)["congregation_id"] == a["id"] and db.device_by_token("nope") is None
    sid = db.start_session(a["id"], ["fr"])
    assert db.sessions(a["id"])[0]["ended_at"] is None
    db2 = Database(tmp_path / "x.db")        # a restart closes sessions left open
    assert db2.sessions(a["id"])[0]["ended_at"] is not None and sid


def test_rate_limiter():
    r = RateLimiter(2, 60)
    assert r.allow("ip") and r.allow("ip") and not r.allow("ip") and r.allow("other")


def test_next_meeting():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    times = [{"day": "sun", "time": "10:00"}, {"day": "wed", "time": "19:00"}]
    sat = datetime(2026, 11, 7, 12, 0, tzinfo=ZoneInfo("Africa/Dakar"))
    assert next_meeting(times, "Africa/Dakar", sat)["label"] == "Sunday 10:00"
    sun_after = datetime(2026, 11, 8, 11, 0, tzinfo=ZoneInfo("Africa/Dakar"))
    assert next_meeting(times, "Africa/Dakar", sun_after)["label"] == "Wednesday 19:00"
    assert next_meeting(times, "", None)["starts"] is None and next_meeting([], "", None) is None


# ------------------------------------------------------------------ sign-up

def test_signup_verify_claim_flow(client, hear):
    r = client.post("/api/hc/signup", json={**SIGNUP, "lat": 14.69, "lng": -17.44})
    assert r.status_code == 200
    slug, claim = r.json()["slug"], r.json()["claim"]
    assert slug == "dakar-ward"
    cong = hear.db.by_slug(slug)
    assert not cong["email_verified"] and cong["email"] == "bishop@example.org" and cong["lat"] == 14.69
    assert "https://hear.test/api/hc/verify?token=" in hear.mailer.outbox.read_text()
    assert client.get(f"/c/{slug}").status_code == 404                        # not live-capable until verified
    pending = client.post("/api/hc/claim", json={"claim": claim})
    assert pending.status_code == 202 and pending.json() == {"pending": True}
    assert client.get(verify_link(hear)).status_code == 200
    assert hear.db.by_slug(slug)["email_verified"]
    got = client.post("/api/hc/claim", json={"claim": claim})
    token = got.json()["device_token"]
    assert got.status_code == 200 and hear.db.device_by_token(token)["congregation_id"] == cong["id"]
    assert client.post("/api/hc/claim", json={"claim": claim}).status_code == 409   # handed out once
    assert client.get("/api/hc/verify?token=bogus").status_code == 410
    assert client.get(f"/c/{slug}").status_code == 200
    assert client.post("/api/hc/signup", json={**SIGNUP}).json()["slug"] == "dakar-ward-2"


def test_signup_validation(client):
    bad = [{**SIGNUP, "email": "nope"}, {**SIGNUP, "unit_type": "cult"}, {**SIGNUP, "lat": 14.0},
           {**SIGNUP, "timezone": "Mars/Base"}, {**SIGNUP, "meeting_times": [{"day": "xx", "time": "10:00"}]}]
    codes = [client.post("/api/hc/signup", json=b).status_code for b in bad]
    assert codes == [422, 422, 400, 422, 422]


def test_add_another_mac_by_magic_link(client, hear):
    cong, _ = make_cong(hear, email="owner@example.org")
    r = client.post("/api/hc/device-link", json={"email": "owner@example.org", "device_name": "Second Mac"})
    claim = r.json()["claim"]
    assert client.post("/api/hc/claim", json={"claim": claim}).status_code == 202
    client.get(verify_link(hear))
    token = client.post("/api/hc/claim", json={"claim": claim}).json()["device_token"]
    assert hear.db.device_by_token(token)["name"] == "Second Mac" and len(hear.db.devices(cong["id"])) == 2
    # an unknown address gets the same answer and no e-mail
    before = hear.mailer.outbox.read_text()
    r2 = client.post("/api/hc/device-link", json={"email": "stranger@example.org"})
    assert r2.status_code == 200 and hear.mailer.outbox.read_text() == before
    # two congregations on one address: say which
    c2 = hear.db.create_congregation(church="LDS", name="Other Ward", unit_type="ward", email="owner@example.org")
    hear.db.set_verified(c2["id"])
    assert client.post("/api/hc/device-link", json={"email": "owner@example.org"}).status_code == 400
    assert client.post("/api/hc/device-link", json={"email": "owner@example.org", "slug": "other-ward"}).status_code == 200


def test_rate_limits(client, hear):
    codes = [client.post("/api/hc/signup", json={**SIGNUP, "name": f"Ward {i}"}).status_code for i in range(6)]
    assert codes == [200] * 5 + [429]
    make_cong(hear, email="m@example.org")
    codes = [client.post("/api/hc/device-link", json={"email": "m@example.org"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]                                     # per address: 3 an hour


def test_smtp_not_configured_writes_outbox(hear):
    hear.mailer.send("x@example.org", "Hello", "Body text")
    log = hear.mailer.outbox.read_text()
    assert "To: x@example.org" in log and "Body text" in log


# ------------------------------------------------------------------ admin

def test_admin_lists_and_hides(client, hear):
    cong, _ = make_cong(hear)
    assert client.get("/api/admin/congregations").status_code == 403
    assert client.get("/admin", follow_redirects=False).status_code == 303   # to the sign-in page
    assert client.get("/admin?key=boss").status_code == 200
    rows = client.get("/api/admin/congregations?key=boss").json()["congregations"]
    assert rows[0]["slug"] == "dakar-ward" and rows[0]["devices"] == 1 and not rows[0]["hidden"]
    assert client.post(f"/api/admin/congregations/{cong['id']}/hidden?key=boss", json={"hidden": True}).status_code == 200
    assert client.get("/c/dakar-ward").status_code == 404
    assert client.get("/api/admin/congregations?key=boss").json()["congregations"][0]["hidden"]
    client.post(f"/api/admin/congregations/{cong['id']}/hidden?key=boss", json={"hidden": False})
    assert client.get("/c/dakar-ward").status_code == 200
    assert client.post("/api/admin/congregations/999/hidden?key=boss", json={"hidden": True}).status_code == 404


def test_listener_page_is_versioned_and_scoped(client, hear):
    make_cong(hear)
    html = client.get("/c/dakar-ward").text
    assert "/static/listener/listener.js?v=" in html
    info = client.get("/api/hc/c/dakar-ward").json()
    assert info["live"] is False and info["next_meeting"]["label"] == "Sunday 10:00"
    assert client.get("/c/dakar-ward/stream/fr.mp3").status_code == 404      # not live
    assert client.get("/c/nope").status_code == 404


# ------------------------------------------------------------------ live sessions (real server)

@pytest.fixture
def server(hear):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield port
    srv.should_exit = True
    t.join(5)


async def engine(port, token, languages=("fr", "es")):
    st = main.S.settings.stream
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws/engine", additional_headers={"Authorization": f"Bearer {token}"})
    await ws.send(json.dumps({"type": "hello", "name": "Test Mac", "languages": list(languages),
                              "stream": {"sample_rate": st.sample_rate, "bitrate_kbps": st.bitrate_kbps}}))
    return ws


async def recv(ws, kind=None, timeout=3):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if kind is None or m.get("type") == kind:
            return m


async def start(ws, languages=("fr",), **extra):
    await ws.send(json.dumps({"type": "session_start", "languages": list(languages), **extra}))
    return await recv(ws)


async def listener(port, slug, lang="fr"):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/c/{slug}/ws/listen")
    await ws.send(json.dumps({"type": "hello", "lang": lang}))
    return ws, await recv(ws, "init")


async def test_engine_session_relays_captions_and_mp3(server, hear):
    cong, token = make_cong(hear)
    port = server
    ws_l, init = await listener(port, "dakar-ward")
    assert init["live"] is False and init["next_meeting"]["label"] == "Sunday 10:00" and init["languages"] == []
    eng = await engine(port, token)
    started = await start(eng, ["fr", "es"])
    assert started["type"] == "session_started" and not started["resumed"]
    assert (await recv(ws_l, "reload"))["type"] == "reload"                # waiting phones join the session
    assert hear.db.sessions(cong["id"])[0]["languages"] == ["fr", "es"]
    ws_l, init = await listener(port, "dakar-ward", "es")
    assert init["live"] and init["lang"] == "es" and [l["code"] for l in init["languages"]] == ["fr", "es"]
    assert init["session"] == started["session"]
    ws_fr, init_fr = await listener(port, "dakar-ward", "xx")              # unknown language → the first offered
    assert init_fr["lang"] == "fr"
    async with httpx.AsyncClient() as http:
        assert (await http.get(f"http://127.0.0.1:{port}/c/dakar-ward/stream/ru.mp3")).status_code == 404
        async with http.stream("GET", f"http://127.0.0.1:{port}/c/dakar-ward/stream/fr.mp3") as r:
            assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
            await eng.send(json.dumps({"type": "hub", "lang": "fr", "remember": True, "msg": {
                "type": "caption", "seq": 1, "session": "engine-own-id", "lang": "fr", "text": "Bonjour", "ts": 1}}))
            await eng.send(json.dumps({"type": "hub", "lang": "es", "remember": True, "msg": {
                "type": "caption", "seq": 1, "session": "x", "lang": "es", "text": "Hola", "ts": 1}}))
            await eng.send(b"\x00FRMP3DATA")                                # index 0 = fr
            await eng.send(b"\x01ESMP3DATA")
            it = r.aiter_bytes()
            chunk = await asyncio.wait_for(it.__anext__(), 3)
            assert chunk == b"FRMP3DATA"
            cap = await recv(ws_fr, "caption")
            assert cap["text"] == "Bonjour" and cap["session"] == started["session"]
            assert (await recv(ws_l, "caption"))["text"] == "Hola"          # the Spanish room only gets Spanish
            late, init_late = await listener(port, "dakar-ward", "fr")      # a late joiner sees what was said
            assert [h["text"] for h in init_late["history"]] == ["Bonjour"]
            await eng.send(json.dumps({"type": "session_stop"}))
            assert (await recv(ws_fr, "status"))["live"] is False
            await asyncio.wait_for(_drain(it), 3)                           # the stream ends with the session
    assert hear.live.get(cong["id"]) is None and hear.db.sessions(cong["id"])[0]["ended_at"] is not None
    assert hear.db.devices(cong["id"])[0]["last_seen"] is not None


async def _drain(it):
    async for _ in it:
        pass


async def test_one_live_session_per_congregation(server, hear):
    cong, token = make_cong(hear)
    other = hear.db.add_device(cong["id"], "Second Mac")
    a = await engine(server, token)
    assert (await start(a))["type"] == "session_started"
    b = await engine(server, other)
    refused = await start(b)
    assert refused["type"] == "session_refused" and "already live" in refused["message"]
    assert (await start(a))["type"] == "session_refused"                    # the same Mac can't start it twice
    await a.send(json.dumps({"type": "session_stop"}))
    await asyncio.sleep(0.2)
    assert (await start(b))["type"] == "session_started"                    # free again


async def test_drop_resumes_within_grace_and_ends_after(server, hear):
    cong, token = make_cong(hear)
    other = hear.db.add_device(cong["id"], "Second Mac")
    a = await engine(server, token)
    code = (await start(a))["session"]
    await a.close()
    await asyncio.sleep(0.1)
    assert hear.live.get(cong["id"]) is not None                            # kept during the grace period
    b = await engine(server, other)
    assert (await start(b))["type"] == "session_refused"                    # another Mac can't take over
    a2 = await engine(server, token)
    resumed = await start(a2, resume=code)
    assert resumed["type"] == "session_started" and resumed["resumed"] and resumed["session"] == code
    await asyncio.sleep(0.6)                                                # longer than the 0.4 s test grace
    assert hear.live.get(cong["id"]) is not None                            # the old link's timer was cancelled
    await a2.close()
    await asyncio.sleep(0.9)
    assert hear.live.get(cong["id"]) is None
    assert hear.db.sessions(cong["id"])[0]["ended_at"] is not None


async def test_hub_tells_the_engine_when_it_ends_the_session(server, hear):
    cong, token = make_cong(hear)
    a = await engine(server, token)
    assert (await start(a))["type"] == "session_started"
    hear.live.end(hear.live.get(cong["id"]), why="hidden")                  # e.g. the admin hides the congregation
    ended = await recv(a, "session_ended")
    assert ended["message"] == "hidden"
    await a.send(json.dumps({"type": "hub", "lang": "fr", "msg": {"type": "caption", "seq": 1, "text": "x"}}))
    assert (await start(a))["type"] == "session_started"                    # a fresh start is possible
    await a.send(json.dumps({"type": "session_stop"}))                      # the engine's own stop is not echoed back
    await asyncio.sleep(0.3)
    with pytest.raises(asyncio.TimeoutError):
        await recv(a, "session_ended", timeout=0.3)


async def test_bad_tokens_hidden_and_incompatible_streams(server, hear):
    cong, token = make_cong(hear)
    with pytest.raises(websockets.InvalidStatus) as e:     # refused before the upgrade, like the legacy path
        await websockets.connect(f"ws://127.0.0.1:{server}/ws/engine", additional_headers={"Authorization": "Bearer nope"})
    assert e.value.response.status_code == 403
    ws = await websockets.connect(f"ws://127.0.0.1:{server}/ws/engine", additional_headers={"Authorization": f"Bearer {token}"})
    await ws.send(json.dumps({"type": "hello", "languages": ["fr"], "stream": {"sample_rate": 8000, "bitrate_kbps": 8}}))
    assert (await recv(ws))["type"] == "session_refused"
    await ws.send(json.dumps({"type": "session_start", "languages": ["fr"]}))
    assert (await recv(ws))["type"] == "session_refused"
    hear.db.set_hidden(cong["id"], True)
    with pytest.raises(websockets.InvalidStatus):
        await engine(server, token)


# ------------------------------------------------------------------ legacy ward

async def test_legacy_engine_token_and_pages_unchanged(server, hear):
    # the legacy engine token still opens the old EngineHub link, and device tokens don't reach it
    ws = await websockets.connect(f"ws://127.0.0.1:{server}/ws/engine", additional_headers={"Authorization": "Bearer legacy-secret"})
    st = main.S.settings.stream
    await ws.send(json.dumps({"type": "hello", "name": "Mac", "languages": ["fr", "wo"], "busy": False,
                              "stream": {"sample_rate": st.sample_rate, "bitrate_kbps": st.bitrate_kbps}}))
    for _ in range(50):
        if main.S.engine_hub.connected and main.S.engine_hub.name == "Mac":
            break
        await asyncio.sleep(0.05)
    assert main.S.engine_hub.connected and main.S.engine_hub.languages == ["fr", "wo"] and main.S.engine_hub.compatible
    await ws.close()
    for _ in range(50):
        if not main.S.engine_hub.connected:
            break
        await asyncio.sleep(0.05)
    assert not main.S.engine_hub.connected
    async with httpx.AsyncClient(follow_redirects=False) as http:
        base = f"http://127.0.0.1:{server}"
        root = await http.get(base + "/")
        assert root.status_code == 200 and "/static/listener/listener.js?v=" in root.text
        assert (await http.get(base + "/operator")).status_code == 303      # sign-in still guards it
        assert (await http.get(base + "/login")).status_code == 200


def test_public_settings_hide_tokens():
    main.S.settings = Settings.model_validate({"engine": {"token": "t0k", "device_token": "d3v"}})
    d = main._public_settings()
    assert d["engine"]["token"] == "set" and d["engine"]["device_token"] == "set" and "d3v" not in str(d)


async def test_publish_client_publishes_a_locally_started_meeting(server, hear):
    """The Mac side: a meeting started locally appears as the congregation's session, captions and MP3 relay."""
    from types import SimpleNamespace as NS
    from app.engine import PublishClient

    cong, token = make_cong(hear)
    chans = {c: NS(hub=NS(taps=[]), broadcaster=NS(taps=[])) for c in ("fr", "es")}
    pipe = NS(running=False, started_at=0.0, channels=chans, on_channels={"fr": chans["fr"]})
    main.S.pipeline = pipe
    client = PublishClient(f"ws://127.0.0.1:{server}/ws/engine", token, "Test Mac")
    task = asyncio.create_task(client.run_forever())
    try:
        for _ in range(60):
            if client.connected:
                break
            await asyncio.sleep(0.05)
        assert client.connected and hear.live.get(cong["id"]) is None      # idle until a meeting is started here
        pipe.running, pipe.started_at = True, 1.0
        for _ in range(60):
            if hear.live.get(cong["id"]):
                break
            await asyncio.sleep(0.05)
        assert hear.live.get(cong["id"]).offered == ["fr"] and client.running
        ws, init = await listener(server, "dakar-ward")
        assert init["live"]
        for tap in chans["fr"].hub.taps:
            tap({"type": "caption", "seq": 1, "session": "mine", "lang": "fr", "text": "Salut", "ts": 1}, True)
        assert (await recv(ws, "caption"))["text"] == "Salut"
        pipe.on_channels = {"fr": chans["fr"], "es": chans["es"]}          # a language switched on mid-meeting
        assert [l["code"] for l in (await recv(ws, "languages"))["languages"]] == ["fr", "es"]
        # a second Mac can't publish while this one is live
        other = await engine(server, hear.db.add_device(cong["id"], "Other"))
        assert (await start(other))["type"] == "session_refused"
        pipe.running = False                                               # the meeting ends here
        assert (await recv(ws, "status"))["live"] is False
        assert hear.live.get(cong["id"]) is None and not client.running
    finally:
        task.cancel()
        del main.S.pipeline
