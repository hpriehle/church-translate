"""Hear Church HTTP/WebSocket routes: sign-up, device tokens, /c/<slug> listener pages, admin list.

Sign-up flow (JSON, for the Mac app; sign-up is open, no manual approval):
  POST /api/hc/signup       church, name, unit_type, email, [stake, address, lat, lng, meeting_times, timezone,
                            device_name] → {slug, claim}; an e-mail with a verification link is sent.
                            With [hearchurch] verify_email = false (for now: no e-mail service yet) it answers
                            {slug, device_token} at once and the congregation counts as verified.
  GET  /api/hc/verify?token=…   (the link in the e-mail) marks the e-mail verified; the congregation is live-capable
  POST /api/hc/claim        {claim} → 202 {pending} until the link was clicked, then once {device_token, slug}
  POST /api/hc/location     (device token) {lat, lng, [address]} or {address} (geocoded) → moves the building's pin
  POST /api/hc/device-link  {email, [slug, device_name]} → e-mails a magic link to add another Mac; the new Mac
                            then polls /api/hc/claim with the returned {claim} exactly like above
  POST /api/hc/connect-code (device token) → {connect_code}: a new 6-digit code for the congregation (old one dies)
  POST /api/hc/connect      {slug, code, device_name} → like claim's answer; 404 not_found, 403 bad_code,
                            429 {error: locked, retry_after}. Signup (verify_email off) also returns connect_code.
                            The answer carries the congregation's billing ("credits" or "own_key").
Billing (prepaid credits, or the congregation's own Anthropic key through a one-time code Harrison makes):
  POST /api/hc/own-key-code/check {code} → {ok} or 403 bad_code / 429 locked. Doesn't use the code up: onboarding
                            asks for the code before the key, and the congregation only exists after sign-up.
  POST /api/hc/own-key-code (device token) {code} → {ok, billing: "own_key"}; uses the code up for this
                            congregation (asking again is fine). 403 bad_code (unknown, or used by another), 429 locked.
  GET  /api/hc/billing      (device token) → {billing, balance: {left_min, free_left_min, balance_usd, can_start, …},
                            balance_usd, language_hours_left, out_of_credit, can_buy, buy_usd, message}
  POST /api/hc/checkout     (device token) {usd} → {url}: a Stripe Checkout page (card only) for that much credit;
                            503 not_configured without STRIPE_SECRET_KEY. Back to /api/hc/checkout/done or /cancelled.
  POST /api/stripe/webhook  Stripe → hub, signed with STRIPE_WEBHOOK_SECRET: checkout.session.completed (paid) calls
                            credit_top_up with its payment_intent and amount_total, once per payment.
  POST /api/admin/congregations/<id>/credits {payment_id, cents} (operator) → {created, credit, balance}: credit a
                            top-up. Idempotent on payment_id (the Stripe step calls `credit_top_up` with the
                            payment's id); 409 when that id was credited with another amount or congregation.
  POST /api/admin/own-key-codes {label} (operator) → {code, label}: the only time the code is shown.
  GET  /api/admin/own-key-codes (operator) → {codes: [{label, created_at, used_at, used_by}]}
The claim secret stays with the app that asked, so a link opened by a mail scanner yields no token.
Sign-up geocodes an address that came without lat/lng (Nominatim, or Mapbox with HEARCHURCH_MAPBOX_TOKEN). The
answer says whether it worked ("located"); if not, lat/lng stay empty and the app should ask for a map pin and
send it to /api/hc/location.
Listener side: hearchurch.com/ (the finder: by location, then by name), hearchurch.com/<slug> (one congregation:
its live meeting, or "wait for the meeting to start"; /c/<slug> redirects there), GET /api/live?lat&lng
(congregations within a mile of a phone; the phone computes exact distances; the position is never stored or
logged), GET /api/hc/search?q= (by name or town), GET /api/hc/c/<slug>.
meeting_times: [{"day": "sun", "time": "10:00"}], timezone: an IANA name (used for "next meeting").
"""
from __future__ import annotations

import asyncio
import html
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import HOME, LOG_DIR, WEB_DIR
from .db import CENTS_PER_LANGUAGE_HOUR, RESERVED_SLUGS, Database
from .geo import Geocoder, bounding_box, hide_coordinates_in_access_log, in_box
from .live import Live
from .mail import Mailer, RateLimiter

