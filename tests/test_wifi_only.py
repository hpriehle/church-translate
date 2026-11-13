"""Credits on "this Wi-Fi only" meetings: the Mac opens a private billing session on the hub (never listed, never
reachable by phones there), meters the languages its own listeners hear, and keeps unsent reports on disk."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from app import main
from app.engine import PublishClient
from app.hear import live as hear_live
from app.translate.hub_session import HubClient, HubSession

from test_hear import client, engine, hear, listener, make_cong, recv, server, start  # noqa: F401  (fixtures)
from test_proxy import FRENCH, SPEECH, auth, claude, turn  # noqa: F401  (fixtures)


async def until(cond, tries=100):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def wifi_only(hear, monkeypatch):
    # The Mac and the hub are one test server here, and app mode would close the hub's routes to the "Mac" (they
    # stay on localhost in the app): so only the sharing setting is read, as private_wanted does in app mode.
    monkeypatch.setattr(PublishClient, "private_wanted", staticmethod(lambda: main.S.settings.server.sharing == "local"))
    main.S.settings.server.sharing = "local"
    main.S.settings.translate.backend = "hub"
    yield
    main.S.settings.server.sharing = "cloud"


# ------------------------------------------------------------------ the hub: a private session is a billing link only

async def test_a_private_session_is_metered_and_translates_but_is_never_public(server, hear, claude, client):
    cong, token = make_cong(hear)
    waiting, init = await listener(server, "dakar-ward", "fr")       # a phone on hearchurch.com/dakar-ward
    assert init["live"] is False
    ws = await engine(server, token)
    started = await start(ws, ["fr", "es"], private=True)
    assert started["type"] == "session_started" and not started["resumed"]
    code = started["session"]

    # nothing public: not live on the page, the search, the listener socket or the streams, no reload for phones
    assert client.get("/api/hc/c/dakar-ward").json()["live"] is False
    assert client.get("/api/hc/search", params={"q": "Dakar"}).json()["results"][0]["live"] is False
    assert client.get("/c/dakar-ward/stream/fr.mp3").status_code == 404
    phone, init = await listener(server, "dakar-ward", "fr")
    assert init["live"] is False and init["session"] is None and init["languages"] == []
    with pytest.raises(asyncio.TimeoutError):
        await recv(waiting, "reload", timeout=0.4)

    # captions or audio sent anyway are dropped; the hub sends no listener counts for it
    await ws.send(json.dumps({"type": "hub", "lang": "fr", "remember": True,
                              "msg": {"type": "caption", "text": "Bonjour", "seq": 1}}))
    await ws.send(bytes([0]) + b"\xff\xfb" + b"\0" * 100)
    s = hear.live.get_any(cong["id"])
    assert s.private and s.hubs["fr"].replay_after(0) == []

    # the meter goes through it like an online meeting's
    await ws.send(json.dumps({"type": "meter", "session": code, "items": [{"seq": 1, "s": 30.0, "langs": ["fr"]}]}))
    ok = await recv(ws, "meter_ok")
    assert ok["seqs"] == [1] and not ok["unknown"]
    assert hear.db.speech_by_language(cong["id"]) == {"fr": 30.0}

    # and translation on credit works while it runs (the proxy sees it)
    r = client.post("/api/hc/translate", json=turn(), headers=auth(token))
    assert r.status_code == 200 and r.json()["content"][1]["text"] == FRENCH

    # the operator's admin list sees it, marked Wi-Fi only
    row = client.get("/api/admin/congregations?key=boss").json()["congregations"][0]
    assert row["live"] is True and row["wifi_only"] is True

    await ws.send(json.dumps({"type": "session_stop"}))
    await until(lambda: hear.live.get_any(cong["id"]) is None)
    for w in (ws, waiting, phone):
        await w.close()


async def test_a_private_session_resumes_only_as_private(server, hear):
    cong, token = make_cong(hear)
    ws = await engine(server, token)
    code = (await start(ws, ["fr"], private=True))["session"]
    await ws.close()
    await until(lambda: hear.live.get_any(cong["id"]).link is None)
    ws = await engine(server, token)
    again = await start(ws, ["fr"], private=True, resume=code)
    assert again["resumed"] and again["session"] == code and hear.live.get(cong["id"]) is None
    await ws.close()


async def test_out_of_credit_refuses_a_wifi_only_meeting_too(server, hear):
    from test_proxy import spend

    cong, token = make_cong(hear)
    spend(hear, cong, 61)                                            # the free hour and a minute more
    ws = await engine(server, token)
    refused = await start(ws, ["fr"], private=True)
    assert refused["type"] == "session_refused" and "out of credit" in refused["message"]
    await ws.close()


# ------------------------------------------------------------------ the Mac, end to end over the real link

async def test_a_wifi_only_mac_meeting_translates_and_bills_its_own_listeners(server, hear, claude, client,
                                                                           wifi_only, monkeypatch):
    monkeypatch.setattr(PublishClient, "METER_EVERY_S", 0.2)
    monkeypatch.setattr(hear_live, "LISTENERS_EVERY_S", 0.1)
    cong, token = make_cong(hear)
    chans = {c: NS(hub=NS(taps=[], count=0), broadcaster=NS(taps=[], listeners=0)) for c in ("fr", "es")}
    pipe = NS(running=False, started_at=0.0, channels=chans, on_channels=dict(chans), speech_taps=[])
    main.S.pipeline = pipe
    mac = PublishClient(f"ws://127.0.0.1:{server}/ws/engine", token, "Test Mac")
    sent = []
    real_send = mac._send

    def spy(item):
        sent.append(item)
        real_send(item)
    mac._send = spy
    task = asyncio.create_task(mac.run_forever())
    try:
        await until(lambda: mac.connected)
        pipe.running, pipe.started_at = True, 1.0
        await until(lambda: mac.running)
        assert [m for m in sent if isinstance(m, dict) and m["type"] == "session_start"][0]["private"] is True
        assert hear.live.get(cong["id"]) is None and hear.live.get_any(cong["id"]).private   # what public routes see
        info = mac.info()
        assert info["live"] is False and info["billing_link"] is True and info["wifi_only"] is True

        chans["fr"].hub.count = 1                                    # a phone on this Mac's Wi-Fi reads French
        await until(lambda: mac.heard == {"fr"})
        await asyncio.sleep(0.3)                                     # hub listener counts (none) don't undo it
        assert mac.heard == {"fr"}

        # the engine's own taps: captions and audio would go out on a public session; on this one nothing does
        for tap in chans["fr"].hub.taps:
            tap({"type": "caption", "text": "Bonjour"}, True)
        for tap in chans["fr"].broadcaster.taps:
            tap(b"\xff\xfb" + b"\0" * 10)
        for tap in pipe.speech_taps:
            tap(1, 30.0, ["fr", "es"])                               # es is translated but nobody listens
        await until(lambda: not mac.meter and hear.db.speech_by_language(cong["id"]))
        assert hear.db.speech_by_language(cong["id"]) == {"fr": 30.0}
        assert not [m for m in sent if isinstance(m, bytes) or m.get("type") == "hub"]

        hs = HubSession("SYSTEM", "opus", "low", client=HubClient(f"http://127.0.0.1:{server}", token))
        await hs.start()
        await hs.ask("reply simply: OK")
        hs.ready = True
        assert await hs.ask(SPEECH) == FRENCH
        await hs.close()

        pipe.running = False
        await until(lambda: hear.live.get_any(cong["id"]) is None)
        assert hear.db.balance(cong["id"])["used_min"] == 0.5
    finally:
        task.cancel()
        del main.S.pipeline


async def test_switching_sharing_during_a_meeting_starts_a_new_session(server, hear, wifi_only):
    cong, token = make_cong(hear)
    chans = {"fr": NS(hub=NS(taps=[], count=0), broadcaster=NS(taps=[], listeners=0))}
    pipe = NS(running=False, started_at=0.0, channels=chans, on_channels=dict(chans), speech_taps=[])
    main.S.pipeline = pipe
    mac = PublishClient(f"ws://127.0.0.1:{server}/ws/engine", token, "Test Mac")
    task = asyncio.create_task(mac.run_forever())
    try:
        await until(lambda: mac.connected)
        pipe.running, pipe.started_at = True, 1.0
        await until(lambda: mac.running and mac.private)
        main.S.settings.server.sharing = "cloud"
        await until(lambda: hear.live.get(cong["id"]) is not None)   # now public
        assert mac.running and not mac.private
    finally:
        task.cancel()
        del main.S.pipeline


def test_wifi_only_means_local_sharing_in_the_app(hear, monkeypatch):
    main.S.settings.server.sharing = "local"
    monkeypatch.setattr(main.S, "app_mode", True, raising=False)
    assert PublishClient.private_wanted()
    main.S.settings.server.sharing = "cloud"
    assert not PublishClient.private_wanted()


# ------------------------------------------------------------------ the meter file

def test_unsent_meter_reports_survive_a_restart(tmp_path, hear):
    path = tmp_path / "data" / "meter.json"
    a = PublishClient("wss://hub.test/ws/engine", "tok", "Mac", meter_path=path)
    a._send = lambda item: None
    a.running, a.session_code, a.heard = True, "s1", {"fr"}
    a._speech(1, 8.0, ["fr"])
    a._speech(2, 6.0, ["fr", "es"])
    a._save_meter(force=True)
    assert path.exists()

    b = PublishClient("wss://hub.test/ws/engine", "tok", "Mac", meter_path=path)    # the app started again
    assert b.meter == {"s1": {1: {"seq": 1, "s": 8.0, "langs": ["fr"]}, 2: {"seq": 2, "s": 6.0, "langs": ["fr"]}}}
    sent = []
    b._send = sent.append
    b._send_meter()                                                  # goes out on the next link
    assert [i["seq"] for m in sent for i in m["items"]] == [1, 2]
    b._metered({"type": "meter_ok", "session": "s1", "seqs": [1, 2]})
    b._save_meter(force=True)
    assert PublishClient("wss://hub.test/ws/engine", "tok", "Mac", meter_path=path).meter == {}

    # another congregation's sign-in (or another hub) doesn't inherit them; a damaged file is ignored
    assert PublishClient("wss://hub.test/ws/engine", "other", "Mac", meter_path=path).meter == {}
    assert PublishClient("wss://other.test/ws/engine", "tok", "Mac", meter_path=path).meter == {}
    path.write_text("{not json")
    assert PublishClient("wss://hub.test/ws/engine", "tok", "Mac", meter_path=path).meter == {}


def test_the_meter_file_is_written_at_most_every_few_seconds(tmp_path, hear, monkeypatch):
    monkeypatch.setattr(main.S, "pipeline", NS(running=True), raising=False)
    path = tmp_path / "meter.json"
    c = PublishClient("wss://hub.test/ws/engine", "tok", "Mac", meter_path=path)
    c.running, c.session_code, c.heard = True, "s1", {"fr"}
    c._speech(1, 5.0, ["fr"])                                        # first one: written
    c._speech(2, 5.0, ["fr"])                                        # within METER_SAVE_S: waits
    assert len(json.loads(path.read_text())["meter"]["s1"]) == 1
    c._finish(lost=True)                                             # the link dropped: written now
    assert len(json.loads(path.read_text())["meter"]["s1"]) == 2


# ------------------------------------------------------------------ offline

def test_without_internet_the_mac_says_why_nothing_is_translated(hear, wifi_only):
    c = PublishClient("wss://hub.test/ws/engine", "tok", "Mac")
    c.connected = False
    assert "No internet" in c.info()["notice"] and "Wi-Fi" in c.info()["notice"]
    c.connected = True
    assert c.info()["notice"] == ""
    c.connected = False
    main.S.settings.translate.backend = "api"                        # own key: not about credits
    assert c.info()["notice"] == ""
