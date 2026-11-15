"""hearchurch.com/start (sign up) and hearchurch.com/account (manage): the church's web account. The Mac app only
signs in (with the e-mail, by code). Served by the hub on the same origin as the site: the Worker only answers its own
routes (download/wrangler.toml) and hands every other path, /start and /account included, to the hub.

Pages (HTML from web/account/, the site's look):
  GET  /start, /start/done   sign up: church, name, meeting times, address (Photon suggestions in the browser + an OSM
                             map), e-mails, optional code, optional card; the e-mail is proved by a 6-digit code first.
  GET  /account              sign in by e-mailed code, then Overview, Billing, Have a code?, Congregation, Emails, Devices.
JSON (CSRF: every POST carries X-CSRF-Token, and an Origin, when the browser sends one, must be this site):
  POST /api/hc/web/signup-code {email} → {ok, message}: a 6-digit code to that address (Resend). 503 not_configured,
                             429 per IP / per address (the Mac's "Email me a code" limits).
  POST /api/hc/web/signup    {…POST /api/hc/signup's fields, email_code} → {ok, slug, email, …} + the session cookie;
                             403 bad_email_code, 403 bad_code (the promo code; nothing made), 429 locked.
  POST /api/hc/web/login-code {email} → always the same {ok, message} (no probing which addresses exist); a code goes
                             only to an address of a congregation. Same limits as above.
  POST /api/hc/web/login     {email, code} → {ok, csrf} + the session cookie; 403 bad_code (one answer for a wrong
                             code and an unknown address), 429 locked.
  GET  /api/hc/web/me        → {signed_in, email, csrf, congregation, congregations}
  POST /api/hc/web/switch    {id} → another congregation of the same address
  POST /api/hc/web/logout    {everywhere?} → the session (or every session of the address) ends; cookie cleared.
  GET  /api/hc/receipts/<credit id> → 302 to Stripe's receipt for that top-up.
Everything else the account pages do goes through the Mac's own routes (/api/hc/billing, /topup, /emails, …): the
session cookie is accepted there in place of a device token, only for WEB_ROUTES (api.device_and_congregation).

Session: cookie hc_session (HttpOnly, Secure, SameSite=Lax, 30 days), a random token whose hash is in
web_sessions; signing out revokes it. A session ends by itself when its address is removed from the congregation.
Before sign-in, the pages' POSTs are guarded by a double-submit token: cookie hc_csrf (HttpOnly, SameSite=Strict) and
the same value in the page, sent back as X-CSRF-Token.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import WEB_DIR
from . import api
from .admin import STRIPE_PREFIXES
from .db import WEB_CODE_TTL_S, WEB_SESSION_TTL_S

log = logging.getLogger("hear")
router = APIRouter()

SESSION_COOKIE = "hc_session"
CSRF_COOKIE = "hc_csrf"
CSRF_HEADER = "x-csrf-token"
PAGES = WEB_DIR / "account"

# The Mac's routes a signed-in browser may use (with its session cookie instead of a device token).
WEB_ROUTES = frozenset({
    "/api/hc/account", "/api/hc/emails", "/api/hc/emails/main", "/api/hc/emails/remove", "/api/hc/devices/revoke",
    "/api/hc/pairing-code", "/api/hc/slug", "/api/hc/location", "/api/hc/congregation", "/api/hc/billing",
    "/api/hc/usage", "/api/hc/checkout", "/api/hc/card/setup", "/api/hc/card/remove", "/api/hc/topup",
    "/api/hc/auto-topup", "/api/hc/codes/redeem"})
RECEIPTS = "/api/hc/receipts/"

LOGIN_SUBJECT = "Your Hear Church sign-in code"
SIGNUP_SUBJECT = "Your Hear Church code"
BAD_LOGIN = "That code is wrong or has expired. Ask for a new one."


def _hear() -> api.Hear:
    return api._hear()


# ------------------------------------------------------------------ CSRF

def _origin_ok(request: Request) -> bool:
    """No Origin (an old browser, a test client): the token alone decides. Otherwise it must be this site."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    allowed = {f"{request.url.scheme}://{request.headers.get('host', '')}".lower()}
    public = urlsplit(api._base_url(request))
    if public.scheme and public.netloc:
        allowed.add(f"{public.scheme}://{public.netloc}".lower())
    return origin.lower() in allowed