log = logging.getLogger("hear")
router = APIRouter()
pages = APIRouter()   # hearchurch.com/<slug>: included after every other route (app/main.py), so it never shadows one

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
LINK_TTL_S = 24 * 3600
NEARBY_MI = 1.0             # /api/live: congregations within this of the phone (it is normally at the chapel)
SOON_S = 30 * 60            # a meeting starting within this long counts as "starting soon"
hide_coordinates_in_access_log()


class Hear:
    def __init__(self, db: Database, mailer: Mailer, live: Live, geocoder: Geocoder | None = None,
                 verify_email: bool = True):
        self.db, self.mailer, self.live = db, mailer, live
        self.verify_email = verify_email
        self.geocoder = geocoder or Geocoder()
        self.connect_ip = FailureLimiter(5, 15 * 60)     # wrong codes per IP
        self.connect_cong = FailureLimiter(10, 3600)     # wrong codes per congregation, then it is locked
        self.own_key_ip = FailureLimiter(10, 3600)       # wrong own-key codes per IP
        self.limits = {"live": RateLimiter(60, 60), "search": RateLimiter(60, 60), "location": RateLimiter(30, 3600),
                       "signup": RateLimiter(5, 3600), "email": RateLimiter(10, 3600),
                       "email_addr": RateLimiter(3, 3600), "claim": RateLimiter(1200, 3600),
                       "verify": RateLimiter(60, 3600), "checkout": RateLimiter(20, 3600)}


