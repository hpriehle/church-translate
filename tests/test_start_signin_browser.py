"""hearchurch.com/start and /account in a real browser (Playwright, Chromium) against a hub on a local port: sign-up
with every field, address search + pin drag, the e-mailed code, /account with the trial line, sign out, sign in by
code, the Mac's sign-in routes, wrong/expired codes and the limits. Desktop and iPhone-size.
Photon is faked; map tiles go to OSM (the test checks they carry a Referer, which OSM's tile policy requires).
Not a project dependency: run with `uv run --with playwright pytest tests/test_start_signin_browser.py`.
HC_SCREENS=1 writes the screenshots to docs/screens/start-signin/."""
import json
import os
import re
from pathlib import Path

import httpx
import pytest

pw = pytest.importorskip("playwright.sync_api")

from test_accounts import FakeResend  # noqa: E402
from test_hear import hear, server  # noqa: E402,F401  (fixtures)

PHOTON = {"features": [{"geometry": {"coordinates": [-93.4555, 45.0725]},
                        "properties": {"housenumber": "1", "street": "Main Street", "city": "Maple Grove",
                                       "state": "Minnesota", "postcode": "55369", "country": "United States"}}]}
VIEWPORTS = {"desktop": {"viewport": {"width": 1280, "height": 900}},
             "iphone": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3, "is_mobile": True,
                        "has_touch": True}}
EMAIL = "bishop@example.org"


def code6(pg, resend, n):
    """The 6-digit code of the e-mail after the first n (it goes out in a background task)."""
    for _ in range(100):
        if len(resend.sent) > n:
            return re.search(r"\n    (\d{6})\n", resend.sent[n][2]).group(1)
        pg.wait_for_timeout(50)
    raise AssertionError("no code e-mail")


