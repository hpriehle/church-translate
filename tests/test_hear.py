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


class FakeGeocoder:
    """Stands in for app.hear.geo.Geocoder: answers from a dict, remembers what it was asked."""

    def __init__(self, known=None):
        self.known, self.asked = known or {}, []

    async def geocode(self, address):
        self.asked.append(address)
        return self.known.get(address)

    async def town(self, lat, lng):
        return "New York, NY" if (round(lat), round(lng)) == (41, -74) else ""


@pytest.fixture
def hear(tmp_path):
    main.S.settings = Settings.model_validate({"server": {"operator_pin": "boss", "public_url": "https://hear.test/"},
                                               "engine": {"token": "legacy-secret"}})
    main.S.local_addrs = set()
    main.S.operator = Hub("operator")
    db = Database(tmp_path / "t.db")
    hear_api.H = Hear(db, Mailer(tmp_path / "outbox.log"), Live(db, main.S.settings.stream, grace_s=0.4),
                      geocoder=FakeGeocoder())   # never the real Nominatim
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


def test_signup_without_email_verification_hands_out_the_token(client, hear):
    hear.verify_email = False
    r = client.post("/api/hc/signup", json={**SIGNUP, "lat": 14.69, "lng": -17.44}).json()
    assert "claim" not in r and r["congregation"]["name"] == SIGNUP["name"]
    assert hear.db.device_by_token(r["device_token"])["congregation_id"] == hear.db.by_slug(r["slug"])["id"]
    assert hear.db.by_slug(r["slug"])["email_verified"] and not hear.mailer.outbox.exists()
    assert client.get(f"/c/{r['slug']}").status_code == 200


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


# ------------------------------------------------------------------ location (phase 2)

from app.hear import geo as hear_geo  # noqa: E402