class FailureLimiter:
    """Counts failures only: locked(key) says for how many seconds a key is locked (0 = not)."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window_s = limit, window_s
        self.fails: dict[str, list[float]] = {}

    def locked(self, key: str) -> int:
        now = time.time()
        recent = [t for t in self.fails.get(key, []) if now - t < self.window_s]
        if recent:
            self.fails[key] = recent
        else:
            self.fails.pop(key, None)
        return 0 if len(recent) < self.limit else max(1, int(recent[0] + self.window_s - now) + 1)

    def fail(self, key: str) -> None:
        self.fails.setdefault(key, []).append(time.time())


H: Hear | None = None


def setup(settings) -> Hear:
    """Open the database and build the registry (called once at server start, and by tests)."""
    global H
    cfg = settings.hearchurch
    path = HOME / cfg.db_path
    db = Database(path)
    H = Hear(db, Mailer.from_settings(cfg, LOG_DIR / "outbox.log"), Live(db, settings.stream, cfg.grace_s),
             verify_email=cfg.verify_email)
    try:
        asyncio.get_running_loop().create_task(backfill_towns())
    except RuntimeError:   # no event loop (a plain script): skip
        pass
    return H


async def backfill_towns() -> None:
    """Congregations from before search showed the town: look it up from their pins, one at a time."""
    h = _hear()
    for c in h.db.missing_towns():
        town = await _town(c["lat"], c["lng"])
        if town:
            h.db.set_town(c["id"], town)


def _hear() -> Hear:
    if H is None:
        raise HTTPException(503, "Hear Church is not set up on this server.")
    return H


def _ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def _limit(request: Request, which: str, key: str | None = None) -> None:
    if not _hear().limits[which].allow(key or _ip(request)):
        raise HTTPException(429, "Too many requests: try again later.")


def _base_url(request: Request) -> str:
    from .. import main

    return (main.S.settings.server.public_url or str(request.base_url)).rstrip("/")


# ------------------------------------------------------------------ sign-up

class MeetingTime(BaseModel):
    day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class SignUp(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    church: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=2, max_length=80)
    unit_type: Literal["ward", "branch", "congregation", "parish", "other"]
    email: str = Field(max_length=120)
    stake: str | None = Field(default=None, max_length=80)
    address: str = Field(default="", max_length=200)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    meeting_times: Annotated[list[MeetingTime], Field(max_length=14)] = []
    timezone: str = Field(default="", max_length=60)
    device_name: str = Field(default="Mac", max_length=60)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            raise ValueError("not an e-mail address")
        return v.lower()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            if v:
                ZoneInfo(v)
        except Exception:
            raise ValueError("unknown time zone")
        return v


def _send_link(request: Request, cong: dict[str, Any], purpose: str, device_name: str) -> str:
    """E-mail a link; returns the claim secret for the app that asked."""
    h = _hear()
    token, claim = h.db.add_email_token(cong["id"], purpose, device_name, LINK_TTL_S)
    link = f"{_base_url(request)}/api/hc/verify?token={token}"
    if purpose == "verify":
        subject, what = "Confirm your e-mail for Hear Church", f"set up {cong['name']} on Hear Church"
    else:
        subject, what = "Add a Mac to your Hear Church congregation", f"add a Mac for {cong['name']}"
    h.mailer.send(cong["email"], subject, f"Open this link to {what}:\n\n{link}\n\n"
                  "It works for 24 hours. If you did not ask for this, ignore this e-mail.")
    return claim


@router.post("/api/hc/signup")
async def signup(request: Request, body: SignUp):
    _limit(request, "signup")
    h = _hear()
    if (body.lat is None) != (body.lng is None):
        raise HTTPException(400, "Give both lat and lng, or neither.")
    lat, lng = body.lat, body.lng
    if lat is None and body.address:   # a failed lookup never blocks sign-up: the app asks for a map pin instead
        found = await _geocode(body.address)
        lat, lng = found if found else (None, None)
    cong = h.db.create_congregation(
        church=body.church, name=body.name, unit_type=body.unit_type, email=body.email, stake=body.stake,
        address=body.address, lat=lat, lng=lng, timezone=body.timezone, city=await _town(lat, lng),
        meeting_times=[m.model_dump() for m in body.meeting_times])
    located = {"located": lat is not None, "lat": lat, "lng": lng}
    pin_note = "" if lat is not None else " We could not find the building on the map: please place its pin."
    if not h.verify_email:  # no e-mail confirmation for now: the congregation is live-capable at once
        h.db.set_verified(cong["id"])
        return {"ok": True, "slug": cong["slug"], "device_token": h.db.add_device(cong["id"], body.device_name),
                "connect_code": h.db.new_connect_code(cong["id"]),
                "congregation": _public(cong), **located, "message": "Signed up." + pin_note}
    claim = _send_link(request, cong, "verify", body.device_name)
    return {"ok": True, "slug": cong["slug"], "claim": claim, **located,
            "message": "Check your e-mail for the verification link." + pin_note}


async def _geocode(address: str) -> tuple[float, float] | None:
    try:
        return await asyncio.wait_for(_hear().geocoder.geocode(address), 15)
    except Exception as e:
        log.warning("geocode gave up: %s", e)
        return None


async def _town(lat: float | None, lng: float | None) -> str:
    """The town at the building, for "Harlem First Ward — New York, NY" in search; "" if unknown."""
    town = getattr(_hear().geocoder, "town", None)
    if lat is None or lng is None or town is None:
        return ""
    try:
        return await asyncio.wait_for(town(lat, lng), 10)
    except Exception as e:
        log.warning("reverse geocode gave up: %s", e)
        return ""


def _device_congregation(request: Request) -> dict[str, Any]:
    h = _hear()
    auth = request.headers.get("authorization", "")
    device = h.db.device_by_token(auth[7:]) if auth.startswith("Bearer ") else None
    cong = h.db.congregation(device["congregation_id"]) if device else None
    if cong is None or cong["hidden"]:
        raise HTTPException(403, "Unknown device: sign in again.")
    return cong


class Location(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    address: str | None = Field(default=None, max_length=200)


@router.post("/api/hc/location")
async def set_location(request: Request, body: Location):
    """The app confirms or drags the building's pin. With only an address, it is geocoded."""
    cong = _device_congregation(request)
    _limit(request, "location", f"cong{cong['id']}")
    h = _hear()
    if (body.lat is None) != (body.lng is None):
        raise HTTPException(400, "Give both lat and lng, or neither.")
    lat, lng = body.lat, body.lng
    if lat is None:
        if not body.address:
            raise HTTPException(400, "Give lat and lng, or an address.")
        found = await _geocode(body.address)
        if not found:
            raise HTTPException(422, "We could not find that address: place the pin on the map instead.")
        lat, lng = found
    h.db.set_location(cong["id"], lat, lng, body.address, city=await _town(lat, lng))
    return {"ok": True, "lat": lat, "lng": lng, "address": body.address if body.address is not None else cong["address"]}


def _page(title: str, text: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"<title>{html.escape(title)}</title><body style='font:18px system-ui;max-width:30em;margin:3em auto;"
                        f"padding:0 1em'><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>", status_code=status)


@router.get("/api/hc/verify", response_class=HTMLResponse)
async def verify(request: Request, token: str = ""):
    _limit(request, "verify")
    h = _hear()
    row = h.db.approve_email_token(token)
    if row is None:
        return _page("Link expired", "This link is no longer valid. Ask the app for a new one.", 410)
    if row["purpose"] == "verify":
        h.db.set_verified(row["congregation_id"])
    return _page("Done", "Your e-mail is confirmed. Go back to the Hear Church app: it continues by itself.")


class Claim(BaseModel):
    claim: str = Field(max_length=100)