def _csrf_failed() -> HTTPException:
    return HTTPException(403, {"error": "csrf", "message": "This page is out of date: reload it and try again."})


def check_pre_csrf(request: Request) -> None:
    """Before sign-in: the X-CSRF-Token header must match the hc_csrf cookie the page came with."""
    cookie, header = request.cookies.get(CSRF_COOKIE, ""), request.headers.get(CSRF_HEADER, "")
    if not (cookie and header and hmac.compare_digest(cookie, header) and _origin_ok(request)):
        raise _csrf_failed()


def _check_session_csrf(request: Request, s: dict[str, Any]) -> None:
    header = request.headers.get(CSRF_HEADER, "")
    if not (header and hmac.compare_digest(s["csrf"], header) and _origin_ok(request)):
        raise _csrf_failed()


# ------------------------------------------------------------------ the session

def _signed_out() -> HTTPException:
    return HTTPException(401, {"error": "signed_out", "message": "You're signed out: sign in again."})


def current_session(request: Request) -> dict[str, Any] | None:
    """The browser's live session (and its address still on its congregation), or None."""
    h = _hear()
    s = h.db.web_session(request.cookies.get(SESSION_COOKIE, ""))
    if s is None:
        return None
    if s["congregation_id"] is not None:
        cong = h.db.congregation(s["congregation_id"])
        if cong is None or cong["hidden"] or s["email"] not in {e["email"] for e in h.db.emails(cong["id"])}:
            h.db.revoke_web_session(s["id"])   # the address was removed: this sign-in is over
            return None
    if (s["last_seen"] or 0) < time.time() - 300:
        h.db.touch_web_session(s["id"])
    return s


def session_and_congregation(request: Request) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """For api.device_and_congregation: a signed-in browser on one of WEB_ROUTES, as ({"id": None, "web": True},
    congregation). None when the request has no session cookie or isn't for such a route (the device check goes on);
    401 signed_out for a cookie that no longer works; 403 csrf for a POST without the session's token."""
    path = request.url.path
    if path not in WEB_ROUTES and not path.startswith(RECEIPTS):
        return None
    if not request.cookies.get(SESSION_COOKIE):
        return None
    s = current_session(request)
    if s is None or s["congregation_id"] is None:
        raise _signed_out()
    if request.method not in ("GET", "HEAD"):
        _check_session_csrf(request, s)
    request.state.hc_web = True
    request.state.hc_session = s
    return {"id": None, "web": True, "name": "hearchurch.com"}, _hear().db.congregation(s["congregation_id"])


def _set_session(resp: JSONResponse, token: str) -> JSONResponse:
    resp.set_cookie(SESSION_COOKIE, token, max_age=int(WEB_SESSION_TTL_S), httponly=True, secure=True,
                    samesite="lax", path="/")
    return resp


# ------------------------------------------------------------------ pages

def _page(request: Request, name: str) -> HTMLResponse:
    token = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)
    html = (PAGES / name).read_text().replace("{{csrf}}", token)
    resp = HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                       "Referrer-Policy": "same-origin"})
    resp.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict", path="/")
    return resp


@router.get("/start", response_class=HTMLResponse)
@router.get("/start/done", response_class=HTMLResponse)
async def start_page(request: Request):
    return _page(request, "start.html")


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    return _page(request, "account.html")


# ------------------------------------------------------------------ e-mailed codes

class EmailIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: str = Field(max_length=120)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return api.email_address(v)


def _no_mail() -> JSONResponse:
    return JSONResponse({"error": "not_configured", "message": "E-mail isn't set up on this server yet. Write to "
                                                               "support@hearchurch.com."}, status_code=503)


def code_text(code: str, purpose: str) -> str:
    what = ("sign in to your congregation's Hear Church account" if purpose == "login"
            else "finish signing up for Hear Church")
    return (f"Your code to {what}:\n\n    {code}\n\nEnter it on hearchurch.com. It works once, for "
            f"{WEB_CODE_TTL_S // 60} minutes.\n\nIf you didn't ask for it, ignore this e-mail: without the code "
            "nothing happens.\n\nHear Church\n")