def _mock_http(monkeypatch, handler):
    """Route the Geocoder's httpx traffic to `handler(request) -> httpx.Response` (no network)."""
    real = httpx.AsyncClient
    monkeypatch.setattr(hear_geo.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


async def test_nominatim_geocoder_policy_cache_and_failures(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        q = req.url.params["q"]
        if q == "boom":
            return httpx.Response(503)
        return httpx.Response(200, json=[{"lat": "40.7128", "lon": "-74.0060"}] if q != "nowhere" else [])

    _mock_http(monkeypatch, handler)
    g = hear_geo.Geocoder(mapbox_token="", min_interval_s=0.2)
    assert g.provider == "nominatim"
    assert await g.geocode("1 Main St, New York") == (40.7128, -74.006)
    assert seen[0].headers["user-agent"] == "HearChurch/1.0 (hearchurch.com)"
    assert seen[0].url.host == "nominatim.openstreetmap.org" and seen[0].url.params["limit"] == "1"
    assert await g.geocode("  1 MAIN st,  new york ") == (40.7128, -74.006) and len(seen) == 1   # cached
    t0 = time.monotonic()
    assert await g.geocode("nowhere") is None            # not found is cached too
    assert await g.geocode("nowhere") is None and len(seen) == 2
    assert time.monotonic() - t0 >= 0.19                 # at most one request per interval
    assert await g.geocode("boom") is None               # a provider error gives None...
    assert await g.geocode("boom") is None and len(seen) == 4   # ...and is not cached, so it can be retried
    assert await g.geocode("x") is None and len(seen) == 4      # too short to ask about


async def test_mapbox_geocoder_when_token_set(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"features": [{"center": [-111.89, 40.76]}]})

    _mock_http(monkeypatch, handler)
    monkeypatch.setenv("HEARCHURCH_MAPBOX_TOKEN", "pk.test")
    g = hear_geo.Geocoder(min_interval_s=0)
    assert g.provider == "mapbox"
    assert await g.geocode("50 N Temple, Salt Lake City") == (40.76, -111.89)   # mapbox gives lng, lat
    assert seen[0].url.host == "api.mapbox.com" and seen[0].url.params["access_token"] == "pk.test"
    assert "50%20N%20Temple" in str(seen[0].url)


def test_signup_geocodes_and_never_blocks(client, hear):
    hear.geocoder.known["1 Rue Exemple, Dakar"] = (14.69, -17.44)
    r = client.post("/api/hc/signup", json=SIGNUP).json()
    assert r["located"] and (r["lat"], r["lng"]) == (14.69, -17.44)
    assert (hear.db.by_slug(r["slug"])["lat"], hear.db.by_slug(r["slug"])["lng"]) == (14.69, -17.44)
    # not found: sign-up still works, the answer says a pin is needed
    r = client.post("/api/hc/signup", json={**SIGNUP, "name": "Other Ward", "address": "Nowhere 1", "email": "o@example.org"}).json()
    assert r["ok"] and not r["located"] and r["lat"] is None and "pin" in r["message"]
    assert hear.db.by_slug(r["slug"])["lat"] is None
    # a pin that came with the request is kept and nothing is looked up
    hear.geocoder.asked.clear()
    r = client.post("/api/hc/signup", json={**SIGNUP, "name": "Pinned Ward", "lat": 1.5, "lng": 2.5, "email": "p@example.org"}).json()
    assert r["located"] and (r["lat"], r["lng"]) == (1.5, 2.5) and hear.geocoder.asked == []


def test_signup_survives_a_broken_geocoder(client, hear):
    async def boom(address):
        raise RuntimeError("provider down")

    hear.geocoder.geocode = boom
    r = client.post("/api/hc/signup", json=SIGNUP)
    assert r.status_code == 200 and r.json()["located"] is False


def test_location_update_needs_a_device_token(client, hear):
    cong, token = make_cong(hear)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/hc/location", json={"lat": 1, "lng": 2}).status_code == 403
    assert client.post("/api/hc/location", json={"lat": 1, "lng": 2}, headers={"Authorization": "Bearer nope"}).status_code == 403
    r = client.post("/api/hc/location", json={"lat": 40.7, "lng": -74.0, "address": "5 Pine St"}, headers=auth)
    assert r.status_code == 200 and r.json()["lat"] == 40.7
    c = hear.db.congregation(cong["id"])
    assert (c["lat"], c["lng"], c["address"]) == (40.7, -74.0, "5 Pine St")
    client.post("/api/hc/location", json={"lat": 41.0, "lng": -73.0}, headers=auth)   # no address: the old one stays
    assert hear.db.congregation(cong["id"])["address"] == "5 Pine St"
    assert client.post("/api/hc/location", json={"lat": 41.0}, headers=auth).status_code == 400
    assert client.post("/api/hc/location", json={}, headers=auth).status_code == 400
    assert client.post("/api/hc/location", json={"lat": 99, "lng": 0}, headers=auth).status_code == 422
    hear.geocoder.known["9 Elm St"] = (10.0, 20.0)
    r = client.post("/api/hc/location", json={"address": "9 Elm St"}, headers=auth)
    assert (r.json()["lat"], r.json()["lng"]) == (10.0, 20.0) and hear.db.congregation(cong["id"])["address"] == "9 Elm St"
    assert client.post("/api/hc/location", json={"address": "Atlantis"}, headers=auth).status_code == 422
    hear.db.set_hidden(cong["id"], True)
    assert client.post("/api/hc/location", json={"lat": 1, "lng": 2}, headers=auth).status_code == 403


def place(hear, name, lat, lng, *, verified=True, hidden=False, times=None, tz="", stake=None):
    c = hear.db.create_congregation(church="LDS", name=name, unit_type="ward", email=f"{slugify(name)}@x.org", stake=stake,
                                    lat=lat, lng=lng, meeting_times=times or [], timezone=tz)
    if verified:
        hear.db.set_verified(c["id"])
    if hidden:
        hear.db.set_hidden(c["id"], True)
    return c


def go_live(hear, cong, languages=("fr",)):
    link = type("L", (), {"device": {"id": 1}, "name": "Mac", "send": lambda self, m: None, "session": None})()
    s = hear_live_session(hear, cong, link, list(languages))
    return s


def hear_live_session(hear, cong, link, languages):
    from app.hear.live import LiveSession

    s = LiveSession(cong, link, hear.db.start_session(cong["id"], languages), "20261102-100000", languages, languages,
                    main.S.settings.stream)
    hear.live.sessions[cong["id"]] = s
    return s


def test_live_lists_congregations_within_a_mile_live_first(client, hear):
    near = place(hear, "Near Ward", 40.7000, -74.0000)
    place(hear, "Dark Ward", 40.7001, -74.0)                        # right here, not live: still listed (wait page)
    place(hear, "Mid Ward", 40.7000 + 3 / 69, -74.0)                # ~3 miles north: too far
    r = client.get("/api/live", params={"lat": 40.7, "lng": -74.0})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    j = r.json()
    assert j["radius_mi"] == 1 and [x["slug"] for x in j["results"]] == ["dark-ward", "near-ward"]
    go_live(hear, near)
    j = client.get("/api/live", params={"lat": 40.7, "lng": -74.0}).json()
    assert [x["slug"] for x in j["results"]] == ["near-ward", "dark-ward"]     # live first
    row = j["results"][0]
    assert row["live"] and row["name"] == "Near Ward" and row["title"] == "Near Ward" and row["church"] == "LDS"
    assert (row["lat"], row["lng"]) == (40.7, -74.0) and row["languages"] == [{"code": "fr", "name": "Français"}]
    assert row["starts_at"] > 0
    assert set(row) >= {"slug", "name", "title", "city", "church", "unit_type", "lat", "lng", "languages", "live",
                        "starts_soon", "starts_at"}


def test_live_hides_hidden_and_unverified_and_unlocated(client, hear):
    ok = place(hear, "Ok Ward", 40.7, -74.0)
    for c in (place(hear, "Hidden Ward", 40.7, -74.0, hidden=True), place(hear, "Unverified Ward", 40.7, -74.0, verified=False),
              place(hear, "Nowhere Ward", None, None), ok):
        go_live(hear, c)
    j = client.get("/api/live", params={"lat": 40.7, "lng": -74.0}).json()
    assert [x["slug"] for x in j["results"]] == ["ok-ward"]


def test_live_flags_meetings_starting_within_30_minutes(client, hear):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    def at(minutes):
        t = datetime.fromtimestamp(now.timestamp() + minutes * 60, timezone.utc)
        return [{"day": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][t.weekday()], "time": t.strftime("%H:%M")}]
    place(hear, "Soon Ward", 40.7, -74.0, times=at(15), tz="UTC")
    place(hear, "Later Ward", 40.7, -74.0, times=at(90), tz="UTC")
    place(hear, "Notz Ward", 40.7, -74.0, times=at(15))              # no time zone: start unknown
    j = client.get("/api/live", params={"lat": 40.7, "lng": -74.0}).json()
    assert [(x["slug"], x["live"], x["starts_soon"]) for x in j["results"]] == [
        ("soon-ward", False, True), ("later-ward", False, False), ("notz-ward", False, False)]
    assert abs(j["results"][0]["starts_at"] - (now.timestamp() + 900)) < 120


