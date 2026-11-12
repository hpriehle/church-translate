"""Billing: new congregations pay with credits; a one-time code Harrison makes switches one to its own key;
congregations from before billing are grandfathered as own key."""
import sqlite3

from app.hear import codes
from app.hear.db import Database, normalize_own_key_code

from test_hear import client, hear, make_cong, SIGNUP  # noqa: F401  (pytest fixtures)


def admin_code(client, label="Dakar Ward"):
    r = client.post("/api/admin/own-key-codes?key=boss", json={"label": label})
    assert r.status_code == 200
    return r.json()["code"]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_codes_look_right_and_forgive_typing():
    db = Database(":memory:")
    code = db.mint_own_key_code("Dakar Ward")
    assert code.startswith("HC-") and len(code) == 17
    assert normalize_own_key_code(code.lower().replace("-", " ")) == code.replace("-", "")[2:]
    assert normalize_own_key_code(code.replace("HC-", "")) == code.replace("-", "")[2:]
    assert normalize_own_key_code("HC-0000-1111-IIII") is None      # letters that are never in a code
    assert normalize_own_key_code("") is None and normalize_own_key_code("HC-ABC") is None


def test_only_the_hash_is_kept():
    db = Database(":memory:")
    code = db.mint_own_key_code("Dakar Ward")
    dump = "\n".join(db._db.iterdump())
    assert code not in dump and code.replace("-", "")[2:] not in dump


def test_a_new_congregation_pays_with_credits(client, hear):
    hear.verify_email = False
    j = client.post("/api/hc/signup", json=SIGNUP).json()
    assert client.get("/api/hc/billing", headers=bearer(j["device_token"])).json() == {"billing": "credits"}


def test_a_code_switches_one_congregation_to_its_own_key_once(client, hear):
    code = admin_code(client)
    assert client.post("/api/hc/own-key-code/check", json={"code": code}).json() == {"ok": True}
    assert client.post("/api/hc/own-key-code/check", json={"code": code}).status_code == 200   # checking uses nothing up
    a, ta = make_cong(hear, "Dakar Ward", "a@x.org")
    b, tb = make_cong(hear, "Thies Branch", "b@x.org")
    r = client.post("/api/hc/own-key-code", json={"code": code.lower()}, headers=bearer(ta))
    assert r.status_code == 200 and r.json() == {"ok": True, "billing": "own_key"}
    assert client.get("/api/hc/billing", headers=bearer(ta)).json() == {"billing": "own_key"}
    # a retry from the same congregation is fine; anyone else is refused, and the code no longer checks out
    assert client.post("/api/hc/own-key-code", json={"code": code}, headers=bearer(ta)).status_code == 200
    r = client.post("/api/hc/own-key-code", json={"code": code}, headers=bearer(tb))
    assert r.status_code == 403 and r.json() == {"error": "bad_code"}
    assert client.get("/api/hc/billing", headers=bearer(tb)).json() == {"billing": "credits"}
    assert client.post("/api/hc/own-key-code/check", json={"code": code}).status_code == 403
    used = client.get("/api/admin/own-key-codes?key=boss").json()["codes"]
    assert used[0]["label"] == "Dakar Ward" and used[0]["used_by"] == "dakar-ward" and "code" not in used[0]


def test_a_second_mac_learns_the_billing_when_it_connects(client, hear):
    hear.verify_email = False
    j = client.post("/api/hc/signup", json=SIGNUP).json()
    client.post("/api/hc/own-key-code", json={"code": admin_code(client)}, headers=bearer(j["device_token"]))
    r = client.post("/api/hc/connect", json={"slug": j["slug"], "code": j["connect_code"]})
    assert r.json()["billing"] == "own_key"


def test_unknown_codes_and_lockout(client, hear):
    _, token = make_cong(hear)
    assert client.post("/api/hc/own-key-code", json={"code": "HC-AAAA-BBBB-CCCC"}, headers=bearer(token)).status_code == 403
    assert client.post("/api/hc/own-key-code", json={"code": "x"}).status_code == 403          # no device token
    for _ in range(9):
        assert client.post("/api/hc/own-key-code/check", json={"code": "HC-AAAA-BBBB-CCCC"}).status_code == 403
    good = admin_code(client)
    r = client.post("/api/hc/own-key-code/check", json={"code": good})
    assert r.status_code == 429 and r.json()["error"] == "locked"    # even a good code, while locked
    other_ip = {"cf-connecting-ip": "10.1.2.3"}
    assert client.post("/api/hc/own-key-code/check", json={"code": good}, headers=other_ip).status_code == 200


def test_only_the_operator_mints_codes(client, hear):
    assert client.post("/api/admin/own-key-codes", json={"label": "Dakar Ward"}).status_code == 403
    assert client.get("/api/admin/own-key-codes").status_code == 403
    assert client.post("/api/admin/own-key-codes?key=boss", json={"label": ""}).status_code == 422


def test_admin_list_shows_billing(client, hear):
    make_cong(hear)
    assert client.get("/api/admin/congregations?key=boss").json()["congregations"][0]["billing"] == "credits"


def test_congregations_from_before_billing_keep_their_own_key(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)   # a database as the hub had it before credits: no billing column
    old.executescript("""
        CREATE TABLE congregations (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE, church TEXT NOT NULL,
          name TEXT NOT NULL, unit_type TEXT NOT NULL, stake TEXT, address TEXT NOT NULL DEFAULT '', lat REAL, lng REAL,
          meeting_times TEXT NOT NULL DEFAULT '[]', timezone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL,
          email_verified INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, hidden INTEGER NOT NULL DEFAULT 0,
          city TEXT NOT NULL DEFAULT '', connect_code_hash TEXT, connect_code_at REAL);
        INSERT INTO congregations (slug, church, name, unit_type, email, created_at)
          VALUES ('harlem', 'LDS', 'Harlem', 'ward', 'h@x.org', 1);""")
    old.commit()
    old.close()
    db = Database(path)
    assert db.by_slug("harlem")["billing"] == "own_key"
    new = db.create_congregation(church="LDS", name="Dakar Ward", unit_type="ward", email="d@x.org")
    assert new["billing"] == "credits"
    assert Database(path).by_slug("dakar-ward")["billing"] == "credits"   # reopening doesn't grandfather again


def test_cli_mints_and_lists_without_ending_a_live_meeting(tmp_path, capsys):
    path = tmp_path / "hub.db"
    db = Database(path)
    c = db.create_congregation(church="LDS", name="Dakar Ward", unit_type="ward", email="d@x.org")
    sid = db.start_session(c["id"], ["fr"])
    assert codes.main(["--db", str(path), "mint", "Dakar Ward"]) == 0
    code = capsys.readouterr().out.strip()
    assert db.own_key_code_ok(code)
    assert db._q("SELECT ended_at FROM sessions WHERE id = ?", (sid,))[0]["ended_at"] is None
    assert codes.main(["--db", str(path), "list"]) == 0
    assert "Dakar Ward  (unused)" in capsys.readouterr().out
    assert codes.main(["--db", str(path), "mint", "  "]) == 2