async def _send_code(email: str, purpose: str) -> None:
    """Make and send the code after the answer went out, so the answer's timing tells nothing."""
    h = _hear()
    code = h.db.new_web_code(email, purpose)
    try:
        ok = await h.resend.send(email, LOGIN_SUBJECT if purpose == "login" else SIGNUP_SUBJECT,
                                 code_text(code, purpose))
    except Exception as e:
        ok = False
        log.warning("web %s code not sent: %s", purpose, e)
    log.info("web %s code %s", purpose, "sent" if ok else "NOT sent")


def _mail_limits(request: Request, email: str) -> None:
    api._limit(request, "email_code")
    api._limit(request, "email_code_addr", email)


@router.post("/api/hc/web/signup-code")
async def signup_code(request: Request, body: EmailIn, tasks: BackgroundTasks):
    check_pre_csrf(request)
    if not _hear().resend.configured():
        return _no_mail()
    _mail_limits(request, body.email)
    tasks.add_task(_send_code, body.email, "signup")
    return {"ok": True, "message": f"We sent a 6-digit code to {body.email}. It works for {WEB_CODE_TTL_S // 60} "
                                   "minutes."}


class WebSignUp(api.SignUp):
    email_code: str = Field(max_length=12)


@router.post("/api/hc/web/signup")
async def web_signup(request: Request, body: WebSignUp):
    """Sign up on hearchurch.com: the main e-mail is proved by its code; then the same sign-up as the Mac's. The
    congregation is verified at once (no link to click) and this browser is signed in to it. No Mac yet: the app
    signs in with the e-mail."""
    check_pre_csrf(request)
    h = _hear()
    ip = api._ip(request)
    if wait := h.web_code_ip.locked(ip):
        return api._locked(wait)
    if body.code:   # a mistyped promo code is told before the e-mail code is used up
        from . import payments
        if wait := h.own_key_ip.locked(ip):
            return api._locked(wait)
        if h.db.check_code(body.code) is None:
            h.own_key_ip.fail(ip)
            return payments._bad_code()
    if not h.db.use_web_code(body.email, "signup", body.email_code):
        h.web_code_ip.fail(ip)
        return JSONResponse({"error": "bad_email_code", "message": BAD_LOGIN}, status_code=403)
    api._limit(request, "signup")   # sign-ups per IP (wrong e-mail codes count against web_code_ip instead)
    made = await api.create_from_signup(request, body)
    if isinstance(made, JSONResponse):
        return made
    cong, redeemed, located = made
    h.db.set_verified(cong["id"])
    log.info("%s signed up on the web", cong["slug"])
    token, csrf = h.db.new_web_session(body.email, cong["id"])
    from . import payments
    return _set_session(JSONResponse({
        "ok": True, "slug": cong["slug"], "email": body.email, "csrf": csrf, "billing": cong["billing"],
        "can_save_card": payments.billing_extras(cong)["can_save_card"], **located, **redeemed}), token)


@router.post("/api/hc/web/login-code")
async def login_code(request: Request, body: EmailIn, tasks: BackgroundTasks):
    """The same answer whether or not the address belongs to a congregation (no probing)."""
    check_pre_csrf(request)
    h = _hear()
    if not h.resend.configured():
        return _no_mail()
    _mail_limits(request, body.email)
    if h.db.congregations_with_email(body.email):
        tasks.add_task(_send_code, body.email, "login")
    return {"ok": True, "message": f"If {body.email} belongs to a congregation on Hear Church, we sent it a 6-digit "
                                   f"code. It works for {WEB_CODE_TTL_S // 60} minutes."}


class LoginIn(EmailIn):
    code: str = Field(max_length=12)


@router.post("/api/hc/web/login")
async def login(request: Request, body: LoginIn):
    check_pre_csrf(request)
    h = _hear()
    ip = api._ip(request)
    if wait := h.web_code_ip.locked(ip):
        return api._locked(wait)
    congs = h.db.congregations_with_email(body.email)
    if not (congs and h.db.use_web_code(body.email, "login", body.code)):
        h.web_code_ip.fail(ip)
        return JSONResponse({"error": "bad_code", "message": BAD_LOGIN}, status_code=403)
    token, csrf = h.db.new_web_session(body.email, congs[0]["id"])
    log.info("%s: signed in on the web", congs[0]["slug"])
    return _set_session(JSONResponse({"ok": True, "csrf": csrf}), token)