def test_live_validates_and_rate_limits_per_ip(client, hear):
    assert client.get("/api/live").status_code == 422
    assert client.get("/api/live", params={"lat": 95, "lng": 0}).status_code == 422
    assert client.get("/api/live", params={"lat": "x", "lng": 0}).status_code == 422
    hear.limits["live"] = RateLimiter(3, 60)
    codes = [client.get("/api/live", params={"lat": 1, "lng": 1}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    other = client.get("/api/live", params={"lat": 1, "lng": 1}, headers={"cf-connecting-ip": "9.9.9.9"})
    assert other.status_code == 200                                                      # another IP is unaffected


def test_live_works_across_the_date_line(client, hear):
    c = place(hear, "Fiji Ward", -17.7, -179.995)
    go_live(hear, c)
    j = client.get("/api/live", params={"lat": -17.7, "lng": 179.995}).json()   # ~0.7 mile, across ±180°
    assert [x["slug"] for x in j["results"]] == ["fiji-ward"]


def test_access_log_never_carries_the_position(hear):
    import logging

    class Grab(logging.Handler):
        def __init__(self):
            super().__init__(logging.INFO)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    grab, lg = Grab(), logging.getLogger("uvicorn.access")
    lg.addHandler(grab)
    old = lg.level
    lg.setLevel(logging.INFO)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="info", access_log=True, log_config=None))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    try:
        for _ in range(100):
            if srv.started:
                break
            time.sleep(0.05)
        assert httpx.get(f"http://127.0.0.1:{port}/api/live?lat=40.123456&lng=-74.654321").status_code == 200
        assert httpx.get(f"http://127.0.0.1:{port}/api/hc/search?q=abc").status_code == 200
    finally:
        srv.should_exit = True
        t.join(5)
        lg.removeHandler(grab)
        lg.setLevel(old)
    joined = "\n".join(grab.lines)
    assert "/api/live" in joined and "40.1234" not in joined and "74.6543" not in joined and "lat=" not in joined
    assert "/api/hc/search?q=abc" in joined      # other paths are logged as usual