@pytest.mark.free_trial
@pytest.mark.parametrize("size", VIEWPORTS)
def test_start_then_account_then_sign_in_again(size, hear, server, tmp_path):  # noqa: F811
    resend = hear.resend = FakeResend()
    base = f"http://localhost:{server}"   # Chromium keeps Secure cookies on http://localhost
    out = Path("docs/screens/start-signin") if os.environ.get("HC_SCREENS") else tmp_path
    out.mkdir(parents=True, exist_ok=True)
    tile_referers = []

    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(**VIEWPORTS[size])
        ctx.route("https://photon.komoot.io/**", lambda r: r.fulfill(json=PHOTON))
        ctx.route("https://tile.openstreetmap.org/**",
                  lambda r: (tile_referers.append(r.request.headers.get("referer")), r.continue_()))
        pg = ctx.new_page()
        n = iter(range(1, 100))

        def shot(name):
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(out / f"{size}-{next(n):02d}-{name}.png"), full_page=True)

        def msg(sel):
            pg.locator(sel).wait_for(state="visible")
            return pg.locator(sel).inner_text()

        # ---- /start: the form's own checks first
        pg.goto(base + "/start")
        shot("start-empty")
        pg.click("#continue")
        assert msg("#form-msg") == "Choose your church."
        pg.select_option("#church", label="The Church of Jesus Christ of Latter-day Saints")
        pg.click("#continue")
        assert pg.inner_text("#form-msg") == "Enter the congregation's name."
        pg.fill("#name", "Maple Grove Ward")
        pg.fill("#stake", "Maple Stake")
        pg.click("#continue")
        assert pg.inner_text("#form-msg") == "Enter your e-mail address."
        pg.fill("#email", EMAIL)
        pg.click("#add-email")
        pg.fill("input.more", "not-an-email")
        pg.click("#continue")
        assert pg.inner_text("#form-msg") == '"not-an-email" isn\'t an e-mail address.'
        pg.hover("#continue")   # the gold button stays gold under the pointer (and after a tap on a phone)
        assert "gradient" in pg.eval_on_selector("#continue", "e => getComputedStyle(e).backgroundImage")
        shot("start-validation")
        pg.fill("input.more", "clerk@example.org")
        pg.click(".add-time")
        pg.locator("#times-box select").nth(1).select_option("wed")
        pg.locator("#times-box input[type=time]").nth(1).fill("19:00")
        pg.fill("#promo", "NOPE-NOPE")
        pg.click("#check-promo")
        assert msg("#promo-msg")
        pg.fill("#promo", "")

        # ---- address search, then drag the pin
        pg.fill("#address", "1 Main St Maple")
        pg.locator("#address-suggestions li").first.click()
        assert pg.input_value("#address") == "1 Main Street, Maple Grove, Minnesota, 55369, United States"
        pin = pg.locator(".leaflet-marker-icon")
        pin.wait_for()
        pg.wait_for_timeout(800)   # the zoom to the street
        box = pin.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        pg.mouse.move(x, y)
        pg.mouse.down()
        pg.mouse.move(x + 40, y + 30, steps=8)
        pg.mouse.up()
        assert any(tile_referers) and all(r and r.startswith(base) for r in tile_referers), tile_referers
        shot("start-filled")

        # ---- the e-mailed code: a wrong one, then the right one
        pg.click("#continue")
        pg.locator("#code-view").wait_for(state="visible")
        assert pg.inner_text("#code-email") == EMAIL
        shot("start-code")
        pg.fill("#email-code", "12")
        pg.click("#create")
        assert msg("#code-msg") == "Enter the 6 digits from the e-mail."
        right = code6(pg, resend, 0)
        pg.fill("#email-code", "000000" if right != "000000" else "111111")
        pg.click("#create")
        pg.wait_for_function("document.querySelector('#code-msg').textContent.length > 0 && "
                             "!document.querySelector('#code-msg').textContent.startsWith('Enter the 6')")
        shot("start-wrong-code")
        pg.fill("#email-code", right)
        pg.click("#create")
        pg.wait_for_url("**/start/done")
        pg.locator("#done-view").wait_for(state="visible")
        assert pg.inner_text("#done-email") == EMAIL
        shot("start-done")

        cong = hear.db.by_slug("maple-grove-ward")
        assert cong["lat"] < 45.0725 and cong["lng"] > -93.4555   # dragged down and right from the search result
        assert cong["stake"] == "Maple Stake" and cong["address"].startswith("1 Main Street")
        assert cong["meeting_times"] == [{"day": "sun", "time": "10:00"}, {"day": "wed", "time": "19:00"}]
        assert sorted(e["email"] for e in hear.db.emails(cong["id"])) == ["bishop@example.org", "clerk@example.org"]

        # ---- the Mac's sign-in with the same account: "Email me a code" → /api/hc/pair
        with httpx.Client(base_url=f"http://127.0.0.1:{server}") as mac:
            assert mac.post("/api/hc/email-code", json={"email": EMAIL}).status_code == 200
            bad = mac.post("/api/hc/pair", json={"code": "AAAA-AAAA", "device_name": "Chapel Mac"})
            assert bad.status_code == 403, bad.text
            code = re.search(r"\n    ([A-Z0-9]{4}-[A-Z0-9]{4})\n", resend.sent[-1][2]).group(1)
            r = mac.post("/api/hc/pair", json={"code": code, "device_name": "Chapel Mac"})
            assert r.status_code == 200 and r.json()["slug"] == "maple-grove-ward", r.text
            again = mac.post("/api/hc/pair", json={"code": code, "device_name": "Chapel Mac"})   # used up
            assert again.status_code == 403
            (out / f"{size}-mac-api.json").write_text(json.dumps(
                {"wrong_code": bad.json(), "pair": {k: v for k, v in r.json().items() if k != "device_token"},
                 "reused_code": again.json()}, indent=2))
        hear.limits["email_code_addr"].hits.clear()   # an hour later: the 3 codes an hour per address

        # ---- /account: the trial line, then sign out
        pg.goto(base + "/account")
        pg.locator("#app-view").wait_for(state="visible")
        pg.wait_for_function("document.querySelector('#ov-message').textContent.length > 0")
        assert pg.inner_text("#ov-message").startswith("Free trial:")
        shot("account")
        pg.click("#logout")
        pg.locator("#login-view").wait_for(state="visible")
        shot("signed-out")

        # ---- sign in again with "Send me a code": an expired code, a wrong one, then the right one
        pg.fill("#email", EMAIL)
        sent = len(resend.sent)
        pg.click("#send")
        pg.locator("#login-code").wait_for(state="visible")
        shot("signin-code")
        right = code6(pg, resend, sent)
        hear.db._x("UPDATE web_codes SET expires_at = 0 WHERE purpose = 'login'")
        pg.fill("#code", right)
        pg.click("#verify")
        expired = msg("#login-msg")
        shot("signin-expired")
        pg.click("#other-email")
        pg.fill("#email", EMAIL)
        sent = len(resend.sent)
        pg.click("#send")
        pg.locator("#login-code").wait_for(state="visible")
        right = code6(pg, resend, sent)
        pg.fill("#code", "000000" if right != "000000" else "111111")
        pg.click("#verify")
        pg.locator("#login-msg").wait_for(state="visible")
        wrong = pg.inner_text("#login-msg")
        pg.fill("#code", right)
        pg.click("#verify")
        pg.locator("#app-view").wait_for(state="visible")
        shot("signed-in-again")

        # ---- the limits: a fourth code request for the address in an hour is refused, and says so
        pg.click("#logout")
        pg.locator("#login-view").wait_for(state="visible")
        pg.fill("#email", EMAIL)
        pg.click("#send")
        pg.locator("#login-code").wait_for(state="visible")
        pg.click("#other-email")
        pg.click("#send")
        limited = msg("#login-msg")
        shot("signin-rate-limited")
        ctx.close()
        b.close()

    assert expired == wrong, (expired, wrong)   # the same answer: no telling what was wrong
    assert limited == "Too many codes asked for. Use the last code we sent, or try again in an hour.", limited