def _cong_brief(c: dict[str, Any]) -> dict[str, Any]:
    return {"id": c["id"], "slug": c["slug"], "name": c["name"], "display": api.display_name(c),
            "church": c["church"], "unit_type": c["unit_type"], "stake": c["stake"], "billing": c["billing"],
            "address": c["address"], "lat": c["lat"], "lng": c["lng"], "meeting_times": c["meeting_times"],
            "timezone": c["timezone"]}


@router.get("/api/hc/web/me")
async def me(request: Request):
    s = current_session(request)
    if s is None:
        return JSONResponse({"signed_in": False}, headers={"Cache-Control": "no-store"})
    h = _hear()
    cong = h.db.congregation(s["congregation_id"]) if s["congregation_id"] else None
    return JSONResponse({"signed_in": True, "email": s["email"], "csrf": s["csrf"],
                         "congregation": _cong_brief(cong) if cong else None,
                         "congregations": [{"id": c["id"], "display": api.display_name(c)}
                                           for c in h.db.congregations_with_email(s["email"])]},
                        headers={"Cache-Control": "no-store"})


def _session_or_401(request: Request) -> dict[str, Any]:
    s = current_session(request)
    if s is None:
        raise _signed_out()
    _check_session_csrf(request, s)
    return s


class SwitchIn(BaseModel):
    id: int


@router.post("/api/hc/web/switch")
async def switch(request: Request, body: SwitchIn):
    s = _session_or_401(request)
    h = _hear()
    if body.id not in {c["id"] for c in h.db.congregations_with_email(s["email"])}:
        return JSONResponse({"error": "not_found", "message": "That congregation isn't one of yours."}, status_code=404)
    h.db.set_web_session_congregation(s["id"], body.id)
    return {"ok": True}


class LogoutIn(BaseModel):
    everywhere: bool = False


@router.post("/api/hc/web/logout")
async def logout(request: Request, body: LogoutIn | None = None):
    s = current_session(request)
    if s is not None:
        _check_session_csrf(request, s)
        h = _hear()
        if body and body.everywhere:
            h.db.revoke_web_sessions(s["email"])
        else:
            h.db.revoke_web_session(s["id"])
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return resp


# ------------------------------------------------------------------ Stripe receipts

def _g(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


async def receipt_url(payment_id: str) -> str | None:
    """Stripe's receipt page for the payment behind a top-up (a payment intent, Checkout session or charge)."""
    from . import payments

    client = payments._stripe()
    if client is None:
        return None
    if payment_id.startswith("cs_"):
        s = await asyncio.to_thread(client.v1.checkout.sessions.retrieve, payment_id,
                                    {"expand": ["payment_intent.latest_charge"]})
        charge = _g(_g(s, "payment_intent"), "latest_charge")
    elif payment_id.startswith("pi_"):
        pi = await asyncio.to_thread(client.v1.payment_intents.retrieve, payment_id, {"expand": ["latest_charge"]})
        charge = _g(pi, "latest_charge")
    else:
        charge = await asyncio.to_thread(client.v1.charges.retrieve, payment_id)
    return _g(charge, "receipt_url") if charge is not None and not isinstance(charge, str) else None


@router.get(RECEIPTS + "{credit_id}")
async def receipt(request: Request, credit_id: int):
    _, cong = api.device_and_congregation(request)
    credit = next((c for c in _hear().db.credits(cong["id"]) if c["id"] == credit_id), None)
    if credit is None or not credit["payment_id"].startswith(STRIPE_PREFIXES):
        return api._page("No receipt", "There is no Stripe receipt for that payment.", 404)
    try:
        url = await receipt_url(credit["payment_id"])
    except Exception as e:
        log.warning("stripe receipt for credit %d: %s", credit_id, e)
        url = None
    if not url:
        return api._page("Receipt not available", "Stripe didn't give us the receipt just now. Try again later; it "
                                                  "was also e-mailed to your main address.", 502)
    return RedirectResponse(url, status_code=302)
