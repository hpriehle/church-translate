"""hearchurch.com/start and /account (app/hear/web.py): sign-up with an e-mailed code, sign-in by code, the session
cookie (expiry, sign-out, revocation), CSRF, no probing of addresses, and every account action through the Mac's own
routes. Stripe and Resend are fakes (never the network); Photon and the map run only in the browser."""
import re
import time
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from app import main
from app.hear import web
from app.hear.db import WEB_CODE_TRIES

from test_accounts import FakeResend
from test_cards_codes import FakeStripe, new_code, pi_succeeded, post_event, save_card
from test_hear import client, hear, make_cong  # noqa: F401  (fixtures)
from test_stripe import keys  # noqa: F401  (fixture)

BODY = {"church": "The Church of Jesus Christ of Latter-day Saints", "name": "Maple Grove Ward", "unit_type": "ward",
        "stake": "Maple Stake", "address": "1 Main St, Maple Grove, MN", "lat": 45.07, "lng": -93.45,
        "meeting_times": [{"day": "sun", "time": "09:00"}], "timezone": "America/Chicago",
        "email": "Bishop@Example.org", "emails": ["clerk@example.org"]}


@pytest.fixture
def resend(hear):
    fake = FakeResend()
    hear.resend = fake
    return fake


@pytest.fixture
def fake(monkeypatch, keys):  # noqa: F811
    from app.hear import api as hear_api

    f = FakeStripe()
    f.v1.payment_intents.retrieve = lambda pid, params=None: (
        f.calls.append(("pi.retrieve", pid, params)) or NS(id=pid, latest_charge=NS(receipt_url=f"https://pay.stripe.com/receipts/{pid}")))
    monkeypatch.setattr(hear_api, "_stripe_client", lambda key: f)
    return f


@pytest.fixture
def browser(hear):
    """A browser on https://hearchurch.test (the cookies are Secure, so plain http would never send them back)."""
    return TestClient(main.app, base_url="https://hearchurch.test")


def page_token(b, path="/account") -> str:
    r = b.get(path)
    assert r.status_code == 200, r.text
    return re.search(r'<meta name="csrf" content="([^"]+)">', r.text).group(1)


def H(token, **more):
    return {"X-CSRF-Token": token, **more}


def last_code(resend, to=None) -> str:
    sent = [s for s in resend.sent if to is None or s[0] == to]
    return re.search(r"\n    (\d{6})\n", sent[-1][2]).group(1)


def sign_up(b, resend, body=BODY, **more) -> dict:
    t = page_token(b, "/start")
    r = b.post("/api/hc/web/signup-code", json={"email": body["email"]}, headers=H(t))
    assert r.status_code == 200, r.text
    r = b.post("/api/hc/web/signup", json={**body, **more, "email_code": last_code(resend)}, headers=H(t))
    assert r.status_code == 200, r.text
    return r.json()


def sign_in(b, resend, email="a@example.org") -> str:
    """Sign `b` in by e-mailed code; returns the session's CSRF token."""
    t = page_token(b)
    assert b.post("/api/hc/web/login-code", json={"email": email}, headers=H(t)).status_code == 200
    r = b.post("/api/hc/web/login", json={"email": email, "code": last_code(resend, email)}, headers=H(t))
    assert r.status_code == 200, r.text
    return r.json()["csrf"]


def cookie_header(r, name) -> str:
    return next(v for v in r.headers.get_list("set-cookie") if v.startswith(name + "="))


# ------------------------------------------------------------------ pages

def test_pages_are_served_by_the_hub_with_a_csrf_cookie(browser, hear):
    for path in ("/start", "/start/done", "/account"):
        r = browser.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        assert '<meta name="csrf" content="' in r.text and "{{csrf}}" not in r.text
        c = cookie_header(r, "hc_csrf").lower()
        assert "httponly" in c and "secure" in c and "samesite=strict" in c
    assert "Get started" in browser.get("/start").text and 'href="/account"' in browser.get("/start").text
    for f in ("account/account.css", "account/common.js", "account/start.js", "account/account.js",
              "vendor/leaflet/leaflet.js"):
        assert browser.get(f"/static/{f}").status_code == 200
    # OSM's tile servers block requests without a Referer, and these pages send Referrer-Policy: same-origin
    assert 'referrerPolicy: "strict-origin-when-cross-origin"' in browser.get("/static/account/common.js").text


