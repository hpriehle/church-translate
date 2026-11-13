"""Go-live check: the new hub on a database made by the code that is deployed today.

The database is built by `app/hear/db.py` as it is on origin/scripture-lookup (the deployed hub), taken from git into
a temp dir, with congregations, Macs, sign-in links and sessions (one still open, as if the hub stopped mid-meeting).
Then the new code opens it and serves it: every congregation that was there is grandfathered on its own key, no row
or value is lost, the billing tables appear, and an old Mac (no meter, no billing call) keeps meeting as before."""
import asyncio
import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.broadcast.hub import Hub
from app.config import Settings
from app.hear import api as hear_api
from app.hear.api import Hear
from app.hear.db import Database
from app.hear.live import Live
from app.hear.mail import Mailer

from test_hear import FakeGeocoder, engine, recv, server, start  # noqa: F401  (fixtures)

DEPLOYED = "origin/scripture-lookup"
ROOT = Path(__file__).resolve().parents[1]


def deployed_db_module(tmp_path):
    """The deployed hub's db.py, loaded under another name (it only needs the standard library)."""
    try:
        src = subprocess.run(["git", "show", f"{DEPLOYED}:app/hear/db.py"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"{DEPLOYED} isn't in this clone (git fetch origin)")
    f = tmp_path / "deployed_hear_db.py"
    f.write_text(src)
    spec = importlib.util.spec_from_file_location("deployed_hear_db", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dump(path):
    """Every row of every table, as plain dicts keyed by table."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    out = {t: [dict(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}
    con.close()
    return out


@pytest.fixture
def legacy(tmp_path):
    """A hub database as the deployed code leaves it, and what it held."""
    old = deployed_db_module(tmp_path)
    assert "billing" not in old.SCHEMA                                     # really the pre-credits code
    path = tmp_path / "data" / "hearchurch.db"
    db = old.Database(path)
    harlem = db.create_congregation(church="LDS", name="Harlem Ward", unit_type="ward", email="Harlem@x.org",
                                    stake="New York Stake", address="128 Lenox Ave, New York, NY 10026",
                                    lat=40.80, lng=-73.95, meeting_times=[{"day": "sun", "time": "10:00"}],
                                    timezone="America/New_York")
    db.set_verified(harlem["id"])
    dakar = db.create_congregation(church="LDS", name="Dakar Ward", unit_type="ward", email="d@x.org",
                                   address="Rue 10, Dakar, Senegal", lat=14.7, lng=-17.4)
    unverified = db.create_congregation(church="Baptist", name="Grace Chapel", unit_type="church", email="g@x.org")
    db.set_verified(dakar["id"])
    harlem_token = db.add_device(harlem["id"], "Harlem Mac")
    dakar_token = db.add_device(dakar["id"], "Dakar Mac")
    db.add_device(dakar["id"], "Dakar Mac 2")
    connect_code = db.new_connect_code(dakar["id"])
    db.add_email_token(unverified["id"], "signup", "Mac", 3600)
    s1 = db.start_session(harlem["id"], ["fr", "es"])
    db.end_session(s1)
    db.start_session(dakar["id"], ["wo"])
    db.end_session(db.start_session(dakar["id"], ["fr"]))
    db.start_session(harlem["id"], ["fr"])                                  # open: the hub stops mid-meeting
    db._db.close()
    return {"path": path, "before": dump(path), "harlem": harlem, "dakar": dakar, "unverified": unverified,
            "harlem_token": harlem_token, "dakar_token": dakar_token, "connect_code": connect_code}


@pytest.fixture
def hear(legacy, tmp_path):
    """The new hub's registry opened on that database (as app/main.py does at start), like test_hear's fixture."""
    main.S.settings = Settings.model_validate({"server": {"operator_pin": "boss", "public_url": "https://hear.test/"}})
    main.S.local_addrs = set()
    main.S.operator = Hub("operator")
    db = Database(legacy["path"])
    hear_api.H = Hear(db, Mailer(tmp_path / "outbox.log"), Live(db, main.S.settings.stream, grace_s=0.4),
                      geocoder=FakeGeocoder())
    yield hear_api.H
    hear_api.H = None


def test_upgrade_keeps_every_row_and_grandfathers_every_congregation(legacy, hear):
    before, after = legacy["before"], dump(legacy["path"])
    assert {"speech", "credits", "own_key_codes"} <= set(after) and not {"speech", "credits"} & set(before)
    for table, rows in before.items():                                   # no row and no old value lost
        assert len(after[table]) == len(rows), table
        for old_row, new_row in zip(rows, after[table]):
            changed = {k for k, v in old_row.items() if new_row[k] != v}
            # the one open session is ended at start, as the deployed hub does after a restart
            assert changed <= ({"ended_at"} if table == "sessions" and old_row["ended_at"] is None else set()), \
                (table, changed)
    assert all(c["billing"] == "own_key" for c in after["congregations"])
    assert all(s["billing"] == "own_key" and s["code"] is None and s["device_id"] is None for s in after["sessions"])
    assert all(s["ended_at"] for s in after["sessions"])
    assert after["speech"] == [] and after["credits"] == [] and after["own_key_codes"] == []
    db = hear.db
    for c in after["congregations"]:
        assert db.balance(c["id"])["can_start"] and db.balance(c["id"])["bought_usd"] == 0
    assert db.device_by_token(legacy["harlem_token"])["congregation_id"] == legacy["harlem"]["id"]
    assert db.connect_code_ok(legacy["dakar"]["id"], legacy["connect_code"])
    # a congregation signing up after the deploy is on credits; opening again grandfathers nobody else
    new = db.create_congregation(church="LDS", name="Accra Ward", unit_type="ward", email="a@x.org")
    assert new["billing"] == "credits"
    assert Database(legacy["path"], close_stale=False).congregation(new["id"])["billing"] == "credits"


def test_old_macs_keep_working_against_the_new_hub(legacy, hear, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-used")             # the hub's key is set at go-live
    c = TestClient(main.app)
    auth = {"Authorization": f"Bearer {legacy['harlem_token']}"}
    b = c.get("/api/hc/billing", headers=auth).json()
    assert b["billing"] == "own_key"
    assert c.post("/api/hc/checkout", json={"usd": 10}, headers=auth).status_code in (409, 503)  # nothing to buy
    assert c.post("/api/hc/translate", json={}, headers=auth).status_code == 409   # own key: no proxy
    j = c.post("/api/hc/connect", json={"slug": "dakar-ward", "code": legacy["connect_code"],
                                        "device_name": "Third Mac"}).json()
    assert j["ok"] and j["billing"] == "own_key"                         # a Mac joining later stays on own key
    rows = {r["slug"]: r for r in c.get("/api/admin/congregations?key=boss").json()["congregations"]}
    assert {r["billing"] for r in rows.values()} == {"own_key"} and rows["harlem-ward"]["devices"] == 1
    assert rows["harlem-ward"]["last_session"] == legacy["before"]["sessions"][-1]["started_at"]
    assert c.get("/api/hc/search?q=harlem").status_code == 200


def test_rollback_the_deployed_code_still_runs_on_the_upgraded_database(legacy, hear, tmp_path):
    """Rolling the hub back after the upgrade: the old code opens the new database and works. A congregation that
    signs up while rolled back gets the column default, 'credits', and a later roll-forward doesn't grandfather it
    again (the column exists): the runbook says to set those to own_key by hand."""
    hear.db._db.close()
    old = deployed_db_module(tmp_path)
    db = old.Database(legacy["path"])
    assert [c["slug"] for c in db.list_congregations()] == ["harlem-ward", "dakar-ward", "grace-chapel"]
    assert db.device_by_token(legacy["dakar_token"])
    db.end_session(db.start_session(legacy["dakar"]["id"], ["fr"]))
    late = db.create_congregation(church="LDS", name="Rollback Ward", unit_type="ward", email="r@x.org")
    db._db.close()
    again = Database(legacy["path"])
    assert again.congregation(legacy["dakar"]["id"])["billing"] == "own_key"
    assert again.congregation(late["id"])["billing"] == "credits"
    assert again.sessions(legacy["dakar"]["id"])[-1]["billing"] == "credits"


async def test_an_old_mac_starts_a_meeting_with_no_credit_and_no_meter(server, legacy, hear):
    """0.1.0 sends session_start without meter reports and never asks for billing: own key, never refused."""
    ws = await engine(server, legacy["dakar_token"], languages=("fr",))
    m = await start(ws, ["fr"])
    assert m["type"] == "session_started", m
    assert hear.live.get(legacy["dakar"]["id"]) is not None
    await ws.send(json.dumps({"type": "session_stop"}))
    await asyncio.sleep(0.2)
    await ws.close()
    s = hear.db.sessions(legacy["dakar"]["id"])[-1]
    assert s["billing"] == "own_key" and s["code"] == m["session"]
    assert hear.db.balance(legacy["dakar"]["id"])["used_min"] == 0