@router.post("/api/hc/claim")
async def claim(request: Request, body: Claim):
    _limit(request, "claim")
    h = _hear()
    state, row = h.db.claim_email_token(body.claim)
    if state in ("unknown", "pending"):  # an unknown claim looks like a pending one (no probing)
        return JSONResponse({"pending": True}, status_code=202)
    if state == "expired":
        raise HTTPException(410, "That link expired: start again.")
    if state == "used":
        raise HTTPException(409, "That device token was already handed out: ask for a new link.")
    cong = h.db.congregation(row["congregation_id"])
    return {"ok": True, "device_token": h.db.add_device(cong["id"], row["device_name"]), "slug": cong["slug"],
            "congregation": _public(cong)}


class DeviceLink(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: str = Field(max_length=120)
    slug: str = Field(default="", max_length=80)
    device_name: str = Field(default="Mac", max_length=60)


@router.post("/api/hc/device-link")
async def device_link(request: Request, body: DeviceLink):
    """Add another Mac: e-mail a magic link. Always answers the same, so addresses can't be probed."""
    import secrets

    _limit(request, "email")
    email = body.email.lower()
    _limit(request, "email_addr", email)
    h = _hear()
    matches = [c for c in h.db.by_email(email) if not c["hidden"] and (not body.slug or c["slug"] == body.slug)]
    if len(matches) > 1:
        raise HTTPException(400, "More than one congregation uses that e-mail: say which one with slug.")
    claim_secret = _send_link(request, matches[0], "device", body.device_name) if matches else secrets.token_urlsafe(24)
    return {"ok": True, "claim": claim_secret, "message": "If that e-mail belongs to a congregation, a link was sent."}


@router.post("/api/hc/connect-code")
async def connect_code(request: Request):
    """(device token) A NEW connect code for this device's congregation; the old one stops working."""
    cong = _device_congregation(request)
    return {"connect_code": _hear().db.new_connect_code(cong["id"])}


class Connect(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    slug: str = Field(max_length=80)
    code: str = Field(max_length=20)
    device_name: str = Field(default="Mac", max_length=60)


def _locked(seconds: int) -> JSONResponse:
    return JSONResponse({"error": "locked", "retry_after": seconds}, status_code=429, headers={"Retry-After": str(seconds)})


@router.post("/api/hc/connect")
async def connect(request: Request, body: Connect):
    """Add a Mac with the congregation's connect code. 404 not_found, 403 bad_code, 429 locked."""
    h = _hear()
    ip = _ip(request)
    if wait := h.connect_ip.locked(ip):
        return _locked(wait)
    cong = h.db.by_slug(body.slug.lower())
    if cong is None or cong["hidden"]:
        h.connect_ip.fail(ip)   # guessing slugs counts too
        return JSONResponse({"error": "not_found"}, status_code=404)
    if wait := h.connect_cong.locked(cong["slug"]):
        return _locked(wait)    # even a correct code is refused while locked
    if not h.db.connect_code_ok(cong["id"], body.code):
        h.connect_ip.fail(ip)
        h.connect_cong.fail(cong["slug"])
        return JSONResponse({"error": "bad_code"}, status_code=403)
    return {"ok": True, "device_token": h.db.add_device(cong["id"], body.device_name or "Mac"), "slug": cong["slug"],
            "congregation": _public(cong), "billing": cong["billing"]}


# ------------------------------------------------------------------ billing: one-time own-key codes

class OwnKeyCode(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    code: str = Field(max_length=40)


@router.post("/api/hc/own-key-code/check")
async def own_key_code_check(request: Request, body: OwnKeyCode):
    """Is this an unused own-key code? Onboarding checks it before asking for the key; nothing is used up."""
    h = _hear()
    ip = _ip(request)
    if wait := h.own_key_ip.locked(ip):
        return _locked(wait)
    if not h.db.own_key_code_ok(body.code):
        h.own_key_ip.fail(ip)
        return JSONResponse({"error": "bad_code"}, status_code=403)
    return {"ok": True}


@router.post("/api/hc/own-key-code")
async def own_key_code_redeem(request: Request, body: OwnKeyCode):
    """(device token) Use the code up: this congregation pays with its own Anthropic key from now on."""
    cong = _device_congregation(request)
    h = _hear()
    ip = _ip(request)
    if wait := h.own_key_ip.locked(ip):
        return _locked(wait)
    if not h.db.redeem_own_key_code(body.code, cong["id"]):
        h.own_key_ip.fail(ip)
        return JSONResponse({"error": "bad_code"}, status_code=403)
    log.info("own-key code used by %s", cong["slug"])
    return {"ok": True, "billing": "own_key"}


@router.get("/api/hc/billing")
async def billing(request: Request):
    """(device token) How this congregation pays ("credits" or "own_key") and what it has left."""
    return billing_info(_device_congregation(request))


def billing_info(cong: dict[str, Any]) -> dict[str, Any]:
    """What the operator page and the app show: the balance in dollars and language-hours, whether the next
    meeting can start, and whether credit can be bought here (Stripe set up on this server)."""
    b = _hear().db.balance(cong["id"])
    credits = cong["billing"] == "credits"
    hours = round(b["left_min"] / 60, 2)
    owed = f"-${-b['balance_usd']:.2f}" if b["balance_usd"] < 0 else f"${b['balance_usd']:.2f}"
    if not credits:
        message = "Translation is paid with your own Anthropic key."
    elif not b["can_start"]:
        message = f"Out of credit (balance {owed}). Buy credit to start the next meeting."
    else:
        free = f", {b['free_left_min'] / 60:.1f} of them free" if b["free_left_min"] > 0 else ""
        message = f"Credit {owed} · {hours:.1f} language-hours left{free}."
    return {"billing": cong["billing"], "balance": b, "balance_usd": b["balance_usd"],
            "language_hours_left": hours if credits else None, "out_of_credit": credits and not b["can_start"],
            "usd_per_language_hour": CENTS_PER_LANGUAGE_HOUR / 100, "can_buy": credits and bool(stripe_keys()[0]),
            "buy_usd": list(BUY_USD), "buy_min_usd": BUY_MIN_USD, "buy_max_usd": BUY_MAX_USD, "message": message}


# ------------------------------------------------------------------ billing: buying credit (Stripe Checkout)
# Harrison's Stripe account (Automaddie). STRIPE_SECRET_KEY (sk_test_… / sk_live_…) creates Checkout sessions;
# STRIPE_WEBHOOK_SECRET (whsec_…) checks the signature of what Stripe posts to /api/stripe/webhook. Without the
# secret key the buy route answers 503 not_configured; without the webhook secret the webhook does. Only cards
# are offered: a card is paid when Checkout completes, so checkout.session.completed with payment_status "paid"
# is the one event that credits (a delayed method such as a bank debit would complete unpaid and be missed).

BUY_USD = (10, 25, 50, 100)        # the amounts the operator page offers
BUY_MIN_USD, BUY_MAX_USD = 5, 500  # one top-up, whole dollars ($5 = one language-hour)


def stripe_keys() -> tuple[str, str]:
    """(secret key, webhook signing secret) from the environment; "" when not set."""
    return os.environ.get("STRIPE_SECRET_KEY", "").strip(), os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()


def _stripe_client(key: str):
    import stripe

    return stripe.StripeClient(key)


class Buy(BaseModel):
    usd: int = Field(ge=BUY_MIN_USD, le=BUY_MAX_USD)


@router.post("/api/hc/checkout")
async def checkout(request: Request, body: Buy):
    """(device token) {usd} → {url}: a Stripe Checkout page where the church pays for that much credit. 503
    not_configured without a Stripe key on this server, 409 for a congregation that uses its own key."""
    cong = _device_congregation(request)
    _limit(request, "checkout", f"cong{cong['id']}")
    key = stripe_keys()[0]
    if not key:
        return JSONResponse({"error": "not_configured", "message": "Buying credit isn't set up on this server yet."},
                            status_code=503)
    if cong["billing"] != "credits":
        raise HTTPException(409, "This congregation pays with its own Anthropic key.")
    cents, base = body.usd * 100, _base_url(request)
    meta = {"congregation_id": str(cong["id"]), "slug": cong["slug"], "cents": str(cents)}
    hours = cents / CENTS_PER_LANGUAGE_HOUR
    params = {
        "mode": "payment",
        "payment_method_types": ["card"],   # paid at checkout: nothing arrives later unpaid
        "line_items": [{"quantity": 1, "price_data": {
            "currency": "usd", "unit_amount": cents,
            "product_data": {"name": "Hear Church credit",
                             "description": f"{hours:g} language-hours of live translation for {display_name(cong)}"}}}],
        "client_reference_id": str(cong["id"]),
        "metadata": meta,
        "payment_intent_data": {"metadata": meta, "description": f"Hear Church credit: {display_name(cong)}"},
        "success_url": f"{base}/api/hc/checkout/done",
        "cancel_url": f"{base}/api/hc/checkout/cancelled",
    }
    try:
        session = await asyncio.to_thread(_stripe_client(key).v1.checkout.sessions.create, params)
    except Exception as e:
        log.warning("stripe checkout for %s failed: %s", cong["slug"], e)
        raise HTTPException(502, "Stripe didn't answer: try again in a minute.")
    log.info("checkout %s: $%d for %s", session.id, body.usd, cong["slug"])
    return {"url": session.url, "id": session.id, "usd": body.usd}


@router.get("/api/hc/checkout/done", response_class=HTMLResponse)
async def checkout_done():
    return _page("Thank you", "Your payment went through. The credit shows in Hear Church within a minute; "
                              "you can close this page.")


@router.get("/api/hc/checkout/cancelled", response_class=HTMLResponse)
async def checkout_cancelled():
    return _page("No payment made", "Nothing was charged. You can close this page and buy credit later.")


@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe → hub. checkout.session.completed and paid: credit the congregation once per payment intent (Stripe
    sends an event again until it gets a 2xx, and may send it twice). 400 for a bad or stale signature; other
    events are acknowledged and ignored. A payment that can't be credited (unknown congregation, id reused) is
    logged and acknowledged, since a retry wouldn't change it; a database error answers 500 so Stripe retries."""
    import stripe

    secret = stripe_keys()[1]
    if not secret:
        return JSONResponse({"error": "not_configured"}, status_code=503)
    payload = await request.body()
    try:
        stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""), secret)
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(400, "Bad signature.")
    event = json.loads(payload)
    if event.get("type") != "checkout.session.completed":
        return {"ok": True, "credited": False}
    s = (event.get("data") or {}).get("object") or {}
    meta = s.get("metadata") or {}
    if s.get("payment_status") != "paid" or s.get("mode") != "payment" or s.get("currency") != "usd":
        log.warning("stripe: checkout %s not credited (%s, %s, %s)", s.get("id"), s.get("payment_status"),
                    s.get("mode"), s.get("currency"))
        return {"ok": True, "credited": False}
    try:
        cid = int(meta.get("congregation_id") or s.get("client_reference_id"))
        cents = int(s["amount_total"])
        credit, created = credit_top_up(cid, str(s.get("payment_intent") or s.get("id") or ""), cents)
    except (KeyError, ValueError, TypeError) as e:
        log.error("stripe: checkout %s paid but not credited: %r", s.get("id"), e)
        return {"ok": True, "credited": False}
    return {"ok": True, "credited": created, "credit_id": credit["id"]}


def credit_top_up(cid: int, payment_id: str, cents: int) -> tuple[dict[str, Any], bool]:
    """Credit a paid top-up to a congregation, once per payment: called by the Stripe webhook
    (checkout.session.completed → its payment_intent id and amount_total) and the operator's admin route. Returns
    (credit, created). KeyError for an unknown congregation, ValueError for a bad amount or a payment id already
    used differently."""
    h = _hear()
    if h.db.congregation(cid) is None:
        raise KeyError(cid)
    credit, created = h.db.add_credit(cid, payment_id, cents)
    if created:
        log.info("credit: %s cents to congregation %d (%s)", cents, cid, payment_id)
    return credit, created


# ------------------------------------------------------------------ listener side

def _public(c: dict[str, Any]) -> dict[str, Any]:
    return {k: c[k] for k in ("slug", "church", "name", "unit_type", "stake", "address", "meeting_times")}


def next_meeting(times: list[dict], tz: str, now: datetime | None = None) -> dict[str, Any] | None:
    """The soonest meeting time as {"label": "Sunday 10:00", "starts": epoch or None (no timezone known)}."""
    if not times:
        return None
    try:
        zone = ZoneInfo(tz) if tz else None
    except Exception:
        zone = None
    if zone is None:
        t = times[0]
        return {"label": f"{DAY_NAMES[DAYS.index(t['day'])]} {t['time']}", "day": t["day"], "time": t["time"], "starts": None}
    now = (now or datetime.now(zone)).astimezone(zone)
    best = None
    for t in times:
        hh, mm = map(int, t["time"].split(":"))
        d = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=(DAYS.index(t["day"]) - now.weekday()) % 7)
        if d <= now:
            d += timedelta(days=7)
        if best is None or d < best[0]:
            best = (d, t)
    return {"label": f"{DAY_NAMES[DAYS.index(best[1]['day'])]} {best[1]['time']}", "day": best[1]["day"],
            "time": best[1]["time"], "starts": best[0].timestamp()}