def test_search_by_name_verified_and_visible_only(client, hear):
    a = place(hear, "Brooklyn First Ward", 40.65, -73.95, stake="Brooklyn Stake")
    place(hear, "Brooklyn Second Ward", 40.66, -73.96)
    place(hear, "Queens Ward", 40.7, -73.8, stake="Brooklyn Heights Stake")
    place(hear, "Secret Brooklyn Ward", 40.7, -73.8, hidden=True)
    place(hear, "Unverified Brooklyn Ward", 40.7, -73.8, verified=False)
    place(hear, "Percent 100% Ward", None, None)
    names = [x["name"] for x in client.get("/api/hc/search", params={"q": "brooklyn"}).json()["results"]]
    assert names == ["Brooklyn First Ward", "Brooklyn Second Ward", "Queens Ward"]       # name matches first, stake too
    assert [x["name"] for x in client.get("/api/hc/search", params={"q": "  FIRST  ward"}).json()["results"]] == ["Brooklyn First Ward"]
    assert client.get("/api/hc/search", params={"q": "%"}).json()["results"] == []         # wildcards are literal
    assert [x["name"] for x in client.get("/api/hc/search", params={"q": "100%"}).json()["results"]] == ["Percent 100% Ward"]
    assert client.get("/api/hc/search", params={"q": "b"}).json()["results"] == []         # too short
    go_live(hear, a)
    first = client.get("/api/hc/search", params={"q": "brooklyn"}).json()["results"][0]
    assert first["live"] and first["slug"] == "brooklyn-first-ward" and first["lat"] == 40.65
    for i in range(12):
        place(hear, f"Many {i:02d} Ward", 1, 1)
    assert len(client.get("/api/hc/search", params={"q": "many"}).json()["results"]) == 10      # limited
    hear.limits["search"] = RateLimiter(1, 60)
    client.get("/api/hc/search", params={"q": "many"})
    assert client.get("/api/hc/search", params={"q": "many"}).status_code == 429


def test_find_page_and_legacy_root_unchanged(client, hear):
    r = client.get("/find")
    assert r.status_code == 200 and "/static/find/find.js?v=" in r.text
    assert client.get("/static/find/find.js").status_code == 200
    root = client.get("/")   # any address but hearchurch.com: the ward's own listening page, as before
    assert root.status_code == 200 and "/static/listener/listener.js?v=" in root.text and "find.js" not in root.text


async def test_hub_reports_listener_counts_to_the_engine(server, hear, monkeypatch):
    from app.hear import live as hear_live

    monkeypatch.setattr(hear_live, "LISTENERS_EVERY_S", 0.1)
    _cong, token = make_cong(hear)
    eng = await engine(server, token)
    await start(eng, ["fr"])
    ws_l, _ = await listener(server, "dakar-ward")
    m = await recv(eng, "listeners")
    assert m == {"type": "listeners", "stream": 0, "captions": 1}
    await ws_l.close()
    assert (await recv(eng, "listeners"))["captions"] == 0                 # only sent again when it changes


# ------------------------------------------------------------------ finding a congregation on hearchurch.com