def test_start_and_account_can_never_be_a_congregations_slug(hear):
    from app.hear.db import RESERVED_SLUGS

    assert {"start", "account"} <= RESERVED_SLUGS
    c = hear.db.create_congregation(church="c", name="Start", unit_type="ward", email="s@example.org")
    assert c["slug"] == "start-2"


# ------------------------------------------------------------------ sign-up

def test_sign_up_happy_path_then_the_mac_signs_in_with_the_email(browser, client, hear, resend):  # noqa: F811
    t = page_token(browser, "/start")
    r = browser.post("/api/hc/web/signup-code", json={"email": "Bishop@Example.org"}, headers=H(t))
    assert r.status_code == 200 and "bishop@example.org" in r.json()["message"]
    (to, subject, text), = resend.sent
    assert to == "bishop@example.org" and "finish signing up" in text
    r = browser.post("/api/hc/web/signup", json={**BODY, "email_code": last_code(resend)}, headers=H(t))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["slug"] == "maple-grove-ward" and out["email"] == "bishop@example.org" and out["located"] is True
    s = cookie_header(r, "hc_session").lower()
    assert "httponly" in s and "secure" in s and "samesite=lax" in s and "max-age=2592000" in s

    cong = hear.db.by_slug("maple-grove-ward")
    assert cong["email_verified"] and cong["billing"] == "credits" and (cong["lat"], cong["lng"]) == (45.07, -93.45)
    assert cong["meeting_times"] == [{"day": "sun", "time": "09:00"}] and cong["stake"] == "Maple Stake"
    assert [(e["email"], e["main"]) for e in hear.db.emails(cong["id"])] == [("bishop@example.org", True),
                                                                            ("clerk@example.org", False)]
    assert hear.db.devices(cong["id"]) == []        # no Mac yet: the app signs in with the e-mail

    me = browser.get("/api/hc/web/me").json()
    assert me["signed_in"] and me["email"] == "bishop@example.org" and me["congregation"]["slug"] == "maple-grove-ward"
    assert browser.get("/api/hc/billing").json()["free_hours_left"] == 1

    # the Mac: "Email me a code" → the code → a device token
    assert client.post("/api/hc/email-code", json={"email": "bishop@example.org"}).status_code == 200
    code = re.search(r"\n    ([A-Z0-9]{4}-[A-Z0-9]{4})\n", resend.sent[-1][2]).group(1)
    p = client.post("/api/hc/pair", json={"code": code, "device_name": "Chapel Mac"})
    assert p.status_code == 200 and p.json()["slug"] == "maple-grove-ward"


def test_sign_up_needs_the_right_email_code(browser, hear, resend):
    t = page_token(browser, "/start")
    browser.post("/api/hc/web/signup-code", json={"email": "bishop@example.org"}, headers=H(t))
    good = last_code(resend)
    wrong = "000000" if good != "000000" else "111111"
    r = browser.post("/api/hc/web/signup", json={**BODY, "email_code": wrong}, headers=H(t))
    assert r.status_code == 403 and r.json()["error"] == "bad_email_code"
    assert hear.db.by_slug("maple-grove-ward") is None and "hc_session" not in browser.cookies
    for _ in range(WEB_CODE_TRIES - 1):
        browser.post("/api/hc/web/signup", json={**BODY, "email_code": wrong}, headers=H(t))
    r = browser.post("/api/hc/web/signup", json={**BODY, "email_code": good}, headers=H(t))
    assert r.status_code == 403   # too many wrong tries: that code is dead
    browser.post("/api/hc/web/signup-code", json={"email": "bishop@example.org"}, headers=H(t))
    assert browser.post("/api/hc/web/signup", json={**BODY, "email_code": last_code(resend)},
                        headers=H(t)).status_code == 200