def _congregation(slug: str) -> dict[str, Any]:
    c = _hear().db.by_slug(slug)
    if c is None or c["hidden"] or not c["email_verified"]:
        raise HTTPException(404, "No such congregation.")
    return c


UNIT_WORDS = {"ward": "Ward", "branch": "Branch", "parish": "Parish"}
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def display_name(c: dict[str, Any]) -> str:
    """"Harlem First" + ward → "Harlem First Ward" (unless the name already says it)."""
    word = UNIT_WORDS.get(c["unit_type"], "")
    said = c["name"].lower().split()[-1:] in (["ward"], ["branch"], ["parish"], ["congregation"])
    return c["name"] if not word or said else f"{c['name']} {word}"


@router.get("/c/{slug}")
async def old_listener_link(slug: str):
    return RedirectResponse(f"/{slug}", status_code=301)


@pages.get("/{slug}", response_class=HTMLResponse)
async def listener_page(slug: str):
    """hearchurch.com/<slug>: the congregation's page (from its QR code or link). Not live → it waits."""
    from .. import main

    c = _hear().db.by_slug(slug) if SLUG_RE.fullmatch(slug) and slug not in RESERVED_SLUGS else None
    if c is None or c["hidden"] or not c["email_verified"]:
        return _page("Not found", "No congregation has this address. Find yours at hearchurch.com/listen.", 404)
    return main.page(WEB_DIR / "listener" / "index.html")