def test_congregation_page_is_at_its_slug_and_the_old_link_redirects(client, hear):
    make_cong(hear, "Harlem First")
    assert client.get("/harlem-first").status_code == 200
    assert "listener.js" in client.get("/harlem-first").text
    r = client.get("/c/harlem-first", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/harlem-first"
    assert client.get("/nobody-here").status_code == 404
    assert client.get("/Harlem-First").status_code == 404                       # slugs are lowercase
    # The slug route comes last: the site's own pages still answer.
    assert "find.js" in client.get("/find").text
    assert client.get("/login").status_code == 200 and "listener.js" not in client.get("/login").text


def test_hearchurch_com_opens_the_finder_and_the_ward_address_keeps_its_page(client, hear):
    assert "find.js" in client.get("/", headers={"host": "hearchurch.com"}).text
    assert "find.js" in client.get("/", headers={"host": "www.hearchurch.com"}).text
    assert "listener.js" in client.get("/", headers={"host": "live.clntacq.com"}).text


def test_search_lists_name_and_town_while_typing(client, hear):
    c = hear.db.create_congregation(church="LDS", name="Harlem First", unit_type="ward", email="h@x.org",
                                    address="267 Lenox Ave, New York, NY 10027, USA")
    hear.db.set_verified(c["id"])
    make_cong(hear, "Dakar Branch")
    rows = client.get("/api/hc/search", params={"q": "Har"}).json()["results"]
    assert [(r["title"], r["city"], r["slug"]) for r in rows] == [("Harlem First Ward", "New York, NY", "harlem-first")]
    assert [r["slug"] for r in client.get("/api/hc/search", params={"q": "new york"}).json()["results"]] == ["harlem-first"]
    assert client.get("/api/hc/search", params={"q": "dakar"}).json()["results"][0]["title"] == "Dakar Branch"


def test_city_comes_from_the_address():
    from app.hear.db import city_of

    assert city_of("267 Lenox Ave, New York, NY 10027, USA") == "New York, NY"
    assert city_of("New York, NY") == "New York, NY"
    assert city_of("Rue 10, Dakar, Senegal") == "Dakar, Senegal"
    assert city_of("12 rue X, 75001 Paris, France") == "Paris, France"
    assert city_of("12 Main St, Springfield") == "Springfield"
    assert city_of("") == city_of("Harlem") == ""


def test_slugs_never_take_a_site_path(hear):
    from app.hear.db import RESERVED_SLUGS

    def paths(routes):
        for r in routes:
            inner = getattr(r, "original_router", None)
            if inner is not None:
                yield from paths(inner.routes)
            elif getattr(r, "path", None):
                yield r.path
    tops = {p.strip("/").split("/")[0].split(".")[0] for p in paths(main.app.routes)} - {"", "{slug}"}
    assert {slugify(t) for t in tops} <= RESERVED_SLUGS, {slugify(t) for t in tops} - RESERVED_SLUGS
    c = hear.db.create_congregation(church="LDS", name="Find", unit_type="ward", email="f@x.org")
    assert c["slug"] == "find-2"


def test_old_database_gets_the_town_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE congregations (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE,"
               " church TEXT NOT NULL, name TEXT NOT NULL, unit_type TEXT NOT NULL, stake TEXT, address TEXT NOT NULL"
               " DEFAULT '', lat REAL, lng REAL, meeting_times TEXT NOT NULL DEFAULT '[]', timezone TEXT NOT NULL"
               " DEFAULT '', email TEXT NOT NULL, email_verified INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,"
               " hidden INTEGER NOT NULL DEFAULT 0)")
    db.execute("INSERT INTO congregations (slug, church, name, unit_type, address, email, created_at)"
               " VALUES ('a', 'LDS', 'A', 'ward', '1 Main St, Dakar, Senegal', 'a@x.org', 0)")
    db.commit()
    db.close()
    assert Database(path).by_slug("a")["city"] == "Dakar, Senegal"


def test_town_comes_from_the_pin_at_sign_up(client, hear):
    body = {"church": "LDS", "name": "Harlem First", "unit_type": "ward", "email": "h@x.org", "address": "360 Malcolm X",
            "lat": 40.81, "lng": -73.94}
    slug = client.post("/api/hc/signup", json=body).json()["slug"]
    assert hear.db.by_slug(slug)["city"] == "New York, NY"


def test_town_from_nominatim_address_parts():
    from app.hear.geo import town_from_nominatim

    assert town_from_nominatim({"city": "New York", "state": "New York", "ISO3166-2-lvl4": "US-NY",
                                "country": "United States", "country_code": "us"}) == "New York, NY"
    assert town_from_nominatim({"city": "Dakar", "country": "Senegal", "country_code": "sn"}) == "Dakar, Senegal"
    assert town_from_nominatim({"country": "Senegal"}) == ""


async def test_older_congregations_get_their_town_from_the_pin(hear):
    c = place(hear, "Old Ward", 40.81, -73.94)
    hear.db.set_town(c["id"], "")
    await hear_api.backfill_towns()
    assert hear.db.by_slug("old-ward")["city"] == "New York, NY"