def test_sign_up_with_a_code_and_a_bad_code_first(browser, client, hear, resend):  # noqa: F811
    code = new_code(client, type="free_hours", value=3)["code"]
    t = page_token(browser, "/start")
    browser.post("/api/hc/web/signup-code", json={"email": "bishop@example.org"}, headers=H(t))
    email_code = last_code(resend)
    r = browser.post("/api/hc/web/signup", json={**BODY, "code": "ZZZZ-ZZZZ", "email_code": email_code}, headers=H(t))
    assert r.status_code == 403 and r.json()["error"] == "bad_code"
    assert hear.db.by_slug("maple-grove-ward") is None
    r = browser.post("/api/hc/web/signup", json={**BODY, "code": code, "email_code": email_code}, headers=H(t))
    assert r.status_code == 200, r.text   # the e-mail code wasn't used up by the typo
    assert r.json()["code"]["type"] == "free_hours"
    assert browser.get("/api/hc/billing").json()["paid_hours_left"] == 3


def test_sign_up_then_add_a_card_comes_back_to_the_last_page(browser, hear, resend, fake):
    out = sign_up(browser, resend)
    assert out["can_save_card"] is True
    r = browser.post("/api/hc/card/setup?back=start", json={}, headers=H(out["csrf"]))
    assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.com/")
    _, params = fake.of("session")[-1]
    assert params["mode"] == "setup" and params["success_url"] == "https://hear.test/start/done"


# ------------------------------------------------------------------ sign-in

def test_login_code_answers_the_same_for_unknown_addresses(browser, hear, resend):
    make_cong(hear)
    t = page_token(browser)
    known = browser.post("/api/hc/web/login-code", json={"email": "a@example.org"}, headers=H(t))
    unknown = browser.post("/api/hc/web/login-code", json={"email": "nobody@example.org"}, headers=H(t))
    assert known.status_code == unknown.status_code == 200
    assert known.json()["message"].replace("a@example.org", "X") == unknown.json()["message"].replace("nobody@example.org", "X")
    assert [s[0] for s in resend.sent] == ["a@example.org"]
    # and a guess for an unknown address gets the same error as a wrong code for a known one
    w1 = browser.post("/api/hc/web/login", json={"email": "nobody@example.org", "code": "123456"}, headers=H(t))
    good = last_code(resend)
    w2 = browser.post("/api/hc/web/login", json={"email": "a@example.org",
                                                 "code": "000000" if good != "000000" else "111111"}, headers=H(t))
    assert w1.status_code == w2.status_code == 403 and w1.json() == w2.json()