def _entry(c: dict[str, Any], now: float) -> dict[str, Any]:
    """One congregation for the nearby list / search: what a phone needs to show a row and compute its distance."""
    s = _hear().live.get(c["id"])
    nm = next_meeting(c["meeting_times"], c["timezone"])
    soon = bool(nm and nm["starts"] and 0 <= nm["starts"] - now <= SOON_S)
    return {"slug": c["slug"], "name": c["name"], "title": display_name(c), "city": c["city"], "church": c["church"],
            "unit_type": c["unit_type"], "stake": c["stake"], "lat": c["lat"], "lng": c["lng"], "live": s is not None,
            "starts_soon": soon and s is None, "languages": s.languages_public() if s else [],
            "starts_at": s.started if s else (nm["starts"] if nm else None),
            "next_meeting": {k: nm[k] for k in ("label", "day", "time")} if nm else None}


@router.get("/api/live")
async def nearby(request: Request, lat: float = Query(ge=-90, le=90), lng: float = Query(ge=-180, le=180)):
    """Congregations within a mile of a phone (normally the one whose chapel it is in), live ones first. The
    reply is a rough box, not distances: the phone works those out. Nothing about the request is kept."""
    _limit(request, "live")
    h, now = _hear(), time.time()
    box = bounding_box(lat, lng, NEARBY_MI)
    s, n, w, e = box
    w2, e2 = (-180.0, 180.0) if e - w >= 360 else (((w + 180) % 360) - 180, ((e + 180) % 360) - 180)
    out = [x for x in (_entry(c, now) for c in h.db.in_box(s, n, w2, e2)) if in_box(x["lat"], x["lng"], box)]
    out.sort(key=lambda x: (not x["live"], not x["starts_soon"], x["title"].lower()))
    return JSONResponse({"radius_mi": NEARBY_MI, "results": out}, headers={"Cache-Control": "no-store"})


