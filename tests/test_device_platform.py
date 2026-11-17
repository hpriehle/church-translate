"""Devices record what they run on (mac-arm, mac-intel, windows): at sign-in and in the engine's hello. The admin
endpoints and /api/hc/account (the /account device list) carry it as `platform` (null when unknown)."""
import asyncio
import json
import sqlite3

from app.hear.db import Database, device_platform
from test_hear import client, engine, hear, make_cong, server  # noqa: F401  (fixtures)

K = "?key=boss"   # the operator PIN of test_hear's hear fixture


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_only_known_platforms_are_kept():
    assert [device_platform(v) for v in ("mac-arm", " Windows ", "mac-intel")] == ["mac-arm", "windows", "mac-intel"]
    assert [device_platform(v) for v in ("linux", "", None, 3, "windows" * 10)] == [None] * 5


def test_pairing_records_the_platform_and_the_lists_show_it(client, hear):
    cong, token = make_cong(hear)
    code = client.post("/api/hc/pairing-code", headers=auth(token)).json()["code"]
    p = client.post("/api/hc/pair", json={"code": code, "device_name": "OFFICE-PC", "platform": "windows"}).json()
    code = client.post("/api/hc/pairing-code", headers=auth(token)).json()["code"]
    client.post("/api/hc/pair", json={"code": code, "device_name": "Old Mac"})          # an older app: no platform
    code = client.post("/api/hc/pairing-code", headers=auth(token)).json()["code"]
    client.post("/api/hc/pair", json={"code": code, "device_name": "Odd", "platform": "amiga"})
    by_name = {d["name"]: d["platform"] for d in hear.db.devices(cong["id"])}
    assert by_name == {"Mac": None, "OFFICE-PC": "windows", "Old Mac": None, "Odd": None}

    acct = client.get("/api/hc/account", headers=auth(p["device_token"])).json()
    assert {d["name"]: d["platform"] for d in acct["devices"]}["OFFICE-PC"] == "windows"

    j = client.get(f"/api/admin/congregations/{cong['id']}{K}").json()
    assert {d["name"]: d["platform"] for d in j["devices"]}["OFFICE-PC"] == "windows"
    j = client.get(f"/api/admin/congregations/{cong['id']}/account{K}").json()
    assert "windows" in json.dumps(j)


def test_connect_code_records_the_platform(client, hear):
    cong, _ = make_cong(hear)
    code = hear.db.new_connect_code(cong["id"])
    r = client.post("/api/hc/connect", json={"slug": cong["slug"], "code": code, "device_name": "PC",
                                              "platform": "windows"})
    assert r.status_code == 200, r.text
    assert {d["name"]: d["platform"] for d in hear.db.devices(cong["id"])}["PC"] == "windows"


async def test_hello_records_the_platform(server, hear):
    cong, token = make_cong(hear)
    ws = await engine(server, token)
    from app import main
    await ws.send(json.dumps({"type": "hello", "name": "Mac", "languages": ["fr"], "app_version": "0.4.0",
                              "platform": "mac-intel",
                              "stream": {"sample_rate": main.S.settings.stream.sample_rate,
                                         "bitrate_kbps": main.S.settings.stream.bitrate_kbps}}))
    for _ in range(50):
        if hear.db.devices(cong["id"])[0]["platform"]:
            break
        await asyncio.sleep(0.05)
    await ws.close()
    d, = hear.db.devices(cong["id"])
    assert d["platform"] == "mac-intel" and d["app_version"] == "0.4.0"
    hear.db.touch_device(d["id"], "0.4.1")              # a hello without a platform keeps the one it knows
    assert hear.db.devices(cong["id"])[0]["platform"] == "mac-intel"
    assert hear.db.all_devices()[0]["platform"] == "mac-intel"


def test_engine_hello_says_the_platform(monkeypatch):
    from app import engine as eng
    monkeypatch.setenv("HEARCHURCH_PLATFORM", "windows")
    assert eng.device_platform() == "windows"
    monkeypatch.delenv("HEARCHURCH_PLATFORM")
    monkeypatch.setattr(eng.sys, "platform", "win32")
    assert eng.device_platform() == "windows"
    monkeypatch.setattr(eng.sys, "platform", "darwin")
    import platform
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    assert eng.device_platform() == "mac-arm"
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    assert eng.device_platform() == "mac-intel"
    import inspect
    assert '"platform": device_platform()' in inspect.getsource(eng)


def test_the_windows_app_sends_windows():
    from pathlib import Path
    core = Path(__file__).parent.parent / "windows" / "HearChurch" / "Core"
    model, hub = (core / "AppModel.cs").read_text(), (core / "HubClient.cs").read_text()
    assert 'public const string Platform = "windows";' in model and '["HEARCHURCH_PLATFORM"] = Platform' in model
    assert '["platform"] = AppModel.Platform' in hub


def test_old_databases_get_the_platform_column(tmp_path):
    path = tmp_path / "old.db"
    Database(path)._db.close()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE devices DROP COLUMN platform")
    con.commit()
    con.close()
    db = Database(path)
    c = db.create_congregation(church="LDS", name="X Ward", unit_type="ward", email="x@example.org",
                               meeting_times=[{"day": "sun", "time": "10:00"}])
    db.add_device(c["id"], "PC", "windows")
    assert db.devices(c["id"])[0]["platform"] == "windows"
