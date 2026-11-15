"""Hear Church V3 (dev-13 feedback): a congregation changes its listener link (old slugs redirect for good), the
operator switches its billing mode, and the Mac's Billing tab reads the usage history."""
from tests.test_hear import client, hear, make_cong  # noqa: F401  (fixtures)

from app.hear.db import RESERVED_SLUGS


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_change_slug_redirects_the_old_one_forever(client, hear):
    cong, token = make_cong(hear)
    r = client.post("/api/hc/slug", json={"slug": "Dakar-First"}, headers=bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "dakar-first" and r.json()["aliases"] == ["dakar-ward"]
    assert r.json()["url"].endswith("/dakar-first")
    old = client.get("/dakar-ward", follow_redirects=False)
    assert old.status_code == 301 and old.headers["location"] == "/dakar-first"
    assert client.get("/c/dakar-ward").status_code == 200            # /c/old → /old → /new
    assert client.get("/dakar-first").status_code == 200
    assert client.get("/api/hc/c/dakar-ward").json()["slug"] == "dakar-first"   # a page opened before the move
    # a second move keeps both old slugs pointing at the newest one
    client.post("/api/hc/slug", json={"slug": "dakar-centre"}, headers=bearer(token))
    for s in ("dakar-ward", "dakar-first"):
        assert client.get(f"/{s}", follow_redirects=False).headers["location"] == "/dakar-centre"
    # taking back an old slug is fine; the current one becomes an alias
    r = client.post("/api/hc/slug", json={"slug": "dakar-ward"}, headers=bearer(token)).json()
    assert r["slug"] == "dakar-ward" and sorted(r["aliases"]) == ["dakar-centre", "dakar-first"]
    assert client.get("/dakar-ward").status_code == 200
    # sign-up never hands out an old slug of someone else
    other = hear.db.create_congregation(church="c", name="Dakar First", unit_type="ward", email="x@x.org")
    assert other["slug"] == "dakar-first-2"


def test_change_slug_validates(client, hear):
    cong, token = make_cong(hear)
    other, other_token = make_cong(hear, name="Thies Ward", email="t@example.org")
    client.post("/api/hc/slug", json={"slug": "thies-old"}, headers=bearer(other_token))
    client.post("/api/hc/slug", json={"slug": "thies-ward"}, headers=bearer(other_token))   # thies-old is an alias now

    def code(slug):
        r = client.post("/api/hc/slug", json={"slug": slug}, headers=bearer(token))
        return r.status_code, r.json().get("error")

    assert code("source") == (409, "reserved")
    assert "source" in RESERVED_SLUGS and "admin" in RESERVED_SLUGS
    assert code("admin") == (409, "reserved")
    assert code("thies-ward") == (409, "taken")
    assert code("thies-old") == (409, "taken")        # someone else's old link keeps working for them
    for bad in ("ab", "-dakar", "dakar-", "dakar--ward", "dakar ward", "dakar_ward", "dakar/ward", "é" * 4, "a" * 61):
        assert code(bad) == (422, "bad_slug"), bad
    assert client.post("/api/hc/slug", json={"slug": "dakar-ward"}, headers=bearer(token)).json()["aliases"] == []
    assert client.post("/api/hc/slug", json={"slug": "new-one"}).status_code == 403
    assert client.post("/api/hc/slug", json={"slug": "new-one"}, headers=bearer("nope")).status_code == 403
    assert hear.db.by_slug("dakar-ward")["id"] == cong["id"]


def test_admin_sees_aliases(client, hear):
    cong, token = make_cong(hear)
    client.post("/api/hc/slug", json={"slug": "dakar-first"}, headers=bearer(token))
    row, = client.get("/api/admin/congregations?key=boss").json()["congregations"]
    assert row["slug"] == "dakar-first" and row["aliases"] == ["dakar-ward"]
    one = client.get(f"/api/admin/congregations/{cong['id']}?key=boss").json()
    assert one["congregation"]["aliases"] == ["dakar-ward"]


def test_admin_switches_billing_mode(client, hear):
    cong, token = make_cong(hear)
    url = f"/api/admin/congregations/{cong['id']}/billing"
    assert client.post(url, json={"mode": "own_key"}).status_code == 403            # operator only
    r = client.post(url + "?key=boss", json={"mode": "own_key"})
    assert r.status_code == 200 and r.json()["billing"]["billing"] == "own_key"
    assert client.get("/api/hc/billing", headers=bearer(token)).json()["billing"] == "own_key"
    assert client.post(url + "?key=boss", json={"mode": "credits"}).json()["billing"]["billing"] == "credits"
    assert client.get("/api/hc/billing", headers=bearer(token)).json()["billing"] == "credits"
    assert client.post(url + "?key=boss", json={"mode": "free"}).status_code == 400
    assert client.post("/api/admin/congregations/999/billing?key=boss", json={"mode": "credits"}).status_code == 404


KNOWS = {"X-HC-Billing-Types": "credits,own_key,internal"}


def test_admin_sets_the_billing_type_with_the_billing_field(client, hear):
    """Siteboard sends {"billing": …}; the three types, 400 (not 422) for anything else, 404 unknown congregation."""
    cong, token = make_cong(hear)
    url = f"/api/admin/congregations/{cong['id']}/billing?key=boss"
    assert client.post(url.split("?")[0], json={"billing": "internal"}).status_code == 403   # operator only
    for t in ("own_key", "internal", "credits"):
        r = client.post(url, json={"billing": t})
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["billing"]["billing"] == t
        assert hear.db.congregation(cong["id"])["billing"] == t
    for bad in ({"billing": "free"}, {"billing": 3}, {"billing": None}, {}, {"mode": "Internal"}):
        r = client.post(url, json=bad)
        assert r.status_code == 400, (bad, r.status_code)
        assert "credits, own_key, internal" in r.json()["detail"]
    assert client.post("/api/admin/congregations/999/billing?key=boss", json={"billing": "internal"}).status_code == 404
    # "billing" wins over the older "mode" when both come
    r = client.post(url, json={"billing": "internal", "mode": "own_key"})
    assert r.json()["billing"]["billing"] == "internal"
    # the operator's views show the type
    row, = client.get("/api/admin/congregations?key=boss").json()["congregations"]
    assert row["billing"] == "internal"
    one = client.get(f"/api/admin/congregations/{cong['id']}?key=boss").json()
    assert one["congregation"]["billing"] == "internal" and one["billing"]["billing"] == "internal"


def test_internal_billing_as_the_mac_sees_it(client, hear):
    cong, token = make_cong(hear)
    hear.db.set_billing(cong["id"], "internal")
    b = client.get("/api/hc/billing", headers={**bearer(token), **KNOWS}).json()
    assert b["billing"] == "internal" and b["message"] == "Included: no charge."
    assert b["can_buy"] is False and b["out_of_credit"] is False and b["language_hours_left"] is None
    assert b["balance"]["can_start"] is True
    # an app that doesn't know "internal" (0.3.0 builds 18/19) is told "credits": it translates through the hub,
    # with nothing to buy and the plain message
    old = client.get("/api/hc/billing", headers=bearer(token)).json()
    assert old["billing"] == "credits" and old["can_buy"] is False and old["out_of_credit"] is False
    assert old["message"] == "Included: no charge."
    # never on the public pages
    for path in (f"/api/hc/c/{cong['slug']}", f"/{cong['slug']}"):
        r = client.get(path)
        assert r.status_code == 200 and "internal" not in r.text.lower() and "billing" not in r.text.lower()
    # no credit can be bought for it
    r = client.post("/api/hc/checkout", json={"usd": 20}, headers={**bearer(token), **KNOWS})
    assert r.status_code in (409, 503)


def test_internal_meetings_are_metered_at_no_cost_and_never_refused(client, hear):
    from app.hear import admin

    cong, token = make_cong(hear)
    hear.db.set_billing(cong["id"], "internal")
    sid = hear.db.start_session(cong["id"], ["fr"], code="m1", billing="internal")
    hear.db.record_speech(cong["id"], "m1", [{"seq": i, "s": 60, "langs": ["fr"]} for i in range(120)])  # 2 h
    hear.db.end_session(sid)
    bal = hear.db.balance(cong["id"])
    assert bal["metered_min"] == 120 and bal["used_min"] == 0 and bal["free_left_min"] == 60
    assert hear.db.can_start(hear.db.congregation(cong["id"]))
    j = client.get("/api/hc/usage", headers={**bearer(token), **KNOWS}).json()
    m, = j["meetings"]
    assert j["billing"] == "internal" and m["billing"] == "internal" and m["cost_usd"] == 0 and m["language_hours"] == 2
    old = client.get("/api/hc/usage", headers=bearer(token)).json()
    assert old["billing"] == "credits" and old["meetings"][0]["billing"] == "credits"
    assert old["meetings"][0]["cost_usd"] == 0                       # the old app shows "free"
    row, = [r for r in admin.usage(hear) if r["session_id"] == sid]
    assert row["billing"] == "internal" and row["cost_usd"] == 0 and row["amount_cents"] == 0
    stats = client.get("/api/admin/stats?key=boss").json()
    assert stats["all_time"]["language_hours"]["internal"] == 2 and stats["all_time"]["language_hours"]["paid"] == 0
    assert stats["congregations"]["by_billing"] == {"internal": 1}


def test_usage_history_for_the_billing_tab(client, hear):
    cong, token = make_cong(hear)
    sid = hear.db.start_session(cong["id"], ["fr", "es"], code="m1")
    hear.db.record_speech(cong["id"], "m1", [{"seq": i, "s": 60, "langs": ["fr", "es"]} for i in range(45)])
    hear.db.end_session(sid)
    hear.db.add_credit(cong["id"], "pi_test", 2500)
    r = client.get("/api/hc/usage", headers=bearer(token))
    assert r.status_code == 200
    j = r.json()
    assert j["billing"] == "credits"
    m, = j["meetings"]
    assert m["language_hours"] == 1.5 and m["free_minutes"] == 60 and m["paid_minutes"] == 30 and m["cost_usd"] == 2.5
    assert m["language_minutes"] == {"es": 45, "fr": 45}
    assert j["charges"] == [{"kind": "top_up", "at": j["charges"][0]["at"], "amount_usd": 25.0, "hours": 5.0}]
    assert client.get("/api/hc/usage").status_code == 403


def test_a_speed_keeps_the_voice_and_a_voice_keeps_the_speed():
    """Settings → Speaking sends {"voices": {"es": {"speed": 1.2}}}; choosing a voice sends engine and voice only."""
    import pytest
    from pydantic import ValidationError

    from app.config import Settings, VoiceCfg
    from app.main import merged_voice

    s = Settings()
    es = merged_voice(s, "es", {"speed": 1.2})
    assert es == {"engine": "piper", "voice": "es_MX-ald-medium", "speed": 1.2}
    s = Settings(voices={"es": VoiceCfg(**es)})
    assert merged_voice(s, "es", {"engine": "piper", "voice": "es_ES-other"})["speed"] == 1.2
    with pytest.raises(ValidationError):
        merged_voice(s, "es", {"speed": 3})