@router.get("/api/hc/search")
async def search(request: Request, q: str = Query(default="", max_length=80)):
    _limit(request, "search")
    q = " ".join(q.split())
    if len(q) < 2:
        return {"results": []}
    now = time.time()
    return {"results": [_entry(c, now) for c in _hear().db.search(q, 10)]}


@router.get("/listen", response_class=HTMLResponse)   # its address; hearchurch.com/ becomes the marketing site
@router.get("/find", response_class=HTMLResponse)
async def find_page():
    from .. import main

    return main.page(WEB_DIR / "find" / "index.html")


@router.get("/api/hc/c/{slug}")
async def congregation_info(slug: str):
    h = _hear()
    c = _congregation(slug)
    s = h.live.get(c["id"])
    return {**_public(c), "live": s is not None, "languages": s.languages_public() if s else [],
            "next_meeting": next_meeting(c["meeting_times"], c["timezone"])}


@router.get("/c/{slug}/stream/{code}.mp3")
async def stream(slug: str, code: str):
    from .. import main

    s = _hear().live.get(_congregation(slug)["id"])
    if s is None or code not in s.offered:
        raise HTTPException(404, "Not live right now.")
    return main.mp3_response(s.streams[code], "Live translation")


@router.websocket("/c/{slug}/ws/listen")
async def ws_listen(ws: WebSocket, slug: str):
    h = _hear()
    c = h.db.by_slug(slug)
    if c is None or c["hidden"] or not c["email_verified"]:
        await ws.close(code=4404)
        return
    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        hello = {}
    s = h.live.get(c["id"])
    init: dict[str, Any] = {
        "type": "init", "boot": h.live.started, "server_time": time.time(),
        "congregation": {"name": display_name(c), "church": c["church"], "slug": c["slug"]},
        "next_meeting": next_meeting(c["meeting_times"], c["timezone"]),
        "session": None, "live": False, "lang": "", "on": True, "languages": [], "history": []}
    if s is None:  # not live: wait on the congregation's room; the page reloads when a session starts
        await h.live.waiting_hub(c["id"]).serve(ws, [init])
        return
    lang = hello.get("lang") if hello.get("lang") in s.offered else s.offered[0]
    same = hello.get("boot") == h.live.started and hello.get("session") == s.code and hello.get("lang") == lang
    last = int(hello.get("last_seq") or 0) if same else 0
    init.update(session=s.code, live=True, lang=lang, languages=s.languages_public(), speaker=s.speaker,
                history=s.hubs[lang].replay_after(last))
    await s.hubs[lang].serve(ws, [init])