def test_email_code_login_and_its_limits(browser, hear, resend):
    cong, _ = make_cong(hear)
    csrf = sign_in(browser, resend)
    me = browser.get("/api/hc/web/me").json()
    assert me["signed_in"] and me["csrf"] == csrf and me["congregation"]["id"] == cong["id"]
    t = page_token(browser)
    # the code works once
    used = last_code(resend)
    assert browser.post("/api/hc/web/login", json={"email": "a@example.org", "code": used}, headers=H(t)).status_code == 403
    # the Mac flow's limits: 3 codes per address per hour (one already went out above)
    codes = [browser.post("/api/hc/web/login-code", json={"email": "a@example.org"}, headers=H(t)).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_wrong_codes_lock_the_ip(browser, hear, resend):
    make_cong(hear)
    t = page_token(browser)
    for _ in range(10):
        browser.post("/api/hc/web/login", json={"email": "x@example.org", "code": "123456"}, headers=H(t))
    r = browser.post("/api/hc/web/login", json={"email": "x@example.org", "code": "123456"}, headers=H(t))
    assert r.status_code == 429 and r.json()["error"] == "locked"


def test_no_mail_service_says_so(browser, hear):
    hear.resend = FakeResend(key="")
    t = page_token(browser)
    assert browser.post("/api/hc/web/login-code", json={"email": "a@example.org"}, headers=H(t)).status_code == 503


# ------------------------------------------------------------------ the session

def test_session_expires_after_30_days(browser, hear, resend):
    make_cong(hear)
    sign_in(browser, resend)
    assert browser.get("/api/hc/billing").status_code == 200
    (row,) = hear.db._q("SELECT created_at, expires_at FROM web_sessions")
    assert row["expires_at"] - row["created_at"] == pytest.approx(30 * 24 * 3600)
    hear.db._x("UPDATE web_sessions SET expires_at = ?", (time.time() - 1,))   # the hub's side of it (the browser
    assert browser.get("/api/hc/web/me").json() == {"signed_in": False}
    r = browser.get("/api/hc/billing")
    assert r.status_code == 401 and r.json()["detail"]["error"] == "signed_out"


def test_sign_out_revokes_and_everywhere_revokes_all(hear, resend):
    make_cong(hear)
    a, b = (TestClient(main.app, base_url="https://hearchurch.test") for _ in range(2))
    ca, cb = sign_in(a, resend), sign_in(b, resend)
    token = a.cookies.get("hc_session")
    r = a.post("/api/hc/web/logout", json={}, headers=H(ca))
    assert r.status_code == 200 and "hc_session" not in a.cookies
    assert hear.db.web_session(token) is None
    assert b.get("/api/hc/billing").status_code == 200
    c = TestClient(main.app, base_url="https://hearchurch.test")
    sign_in(c, resend)
    assert b.post("/api/hc/web/logout", json={"everywhere": True}, headers=H(cb)).status_code == 200
    assert c.get("/api/hc/billing").status_code == 401


def test_removing_the_address_ends_its_sessions(browser, hear, resend):
    cong, _ = make_cong(hear)
    hear.db.add_email(cong["id"], "clerk@example.org")
    clerk = TestClient(main.app, base_url="https://hearchurch.test")
    sign_in(clerk, resend, "clerk@example.org")
    csrf = sign_in(browser, resend)
    assert browser.post("/api/hc/emails/remove", json={"email": "clerk@example.org"}, headers=H(csrf)).status_code == 200
    assert clerk.get("/api/hc/account").status_code == 401


def test_a_session_only_opens_the_account_routes(browser, hear, resend):
    make_cong(hear)
    csrf = sign_in(browser, resend)
    assert browser.post("/api/hc/connect-code", json={}, headers=H(csrf)).status_code == 403
    assert browser.post("/api/hc/translate", json={}, headers=H(csrf)).status_code in (403, 422)


# ------------------------------------------------------------------ CSRF

def test_csrf_on_every_post(browser, hear, resend):
    make_cong(hear)
    # before sign-in: the page's token must come back, matching its cookie
    page_token(browser)
    assert browser.post("/api/hc/web/login-code", json={"email": "a@example.org"}).status_code == 403
    assert browser.post("/api/hc/web/login-code", json={"email": "a@example.org"},
                        headers=H("forged")).status_code == 403
    fresh = TestClient(main.app, base_url="https://hearchurch.test")   # no cookie: a token alone is not enough
    assert fresh.post("/api/hc/web/login-code", json={"email": "a@example.org"},
                      headers=H("anything")).status_code == 403
    csrf = sign_in(browser, resend)
    # signed in: the session's own token
    r = browser.post("/api/hc/emails", json={"email": "x@example.org"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "csrf"
    assert browser.post("/api/hc/emails", json={"email": "x@example.org"},
                        headers=H(browser.cookies.get("hc_csrf"))).status_code == 403   # the pre-sign-in token isn't it
    assert browser.post("/api/hc/emails", json={"email": "x@example.org"},
                        headers=H(csrf, Origin="https://evil.example")).status_code == 403
    assert browser.post("/api/hc/web/logout", json={}).status_code == 403
    assert browser.post("/api/hc/emails", json={"email": "x@example.org"},
                        headers=H(csrf, Origin="https://hear.test")).status_code == 200
    assert browser.get("/api/hc/account").status_code == 200   # reading needs no token


# ------------------------------------------------------------------ account actions

@pytest.fixture
def signed_in(browser, hear, resend):
    cong, token = make_cong(hear)
    csrf = sign_in(browser, resend)
    return NS(b=browser, h=H(csrf), cong=cong, mac=token)


def test_overview_billing_and_history(signed_in, hear, fake, client):  # noqa: F811
    s = signed_in
    b = s.b.get("/api/hc/billing").json()
    assert b["billing"] == "credits" and b["free_hours_left"] == 1 and b["paid_hours_left"] == 0
    assert b["card"] is None and b["can_buy"] and [x["usd"] for x in b["bundles"]] == [20, 50, 100, 250]
    post_event(client, "payment_intent.succeeded", pi_succeeded(s.cong["id"], pi="pi_paid"))
    u = s.b.get("/api/hc/usage?meetings=5").json()
    top, = [c for c in u["charges"] if c["kind"] == "top_up"]
    assert top["amount_usd"] == 20 and top["receipt_url"].startswith("/api/hc/receipts/")
    r = s.b.get(top["receipt_url"], follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://pay.stripe.com/receipts/pi_paid"
    assert s.b.get("/api/hc/receipts/9999", follow_redirects=False).status_code == 404
    other = TestClient(main.app, base_url="https://hearchurch.test")
    assert other.get(top["receipt_url"], follow_redirects=False).status_code == 403


def test_one_click_with_the_saved_card_from_the_web(signed_in, hear, fake, client):  # noqa: F811
    s = signed_in
    save_card(client, hear, fake, s.cong)
    assert s.b.get("/api/hc/billing").json()["card"]["last4"] == "4242"
    r = s.b.post("/api/hc/topup", json={"usd": 50, "request_id": "web-1"}, headers=s.h)
    assert r.status_code == 200 and r.json()["status"] == "succeeded" and r.json()["hours"] == 11
    _, params, opts = fake.of("pi")[-1]
    assert params["amount"] == 5000 and params["return_url"] == "https://hear.test/account?paid=1#billing"
    assert opts == {"idempotency_key": f"hc-topup-{s.cong['id']}-web-1"}


def test_checkout_card_setup_remove_and_auto_top_up_from_the_web(signed_in, hear, fake, client):  # noqa: F811
    s = signed_in
    r = s.b.post("/api/hc/checkout", json={"usd": 20, "save_card": True}, headers=s.h)
    assert r.status_code == 200
    _, p = fake.of("session")[-1]
    assert p["success_url"] == "https://hear.test/account?paid=1#billing"
    assert s.b.post("/api/hc/card/setup", json={}, headers=s.h).status_code == 200
    _, p = fake.of("session")[-1]
    assert p["success_url"] == "https://hear.test/account?card=saved#billing"
    assert s.b.post("/api/hc/auto-topup", json={"enabled": True, "usd": 50}, headers=s.h).status_code == 409   # no card
    save_card(client, hear, fake, s.cong)
    r = s.b.post("/api/hc/auto-topup", json={"enabled": True, "usd": 50}, headers=s.h)
    assert r.status_code == 200 and r.json()["auto_topup"]["enabled"] and r.json()["auto_topup"]["usd"] == 50
    r = s.b.post("/api/hc/card/remove", json={}, headers=s.h)
    assert r.status_code == 200 and r.json()["card"] is None and not r.json()["auto_topup"]["enabled"]


def test_redeem_a_code_from_the_web(signed_in, client):  # noqa: F811
    s = signed_in
    code = new_code(client, type="percent_off", value=20)["code"]
    r = s.b.post("/api/hc/codes/redeem", json={"code": code}, headers=s.h)
    assert r.status_code == 200 and r.json()["type"] == "percent_off" and r.json()["discount"]["percent"] == 20
    assert s.b.post("/api/hc/codes/redeem", json={"code": "ZZZZ-ZZZZ"}, headers=s.h).status_code == 403


def test_own_key_and_internal_show_their_type(signed_in, hear):
    s = signed_in
    hear.db.set_billing(s.cong["id"], "internal")
    assert s.b.get("/api/hc/billing").json()["billing"] == "internal"   # the web sees the real type
    hear.db.set_billing(s.cong["id"], "own_key")
    b = s.b.get("/api/hc/billing").json()
    assert b["billing"] == "own_key" and b["can_buy"] is False
    assert s.b.post("/api/hc/topup", json={"usd": 20}, headers=s.h).status_code in (409, 503)


def test_edit_the_congregation_address_and_link(signed_in, hear):
    s = signed_in
    r = s.b.post("/api/hc/congregation", json={"name": "Maple Grove 2nd Ward", "stake": "North Stake",
                                               "meeting_times": [{"day": "sun", "time": "11:00"},
                                                                 {"day": "wed", "time": "19:00"}],
                                               "timezone": "America/Chicago"}, headers=s.h)
    assert r.status_code == 200, r.text
    c = hear.db.congregation(s.cong["id"])
    assert c["name"] == "Maple Grove 2nd Ward" and c["stake"] == "North Stake" and len(c["meeting_times"]) == 2
    assert c["slug"] == s.cong["slug"]   # the name doesn't move the link
    assert s.b.post("/api/hc/congregation", json={"timezone": "Mars/Base"}, headers=s.h).status_code == 422
    r = s.b.post("/api/hc/location", json={"lat": 45.1, "lng": -93.4, "address": "2 Oak St, Maple Grove"}, headers=s.h)
    assert r.status_code == 200 and hear.db.congregation(s.cong["id"])["address"] == "2 Oak St, Maple Grove"
    r = s.b.post("/api/hc/slug", json={"slug": "maple-grove"}, headers=s.h)
    assert r.status_code == 200 and r.json()["aliases"] == [s.cong["slug"]]
    a = s.b.get("/api/hc/account").json()
    assert a["slug"] == "maple-grove" and a["url"] == "https://hear.test/maple-grove"
    assert s.b.post("/api/hc/slug", json={"slug": "account"}, headers=s.h).status_code == 409


def test_emails_from_the_web(signed_in, hear):
    s = signed_in
    r = s.b.post("/api/hc/emails", json={"email": "Clerk@Example.org"}, headers=s.h)
    assert r.status_code == 200 and [e["email"] for e in r.json()["emails"]] == ["a@example.org", "clerk@example.org"]
    assert s.b.post("/api/hc/emails/main", json={"email": "clerk@example.org"}, headers=s.h).status_code == 200
    assert hear.db.main_email(s.cong["id"]) == "clerk@example.org"
    r = s.b.post("/api/hc/emails/remove", json={"email": "clerk@example.org"}, headers=s.h)
    assert r.status_code == 409 and r.json()["error"] == "main"


def test_devices_revoke_and_add_a_mac(signed_in, hear, client):  # noqa: F811
    s = signed_in
    a = s.b.get("/api/hc/account").json()
    (mac,) = a["devices"]
    assert mac["name"] == "Mac" and mac["this"] is False
    r = s.b.post("/api/hc/pairing-code", json={}, headers=s.h)
    assert r.status_code == 200 and r.json()["expires_in"] > 800
    p = client.post("/api/hc/pair", json={"code": r.json()["code"], "device_name": "Second Mac"})
    assert p.status_code == 200
    r = s.b.post("/api/hc/devices/revoke", json={"id": mac["id"]}, headers=s.h)
    assert r.status_code == 200 and [d["name"] for d in r.json()["devices"]] == ["Second Mac"]
    assert client.get("/api/hc/billing", headers={"Authorization": f"Bearer {s.mac}"}).status_code == 403


def test_switch_between_congregations_of_one_address(browser, hear, resend):
    a, _ = make_cong(hear, "Ward A", "a@example.org")
    b, _ = make_cong(hear, "Ward B", "a@example.org")
    csrf = sign_in(browser, resend)
    me = browser.get("/api/hc/web/me").json()
    assert me["congregation"]["id"] == a["id"] and [c["id"] for c in me["congregations"]] == [a["id"], b["id"]]
    assert browser.post("/api/hc/web/switch", json={"id": b["id"]}, headers=H(csrf)).status_code == 200
    assert browser.get("/api/hc/account").json()["slug"] == b["slug"]
    other, _ = make_cong(hear, "Ward C", "c@example.org")
    assert browser.post("/api/hc/web/switch", json={"id": other["id"]}, headers=H(csrf)).status_code == 404


# ------------------------------------------------------------------ the pages themselves

def test_one_click_asks_first_on_the_web_and_in_the_app():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    js = (root / "web/account/account.js").read_text()
    assert "Buy ${HC.hours(x.hours)} hours for ${HC.usd(price(usd))} on ${brand(bill.card)}?" in js
    assert js.index("await ask(") < js.index('"/api/hc/topup"')
    swift = (root / "mac/HearChurch/Sources/SettingsView.swift").read_text()
    assert "if oneClick { confirmBuy = x }" in swift and 'on \\(b.card?.short ?? "your card")?' in swift


def test_church_choices_match_the_mac_app():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    swift = (root / "mac/HearChurch/Sources/Profile.swift").read_text()
    lds = re.search(r'latterDaySaints = "([^"]+)"', swift).group(1)
    rest = re.findall(r'"([^"]+)"', re.search(r"static let all = \[latterDaySaints, ([^\]]+)\]", swift).group(1))
    js = (root / "web/account/common.js").read_text()
    listed = re.findall(r'"([^"]+)"', re.search(r"CHURCHES: \[([^\]]+)\]", js).group(1))
    assert listed == [lds, *rest]


def test_the_web_module_lists_only_real_routes():
    paths = set(main.app.openapi()["paths"])
    assert web.WEB_ROUTES <= paths


def test_the_macs_manage_billing_link_signs_in_once(browser, client, hear, resend):  # noqa: F811
    cong, device = make_cong(hear)
    assert client.post("/api/hc/web/link").status_code == 403                       # no device token
    assert client.post("/api/hc/web/link", headers={"Authorization": "Bearer nope"}).status_code == 403
    r = client.post("/api/hc/web/link", headers={"Authorization": f"Bearer {device}"})
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert "/account/link?t=hcl" in url
    path = url[url.index("/account/link"):]
    r = browser.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account#billing"
    me = browser.get("/api/hc/web/me").json()
    assert me["signed_in"] and me["email"] == "a@example.org" and me["congregation"]["id"] == cong["id"]
    assert browser.get("/api/hc/billing").status_code == 200
    other = TestClient(main.app, base_url="https://hearchurch.test")
    r = other.get(path, follow_redirects=False)                                       # used up
    assert r.status_code == 303 and "set-cookie" not in r.headers
    assert other.get("/api/hc/web/me").json() == {"signed_in": False}
    # a browser session can't mint links (only the Mac's device token)
    assert browser.post("/api/hc/web/link", headers=H(me["csrf"])).status_code == 403
    # an expired link does nothing
    url = client.post("/api/hc/web/link", headers={"Authorization": f"Bearer {device}"}).json()["url"]
    hear.db._x("UPDATE web_codes SET expires_at = ? WHERE purpose LIKE 'link:%'", (time.time() - 1,))
    fresh = TestClient(main.app, base_url="https://hearchurch.test")
    fresh.get(url[url.index("/account/link"):], follow_redirects=False)
    assert fresh.get("/api/hc/web/me").json() == {"signed_in": False}