# ------------------------------------------------------------------ engine link

@router.websocket("/ws/engine")
async def ws_engine(ws: WebSocket):
    """A computer publishing meetings: the legacy engine token (app/engine.py) or a Hear Church device token
    (app/hear/live.py)."""
    from .. import main

    auth = ws.headers.get("authorization", "")
    legacy = main.S.settings.engine.token
    if legacy and hmac.compare_digest(auth, f"Bearer {legacy}"):
        await main.S.engine_hub.serve(ws)
        return
    device = H.db.device_by_token(auth[7:]) if H and auth.startswith("Bearer ") else None
    cong = H.db.congregation(device["congregation_id"]) if device else None
    if cong is None or cong["hidden"]:
        await ws.close(code=4403)
        return
    await H.live.serve_device(ws, device)


# ------------------------------------------------------------------ admin (operator sign-in)

@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    from .. import main

    return main._operator_page(request, WEB_DIR / "admin" / "index.html")


@router.get("/api/admin/congregations")
async def admin_list(request: Request):
    from .. import main

    main.require_operator(request)
    h = _hear()
    out = []
    for c in h.db.list_congregations():
        s = h.live.get(c["id"])
        out.append({**{k: c[k] for k in ("id", "slug", "church", "name", "unit_type", "stake", "address", "email",
                                         "email_verified", "hidden", "created_at", "billing")},
                    "devices": len(h.db.devices(c["id"])), "live": s is not None,
                    "listeners": s.listeners() if s else 0,
                    "last_session": (h.db.sessions(c["id"]) or [{}])[-1].get("started_at"),
                    "balance": h.db.balance(c["id"])})
    return {"congregations": out}


class NewOwnKeyCode(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=120)


@router.post("/api/admin/own-key-codes")
async def admin_mint_own_key_code(request: Request, body: NewOwnKeyCode):
    """A one-time own-key code for one church. Shown only in this answer: the hub keeps just its hash."""
    from .. import main

    main.require_operator(request)
    code = _hear().db.mint_own_key_code(body.label)
    log.info("own-key code made for %r", body.label)
    return {"code": code, "label": body.label}


@router.get("/api/admin/own-key-codes")
async def admin_own_key_codes(request: Request):
    from .. import main

    main.require_operator(request)
    return {"codes": _hear().db.own_key_codes()}


class TopUp(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    payment_id: str = Field(min_length=1, max_length=255)
    cents: int = Field(gt=0, le=10_000_000)


@router.post("/api/admin/congregations/{cid}/credits")
async def admin_credit(request: Request, cid: int, body: TopUp):
    """Credit a top-up by hand (or from the Stripe step), idempotent on the payment id."""
    from .. import main

    main.require_operator(request)
    try:
        credit, created = credit_top_up(cid, body.payment_id, body.cents)
    except KeyError:
        raise HTTPException(404, "No such congregation.")
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"created": created, "credit": credit, "balance": _hear().db.balance(cid)}


@router.post("/api/admin/congregations/{cid}/hidden")
async def admin_hide(request: Request, cid: int):
    from .. import main

    main.require_operator(request)
    h = _hear()
    hidden = bool((await request.json()).get("hidden", True))
    if not h.db.set_hidden(cid, hidden):
        raise HTTPException(404)
    s = h.live.get(cid)
    if hidden and s is not None:
        h.live.end(s, why="This congregation was hidden by the administrator.")  # it stops being live
    return {"ok": True, "hidden": hidden}
