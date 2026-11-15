"""Hear Church HTTP/WebSocket routes: sign-up, device tokens, /c/<slug> listener pages, admin list.

Sign-up flow (JSON, for the Mac app; sign-up is open, no manual approval):
  POST /api/hc/signup       church, name, unit_type, email, [stake, address, lat, lng, meeting_times, timezone,
                            device_name, code] → {slug, claim}; an e-mail with a verification link is sent.
                            code: a code from Harrison (app/hear/payments.py), checked first (403 bad_code, nothing
                            made) and redeemed for the new congregation (the answer's "code").
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
                            The answer carries the congregation's billing ("credits", "own_key" or "internal"; see
                            "Billing types" below for what an app that doesn't know "internal" gets).
Accounts (V3.2, docs/v3-languages.md "Accounts"): several e-mails per congregation, one main (billing) one;
  sign-up's `email` is the main one, `emails` adds more.
  GET  /api/hc/account      (device token) → {emails: [{email, main}], main_email, devices: [{…, this}]}
  POST /api/hc/emails       (device token) {email, main?} add (main: true also makes it main); 409 too_many
  POST /api/hc/emails/main  (device token) {email} → 404 not_found unless it is one of them
  POST /api/hc/emails/remove (device token) {email} → 409 main, 404 not_found
  POST /api/hc/pairing-code (device token) → {code: "7KQM-X2PA", expires_at, expires_in}: "Add device", 15 min, once
  POST /api/hc/email-code   {email} → always {ok, message}: a code to every address of the congregation that has it
                            (Resend; 503 not_configured without RESEND_API_KEY; 429 per IP and per address)
  POST /api/hc/pair         {code, device_name} → {device_token, slug, congregation, billing}; 403 bad_code,
                            429 locked. Takes both kinds of code.
  POST /api/hc/slug         (device token) {slug} → {ok, slug, url, aliases}: the listener link becomes
                            hearchurch.com/<slug>; the old slug redirects there for good (slug_aliases, so printed QR
                            codes keep working). 422 bad_slug (format), 409 reserved / taken.
Billing (prepaid credits, or the congregation's own Anthropic key through a one-time code Harrison makes):
  Saved card, 1-click and automatic top-ups, codes (own key, free hours, % off): app/hear/payments.py.
  POST /api/hc/own-key-code/check {code} (deprecated: /api/hc/codes/check) → {ok} or 403 bad_code / 429 locked. Doesn't use the code up: onboarding
                            asks for the code before the key, and the congregation only exists after sign-up.
  POST /api/hc/own-key-code (device token) {code} → {ok, billing: "own_key"}; uses the code up for this
                            congregation (asking again is fine). 403 bad_code (unknown, or used by another), 429 locked.
  GET  /api/hc/billing      (device token) → {billing, balance: {left_min, free_left_min, free_resets_at, balance_min,
                            balance_usd, can_start, …}, balance_usd, language_hours_left, free_hours_left,
                            paid_hours_left, out_of_credit, can_buy, bundles: [{usd, hours, usd_per_hour}], buy_usd
                            (the bundle prices, for 0.3.0 apps), message}
  GET  /api/hc/usage        (device token) → {billing, meetings: [{started_at, ended_at, languages, language_minutes,
                            language_hours, free_minutes, paid_minutes, cost_usd, billing}], charges: [{kind, at,
                            amount_usd}]}: the Billing tab's history, newest first (?meetings=, ?charges= limits).
  POST /api/hc/checkout     (device token) {usd, save_card?} → {url} (less a pending % off code: percent_off,
                            charged_usd; save_card keeps the card for 1-click): a Stripe Checkout page for the bundle at that price ($20,
                            $50, $100 or $250); any other amount is 400 not_a_bundle with a message naming the
                            bundles (0.3.0 apps only offer the prices in buy_usd, so they always hit a bundle).
                            503 not_configured without STRIPE_SECRET_KEY. Back to /api/hc/checkout/done or /cancelled.
  POST /api/stripe/webhook  Stripe → hub, signed with STRIPE_WEBHOOK_SECRET. payment_intent.succeeded credits 1-click
                            and automatic top-ups (and Checkout's, once either way); payment_intent.payment_failed,
                            setup_intent.succeeded and checkout.session.completed in setup mode: payments.py.
                            checkout.session.completed (paid) calls
                            credit_top_up with its payment_intent and amount_total (the bundle's hours), once per
                            payment;
                            charge.refunded takes amount_refunded back off the top-up with that payment_intent.
  POST /api/hc/translate    (device token) translation on credits through the hub's Anthropic key (app/hear/proxy.py).
  POST /api/admin/congregations/<id>/credits {payment_id, cents, minutes?} (operator) → {created, credit, balance}:
                            credit a top-up (minutes default: the bundle's hours at a bundle price, else $5 per hour). Idempotent on payment_id (the Stripe step calls `credit_top_up` with the
                            payment's id); 409 when that id was credited with another amount or congregation.
  POST /api/admin/congregations/<id>/billing {billing: "credits"|"own_key"|"internal"} (operator; {mode: …} works
                            too) → {ok, billing: <as GET /api/hc/billing>}; 400 unknown type, 404 unknown congregation.
Billing types: "credits" (prepaid), "own_key" (the church's own Anthropic key, straight from the Mac), "internal"
  (Harrison's own congregations: the hub translates at no charge with its Claude Code login, app/hear/internal.py;
  metered, never refused for balance, never shown publicly). Device answers say "internal" only to an app that sends
  the header `X-HC-Billing-Types: …internal…` (KNOWS_HEADER); older apps (0.3.0 build 18/19) get "credits" instead,
  which makes them translate through the hub (handled as internal there); their Billing tab shows 0.0 hours and no
  buy buttons until they update.
  POST /api/admin/own-key-codes {label} (operator) → {code, label}: the only time the code is shown.
  GET  /api/admin/own-key-codes (operator) → {codes: [{id, label, created_at, used_at, voided_at, status, used_by…}]}
  More operator JSON for Siteboard (stats, ledger, refunds, void a code, one congregation): app/hear/admin.py.
The claim secret stays with the app that asked, so a link opened by a mail scanner yields no token.
Sign-up geocodes an address that came without lat/lng (Nominatim, or Mapbox with HEARCHURCH_MAPBOX_TOKEN). The
answer says whether it worked ("located"); if not, lat/lng stay empty and the app should ask for a map pin and
send it to /api/hc/location.
Listener side: hearchurch.com/ (the finder: by location, then by name), hearchurch.com/<slug> (one congregation:
its live meeting, or "wait for the meeting to start"; /c/<slug> and old slugs redirect there), GET /api/live?lat&lng
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

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import HOME, LOG_DIR, WEB_DIR
from .db import (BUNDLES, DEVICE_CODE_TTL_S, FREE_SECONDS, LEGACY_CENTS_PER_LANGUAGE_HOUR, RESERVED_SLUGS, Database,
                 bundle)
from .geo import Geocoder, bounding_box, hide_coordinates_in_access_log, in_box
from .live import Live
from .mail import Mailer, RateLimiter, Resend

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
                 verify_email: bool = True, resend: Resend | None = None):
        self.db, self.mailer, self.live = db, mailer, live
        self.resend = resend or Resend()
        self.verify_email = verify_email
        self.geocoder = geocoder or Geocoder()
        self.connect_ip = FailureLimiter(5, 15 * 60)     # wrong codes per IP
        self.connect_cong = FailureLimiter(10, 3600)     # wrong codes per congregation, then it is locked
        self.own_key_ip = FailureLimiter(10, 3600)       # wrong own-key codes per IP
        self.pair_ip = FailureLimiter(10, 15 * 60)       # wrong add-a-Mac codes per IP
        self.codes_cong = FailureLimiter(20, 3600)       # wrong codes (own key, free hours, % off) per congregation
        from . import payments
        live.after_end = payments.after_meeting          # maybe an automatic top-up once a meeting ends
        self.limits = {"live": RateLimiter(60, 60), "search": RateLimiter(60, 60), "location": RateLimiter(30, 3600),
                       "signup": RateLimiter(5, 3600), "email": RateLimiter(10, 3600),
                       "email_addr": RateLimiter(3, 3600), "claim": RateLimiter(1200, 3600),
                       "verify": RateLimiter(60, 3600), "checkout": RateLimiter(20, 3600),
                       "email_code": RateLimiter(10, 3600), "email_code_addr": RateLimiter(3, 3600),
                       "pair_code": RateLimiter(30, 3600), "emails": RateLimiter(60, 3600),
                       "slug": RateLimiter(20, 3600)}


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


def email_address(v: str) -> str:
    v = (v or "").strip()
    if len(v) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
        raise ValueError("not an e-mail address")
    return v.lower()


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
    emails: Annotated[list[str], Field(max_length=9)] = []   # more addresses besides the main one (`email`)
    code: str = Field(default="", max_length=40)   # a code from Harrison (own key, free hours, % off): app/hear/payments.py

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return email_address(v)

    @field_validator("emails")
    @classmethod
    def _emails(cls, v: list[str]) -> list[str]:
        return [email_address(e) for e in v]

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            if v:
                ZoneInfo(v)
        except Exception:
            raise ValueError("unknown time zone")
        return v


def _send_link(request: Request, cong: dict[str, Any], purpose: str, device_name: str, to: str = "") -> str:
    """E-mail a link; returns the claim secret for the app that asked."""
    h = _hear()
    token, claim = h.db.add_email_token(cong["id"], purpose, device_name, LINK_TTL_S)
    link = f"{_base_url(request)}/api/hc/verify?token={token}"
    if purpose == "verify":
        subject, what = "Confirm your e-mail for Hear Church", f"set up {cong['name']} on Hear Church"
    else:
        subject, what = "Add a Mac to your Hear Church congregation", f"add a Mac for {cong['name']}"
    h.mailer.send(to or cong["email"], subject, f"Open this link to {what}:\n\n{link}\n\n"
                  "It works for 24 hours. If you did not ask for this, ignore this e-mail.")
    return claim


@router.post("/api/hc/signup")
async def signup(request: Request, body: SignUp):
    _limit(request, "signup")
    h = _hear()
    if (body.lat is None) != (body.lng is None):
        raise HTTPException(400, "Give both lat and lng, or neither.")
    if body.code:   # checked before the congregation is made, so a typo can be fixed and sent again
        from . import payments
        if wait := h.own_key_ip.locked(_ip(request)):
            return _locked(wait)
        if h.db.check_code(body.code) is None:
            h.own_key_ip.fail(_ip(request))
            return payments._bad_code()
    lat, lng = body.lat, body.lng
    if lat is None and body.address:   # a failed lookup never blocks sign-up: the app asks for a map pin instead
        found = await _geocode(body.address)
        lat, lng = found if found else (None, None)
    cong = h.db.create_congregation(
        church=body.church, name=body.name, unit_type=body.unit_type, email=body.email, stake=body.stake,
        address=body.address, lat=lat, lng=lng, timezone=body.timezone, city=await _town(lat, lng),
        meeting_times=[m.model_dump() for m in body.meeting_times])
    for e in body.emails:   # the main one (`email`) was added with the congregation
        h.db.add_email(cong["id"], e)
    redeemed: dict[str, Any] = {}
    if body.code:
        r = payments.redeem_for(request, cong, body.code)
        redeemed = {"code": {k: v for k, v in r.items() if k != "ok"} if isinstance(r, dict)
                    else {"error": "bad_code", "message": payments.BAD_CODE}}
        cong = h.db.congregation(cong["id"])
    located = {"located": lat is not None, "lat": lat, "lng": lng}
    pin_note = "" if lat is not None else " We could not find the building on the map: please place its pin."
    if not h.verify_email:  # no e-mail confirmation for now: the congregation is live-capable at once
        h.db.set_verified(cong["id"])
        return {"ok": True, "slug": cong["slug"], "device_token": h.db.add_device(cong["id"], body.device_name),
                "connect_code": h.db.new_connect_code(cong["id"]), "emails": h.db.emails(cong["id"]),
                "congregation": _public(cong), "billing": billing_for(request, cong["billing"]), **located, **redeemed,
                "message": "Signed up." + pin_note}
    claim = _send_link(request, cong, "verify", body.device_name)
    return {"ok": True, "slug": cong["slug"], "claim": claim, "emails": h.db.emails(cong["id"]), **located, **redeemed,
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
    return device_and_congregation(request)[1]


def device_and_congregation(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    """The Mac (device token in "Authorization: Bearer …") and its congregation; 403 for anyone else."""
    h = _hear()
    auth = request.headers.get("authorization", "")
    device = h.db.device_by_token(auth[7:]) if auth.startswith("Bearer ") else None
    cong = h.db.congregation(device["congregation_id"]) if device else None
    if cong is None or cong["hidden"]:
        raise HTTPException(403, "Unknown device: sign in again.")
    return device, cong


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
    matches = [c for c in h.db.congregations_with_email(email) if not body.slug or c["slug"] == body.slug]
    if len(matches) > 1:
        raise HTTPException(400, "More than one congregation uses that e-mail: say which one with slug.")
    claim_secret = _send_link(request, matches[0], "device", body.device_name, email) if matches else secrets.token_urlsafe(24)
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
            "congregation": _public(cong), "billing": billing_for(request, cong["billing"])}


# ------------------------------------------------------------------ accounts (V3.2): e-mails, add-a-Mac codes

def _account(cid: int, device_id: int | None = None) -> dict[str, Any]:
    h = _hear()
    return {"emails": h.db.emails(cid), "main_email": h.db.main_email(cid),
            "devices": [{**d, "this": d["id"] == device_id} for d in h.db.devices(cid)]}


@router.get("/api/hc/account")
async def account(request: Request):
    """(device token) The congregation's e-mails (main first) and its Macs; "this" marks the one asking."""
    device, cong = device_and_congregation(request)
    return {"ok": True, "slug": cong["slug"], **_account(cong["id"], device["id"])}


class EmailChange(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: str = Field(max_length=120)
    main: bool = False

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return email_address(v)


@router.post("/api/hc/emails")
async def emails_add(request: Request, body: EmailChange):
    """(device token) Add an address; main: true also makes it the main (billing) one. Adding one that is
    already there is fine (and with main: true just moves the main mark)."""
    device, cong = device_and_congregation(request)
    _limit(request, "emails", f"cong{cong['id']}")
    try:
        added = _hear().db.add_email(cong["id"], body.email, main=body.main)
    except ValueError as e:
        return JSONResponse({"error": "too_many", "message": str(e)}, status_code=409)
    log.info("%s: e-mail %s%s", cong["slug"], "added" if added else "kept", " as main" if body.main else "")
    if body.main:
        await _sync_receipts(cong["id"])
    return {"ok": True, "added": added, **_account(cong["id"], device["id"])}


async def _sync_receipts(cid: int) -> None:
    from . import payments

    await payments.sync_customer_email(cid)


@router.post("/api/hc/emails/main")
async def emails_main(request: Request, body: EmailChange):
    """(device token) Make one of the congregation's addresses the main one. 404 not_found if it isn't one."""
    device, cong = device_and_congregation(request)
    _limit(request, "emails", f"cong{cong['id']}")
    if not _hear().db.set_main_email(cong["id"], body.email):
        return JSONResponse({"error": "not_found", "message": "Add that address first."}, status_code=404)
    log.info("%s: main e-mail changed", cong["slug"])
    await _sync_receipts(cong["id"])
    return {"ok": True, **_account(cong["id"], device["id"])}


@router.post("/api/hc/emails/remove")
async def emails_remove(request: Request, body: EmailChange):
    """(device token) Remove an address. 409 main (make another one main first), 404 not_found."""
    device, cong = device_and_congregation(request)
    _limit(request, "emails", f"cong{cong['id']}")
    state = _hear().db.remove_email(cong["id"], body.email)
    if state == "main":
        return JSONResponse({"error": "main", "message": "That is the main e-mail: make another one main first."},
                            status_code=409)
    if state == "unknown":
        return JSONResponse({"error": "not_found", "message": "That address isn't on this congregation."},
                            status_code=404)
    log.info("%s: e-mail removed", cong["slug"])
    return {"ok": True, **_account(cong["id"], device["id"])}


class DeviceRevoke(BaseModel):
    id: int


@router.post("/api/hc/devices/revoke")
async def devices_revoke(request: Request, body: DeviceRevoke):
    """(device token) Settings > Devices: revoke another Mac of this congregation (its token stops working, a meeting
    it runs ends). 409 this_device for the Mac asking (it signs out instead), 404 not_found for any other id."""
    device, cong = device_and_congregation(request)
    h = _hear()
    _limit(request, "emails", f"cong{cong['id']}")
    if body.id == device["id"]:
        return JSONResponse({"error": "this_device", "message": "This is the Mac you're using: sign it out instead."},
                            status_code=409)
    if body.id not in {d["id"] for d in h.db.devices(cong["id"])}:
        return JSONResponse({"error": "not_found", "message": "That Mac isn't connected to this congregation."},
                            status_code=404)
    h.db.revoke_device(body.id)
    h.live.revoked(body.id)
    log.info("%s: device %d revoked by device %d", cong["slug"], body.id, device["id"])
    return {"ok": True, **_account(cong["id"], device["id"])}


@router.post("/api/hc/pairing-code")
async def pairing_code(request: Request):
    """(device token) "Add device": a one-time code (8 letters/digits, 15 minutes) another Mac enters to join this
    congregation. A new code replaces the previous unused one."""
    cong = _device_congregation(request)
    _limit(request, "pair_code", f"cong{cong['id']}")
    code, expires = _hear().db.new_device_code(cong["id"], "pair")
    log.info("%s: pairing code made", cong["slug"])
    return {"ok": True, "code": code, "expires_at": expires, "expires_in": int(expires - time.time())}


class Pair(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    code: str = Field(max_length=40)
    device_name: str = Field(default="Mac", max_length=60)


class SlugChange(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    slug: str = Field(max_length=200)


SLUG_MIN, SLUG_MAX = 3, 60


def _slug_answer(cong: dict[str, Any], request: Request) -> dict[str, Any]:
    h = _hear()
    return {"ok": True, "slug": cong["slug"], "url": f"{_base_url(request)}/{cong['slug']}",
            "aliases": [a["slug"] for a in h.db.aliases(cong["id"])]}


@router.post("/api/hc/slug")
async def change_slug(request: Request, body: SlugChange):
    """(device token) The congregation's listener link becomes hearchurch.com/<slug>. The old slug keeps
    redirecting to the new one for good (printed QR codes). Lowercase letters, digits and single dashes."""
    cong = _device_congregation(request)
    slug = body.slug.lower()
    if not (SLUG_MIN <= len(slug) <= SLUG_MAX and SLUG_RE.fullmatch(slug)):   # typing mistakes don't count below
        return JSONResponse({"error": "bad_slug", "message": f"Use {SLUG_MIN} to {SLUG_MAX} lowercase letters, digits "
                                                             "and dashes (no dash at the start or end)."},
                            status_code=422)
    _limit(request, "slug", f"cong{cong['id']}")
    result = _hear().db.change_slug(cong["id"], slug)
    if result == "reserved":
        return JSONResponse({"error": "reserved", "message": f"hearchurch.com/{slug} is one of the site's own pages. "
                                                             "Choose another."}, status_code=409)
    if result == "taken":
        return JSONResponse({"error": "taken", "message": f"hearchurch.com/{slug} is already taken. Choose another."},
                            status_code=409)
    if result == "not_found":
        raise HTTPException(403, "Unknown device: sign in again.")
    if result == "ok":
        log.info("congregation %d moved from /%s to /%s", cong["id"], cong["slug"], slug)
        s = _hear().live.get_any(cong["id"])
        if s is not None:
            s.slug = slug
    return _slug_answer(_hear().db.congregation(cong["id"]), request)


@router.post("/api/hc/pair")
async def pair(request: Request, body: Pair):
    """A new Mac enters a pairing code ("Add device" on another Mac) or a code from the e-mail: it gets a device token
    for that congregation. 403 bad_code (unknown, used, or expired), 429 locked after too many wrong codes."""
    h = _hear()
    ip = _ip(request)
    if wait := h.pair_ip.locked(ip):
        return _locked(wait)
    got = h.db.redeem_device_code(body.code, body.device_name or "Mac")
    if got is None:
        h.pair_ip.fail(ip)
        return JSONResponse({"error": "bad_code", "message": "That code is wrong or has expired: ask for a new one."},
                            status_code=403)
    cong, token = got
    log.info("%s: a Mac connected by code", cong["slug"])
    return {"ok": True, "device_token": token, "slug": cong["slug"], "congregation": _public(cong),
            "billing": billing_for(request, cong["billing"])}


class EmailCode(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    email: str = Field(max_length=120)


CODE_EMAIL_SUBJECT = "Your Hear Church code"


def code_email_text(cong: dict[str, Any], code: str) -> str:
    return (f"Your code to connect a Mac to {display_name(cong)} on Hear Church:\n\n    {code}\n\n"
            f"Enter it in the Hear Church app under \"Connect by code\". It works once, for "
            f"{DEVICE_CODE_TTL_S // 60} minutes.\n\nSomeone asked for this code on a new Mac. The same e-mail went to "
            "every address of your congregation. If nobody in your congregation asked for it, ignore this e-mail: "
            "without the code nothing happens.\n\nHear Church\n")


async def _send_codes(cong: dict[str, Any]) -> None:
    """Make an e-mail code and send it to each of the congregation's addresses (separately: they don't see each
    other). Runs after the answer went out, so the answer's timing doesn't tell whether the address is known."""
    h = _hear()
    code, _ = h.db.new_device_code(cong["id"], "email")
    to = [e["email"] for e in h.db.emails(cong["id"])]
    sent = 0
    for addr in to:
        sent += await h.resend.send(addr, CODE_EMAIL_SUBJECT, code_email_text(cong, code))
    log.info("%s: device code e-mailed to %d of %d addresses", cong["slug"], sent, len(to))


@router.post("/api/hc/email-code")
async def email_code(request: Request, body: EmailCode, tasks: BackgroundTasks):
    """"Email me a code" on a new Mac: if the address belongs to a congregation, a one-time code goes to ALL of that
    congregation's addresses; the Mac then sends it to /api/hc/pair. The answer is the same either way (no probing
    which addresses exist). 503 not_configured without RESEND_API_KEY; 429 past the limits per IP and per address."""
    h = _hear()
    if not h.resend.configured():
        return JSONResponse({"error": "not_configured", "message": "E-mail isn't set up on this server yet: use a "
                             "code from \"Add device\" on a Mac that is already connected."}, status_code=503)
    _limit(request, "email_code")
    email = body.email.strip().lower()
    _limit(request, "email_code_addr", email)
    for cong in h.db.congregations_with_email(email)[:3]:
        tasks.add_task(_send_codes, cong)
    return {"ok": True, "message": "If that e-mail belongs to a congregation, a code was sent to all of its "
                                   "addresses. It works for 15 minutes."}


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
    """(device token) How this congregation pays ("credits", "own_key" or "internal") and what it has left."""
    return billing_info(_device_congregation(request), request)


@router.get("/api/hc/usage")
async def usage_history(request: Request, meetings: int = Query(default=50, ge=1, le=500),
                        charges: int = Query(default=50, ge=1, le=500)):
    """(device token) The Billing tab's history, newest first: meetings with their language-hours and cost, and the
    credit bought or refunded."""
    from . import admin

    cong = _device_congregation(request)
    rows = admin.ledger(_hear(), cong["id"])
    by_session = {r["session_id"]: r for r in rows if r["kind"] == "usage"}
    out = []
    for m in reversed(_hear().db.sessions(cong["id"])[-meetings:]):
        u = by_session.get(m["id"]) or {}
        mins = u.get("minutes", 0)
        out.append({"started_at": m["started_at"], "ended_at": m["ended_at"], "billing": billing_for(request, m["billing"]),
                    "languages": m["languages"], "language_minutes": u.get("language_minutes", {}),
                    "language_hours": round(mins / 60, 2), "minutes": mins,
                    "free_minutes": u.get("free_minutes", 0), "paid_minutes": u.get("paid_minutes", 0),
                    "cost_usd": u.get("cost_usd", 0)})
    paid = [{"kind": r["kind"], "at": r["at"], "amount_usd": r["amount_usd"],
             **({"hours": round(r["minutes"] / 60, 2)} if "minutes" in r else {})} for r in rows if r["kind"] != "usage"]
    return {"billing": billing_for(request, cong["billing"]), "meetings": out, "charges": paid[:charges]}


KNOWS_HEADER = "X-HC-Billing-Types"   # sent by apps that know "internal" (the Mac app after 0.3.0 build 19)
INCLUDED_MESSAGE = "Included: no charge."


def billing_for(request: Request | None, billing: str) -> str:
    """The billing type as this caller should see it. "internal" goes only to apps that say they know it
    (KNOWS_HEADER); older Mac apps (0.3.0 builds 18 and 19, which know only "credits" and "own_key") get "credits",
    which makes their engine translate through the hub (the proxy treats the congregation as internal regardless).
    Their app_version ("0.3.0") can't tell the builds apart, so the header decides. No request: the operator."""
    if billing != "internal" or request is None:
        return billing
    return billing if "internal" in request.headers.get(KNOWS_HEADER, "") else "credits"


def bundle_json(b: tuple[int, int]) -> dict[str, Any]:
    """A bundle as the app and the operator see it: {usd: 50, hours: 11, usd_per_hour: 4.55}."""
    return {"usd": b[0] // 100, "hours": b[1], "usd_per_hour": round(b[0] / 100 / b[1], 2)}


def _hours(h: float) -> str:
    return f"{h:.1f}".removesuffix(".0")


def billing_info(cong: dict[str, Any], request: Request | None = None) -> dict[str, Any]:
    """What the operator page and the app show: this month's free hour, the paid hours left (and what they are worth),
    whether the next meeting can start, the bundles, and whether credit can be bought here (Stripe set up on this
    server). With the device's request, "billing" is as billing_for() says (operator views pass none and see the
    real type)."""
    from . import payments

    b = _hear().db.balance(cong["id"])
    credits = cong["billing"] == "credits"
    hours = round(b["left_min"] / 60, 2)
    free_h, paid_h = round(b["free_left_min"] / 60, 2), round(b["balance_min"] / 60, 2)
    owed = f"-${-b['balance_usd']:.2f}" if b["balance_usd"] < 0 else f"${b['balance_usd']:.2f}"
    if cong["billing"] == "internal":
        message = INCLUDED_MESSAGE
    elif not credits:
        message = "Translation is paid with your own API key."
    elif not b["can_start"]:
        message = (f"Out of credit (balance {owed}). This month's free hour is used up: buy credit to start the next "
                   "meeting, or wait for next month's free hour.")
    else:
        message = (f"{_hours(free_h)} free hour{'' if free_h == 1 else 's'} left this month · "
                   f"{_hours(max(paid_h, 0))} paid hours left.")
    return {"billing": billing_for(request, cong["billing"]), "balance": b, "balance_usd": b["balance_usd"],
            "language_hours_left": hours if credits else None,
            "free_hours_left": free_h if credits else None, "paid_hours_left": paid_h if credits else None,
            "free_hours_per_month": FREE_SECONDS / 3600, "free_resets_at": b["free_resets_at"],
            "out_of_credit": credits and not b["can_start"],
            "bundles": [bundle_json(x) for x in BUNDLES],
            # 0.3.0 apps make their buy buttons from buy_usd and label them usd / usd_per_language_hour: the
            # bundle prices, and the old $5 rate (so they show fewer hours than the bundle gives; Checkout says it right)
            "usd_per_language_hour": LEGACY_CENTS_PER_LANGUAGE_HOUR / 100,
            "can_buy": credits and bool(stripe_keys()[0]),
            "buy_usd": [x[0] // 100 for x in BUNDLES], "message": message, **payments.billing_extras(cong)}


# ------------------------------------------------------------------ billing: buying credit (Stripe Checkout)
# Harrison's Stripe account (Automaddie). STRIPE_SECRET_KEY (sk_test_… / sk_live_…) creates Checkout sessions;
# STRIPE_WEBHOOK_SECRET (whsec_…) checks the signature of what Stripe posts to /api/stripe/webhook. Without the
# secret key the buy route answers 503 not_configured; without the webhook secret the webhook does. Stripe no
# longer accepts payment_method_types on Checkout (payment methods come from the Dashboard), so a delayed method
# can be offered: a session credits when it is paid, either at checkout.session.completed (payment_status "paid")
# or later at checkout.session.async_payment_succeeded. Both credit once per payment intent.



def stripe_keys() -> tuple[str, str]:
    """(secret key, webhook signing secret) from the environment; "" when not set."""
    return os.environ.get("STRIPE_SECRET_KEY", "").strip(), os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()


def _stripe_client(key: str):
    import stripe

    return stripe.StripeClient(key)


class Buy(BaseModel):
    usd: int = Field(ge=1, le=100_000)   # a bundle's price in dollars; anything else is refused in checkout()
    save_card: bool = False              # also save the card for 1-click and automatic top-ups (newer apps)


def payment_method_config() -> str:
    return os.environ.get("STRIPE_PAYMENT_METHOD_CONFIG", "").strip()   # Hear Church's own set: card, wallets, Link


def bundles_text() -> str:
    return ", ".join(f"${b[0] // 100} ({b[1]} hours)" for b in BUNDLES)


@router.post("/api/hc/checkout")
async def checkout(request: Request, body: Buy):
    """(device token) {usd} → {url}: a Stripe Checkout page where the church pays for the bundle at that price. 400
    not_a_bundle for any other amount (older apps offered $10/$25/$50/$100; they now get the bundle prices to show,
    and an amount that is no bundle is refused rather than charged as a different one). 503 not_configured without
    a Stripe key on this server, 409 for a congregation that uses its own key."""
    cong = _device_congregation(request)
    _limit(request, "checkout", f"cong{cong['id']}")
    key = stripe_keys()[0]
    if not key:
        return JSONResponse({"error": "not_configured", "message": "Buying credit isn't set up on this server yet."},
                            status_code=503)
    if cong["billing"] != "credits":
        raise HTTPException(409, INCLUDED_MESSAGE if cong["billing"] == "internal"
                            else "This congregation pays with its own API key.")
    b = bundle(body.usd * 100)
    if b is None:
        return JSONResponse({"error": "not_a_bundle", "bundles": [bundle_json(x) for x in BUNDLES],
                             "message": f"Credit comes in bundles: {bundles_text()}. Pick one of those, or update "
                                        "Hear Church to see them."}, status_code=400)
    from . import payments

    hours, base = b[1], _base_url(request)
    cents, disc = payments.discounted(b[0], cong["id"])
    meta = payments.purchase_meta(cong, b, cents, disc, "checkout")
    meta.pop("kind")   # (Checkout's metadata stays as it was before 1-click top-ups)
    note = f", {disc['percent']}% off with your code" if disc else ""
    pi_meta = {**meta, "kind": "checkout"}
    if body.save_card:
        pi_meta["save_card"] = "1"
    params = {
        "mode": "payment",
        "line_items": [{"quantity": 1, "price_data": {
            "currency": "usd", "unit_amount": cents,
            "product_data": {"name": "Hear Church credit",
                             "description": f"{hours} language-hours of live translation for {display_name(cong)} "
                                            f"(${b[0] / 100 / hours:.2f} per hour{note})"}}}],
        "client_reference_id": str(cong["id"]),
        "metadata": meta,
        "payment_intent_data": {"metadata": pi_meta, "description": f"Hear Church credit: {display_name(cong)}"},
        "success_url": f"{base}/api/hc/checkout/done",
        "cancel_url": f"{base}/api/hc/checkout/cancelled",
    }
    main_email = _hear().db.main_email(cong["id"])
    customer = _hear().db.payment_profile(cong["id"])["customer_id"]
    try:
        client = _stripe_client(key)
        if body.save_card and not customer:
            customer = await payments.customer_for(cong, client)
    except Exception as e:
        log.warning("stripe customer for %s failed: %s", cong["slug"], e)
        raise HTTPException(502, "Stripe didn't answer: try again in a minute.")
    if customer:   # Stripe's receipts go to the customer's e-mail: the main (billing) one
        params["customer"] = customer
    elif main_email:   # the main (billing) e-mail: Checkout fills it in and Stripe's receipts go there
        params["customer_email"] = main_email
    if body.save_card:   # the card is kept for 1-click and automatic top-ups (the webhook stores it)
        params["payment_intent_data"]["setup_future_usage"] = "off_session"
    pmc = payment_method_config()
    if pmc:
        params["payment_method_configuration"] = pmc
    try:
        session = await asyncio.to_thread(client.v1.checkout.sessions.create, params)
    except Exception as e:
        log.warning("stripe checkout for %s failed: %s", cong["slug"], e)
        raise HTTPException(502, "Stripe didn't answer: try again in a minute.")
    log.info("checkout %s: $%d bundle for %s", session.id, body.usd, cong["slug"])
    out = {"url": session.url, "id": session.id, "usd": body.usd, "hours": hours}
    if disc:
        out.update(percent_off=disc["percent"], charged_usd=cents / 100)
    return out


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
    sends an event again until it gets a 2xx, and may send it twice). charge.refunded: the refunded total comes back
    off that top-up (`_refunded`), also once. 400 for a bad or stale signature; other
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
    from . import payments

    kind = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "charge.refunded":
        return _refunded(obj)
    if kind == "payment_intent.succeeded":
        return await _pi_succeeded(obj)
    if kind == "payment_intent.payment_failed":
        return await payments.payment_failed(obj)
    if kind == "setup_intent.succeeded":
        return await _soft(payments.setup_intent_succeeded(obj), "setup intent", obj.get("id"))
    if kind == "checkout.session.completed" and obj.get("mode") == "setup":
        return await _soft(payments.checkout_setup_completed(obj), "card setup", obj.get("id"))
    if kind not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return {"ok": True, "credited": False}
    s = obj
    meta = s.get("metadata") or {}
    if s.get("payment_status") != "paid" or s.get("mode") != "payment" or s.get("currency") != "usd":
        log.warning("stripe: checkout %s not credited (%s, %s, %s)", s.get("id"), s.get("payment_status"),
                    s.get("mode"), s.get("currency"))
        return {"ok": True, "credited": False}
    try:
        cid = int(meta.get("congregation_id") or s.get("client_reference_id"))
        cents = int(s["amount_total"])
        pid = str(s.get("payment_intent") or s.get("id") or "")
        credit, created = credit_top_up(cid, pid, cents, _paid_minutes(meta, cents))
    except (KeyError, ValueError, TypeError) as e:
        log.error("stripe: checkout %s paid but not credited: %r", s.get("id"), e)
        return {"ok": True, "credited": False}
    # the discount is used up; the card (when the church asked to save it) comes with payment_intent.succeeded
    await payments.paid(pid, {**meta, "save_card": None}, cents, None, created)
    return {"ok": True, "credited": created, "credit_id": credit["id"]}


async def _soft(job, what: str, oid: Any) -> dict[str, Any]:
    """A webhook step that calls Stripe back (to read the saved card): a Stripe hiccup answers 500 so Stripe retries."""
    try:
        return await job
    except Exception as e:
        log.warning("stripe: %s %s not handled yet: %s", what, oid, e)
        return JSONResponse({"error": "retry"}, status_code=500)


async def _pi_succeeded(pi: dict[str, Any]) -> dict[str, Any]:
    """payment_intent.succeeded: a bundle paid by 1-click, automatic top-up or Checkout (then already credited by
    checkout.session.completed, or credited here first; once either way). Payments without our metadata (anything
    else on the Stripe account) are acknowledged and ignored."""
    from . import payments

    meta = pi.get("metadata") or {}
    if not meta.get("congregation_id") or not meta.get("minutes"):
        return {"ok": True, "credited": False}
    try:
        cid = int(meta["congregation_id"])
        cents = int(pi.get("amount_received") or pi["amount"])
        if pi.get("currency") != "usd":
            raise ValueError(pi.get("currency"))
        credit, created = credit_top_up(cid, str(pi["id"]), cents, _paid_minutes(meta, cents))
    except (KeyError, ValueError, TypeError) as e:
        log.error("stripe: payment %s succeeded but not credited: %r", pi.get("id"), e)
        return {"ok": True, "credited": False}
    pm = pi.get("payment_method")
    await payments.paid(str(pi["id"]), meta, cents, pm if isinstance(pm, str) else (pm or {}).get("id"), created)
    return {"ok": True, "credited": created, "credit_id": credit["id"]}


def _refunded(charge: dict[str, Any]) -> dict[str, Any]:
    """charge.refunded: Harrison refunded (part of) a top-up in the Stripe dashboard. The charge's amount_refunded is
    the total refunded so far, so taking that much off the top-up paid with its payment intent is idempotent (a
    resent or late event changes nothing). A charge that isn't a top-up here is logged and acknowledged."""
    pid = str(charge.get("payment_intent") or "")
    try:
        cents = int(charge["amount_refunded"])
        if charge.get("currency") != "usd":
            raise ValueError(charge.get("currency"))
        credit, changed = _hear().db.refund_credit(pid, cents)
    except (KeyError, ValueError, TypeError) as e:
        log.error("stripe: refund of charge %s (%s) not taken off the ledger: %r", charge.get("id"), pid, e)
        return {"ok": True, "refunded": False}
    if changed:
        log.info("refund: %d of %d cents of %s taken back from congregation %d", credit["refunded_cents"],
                 credit["cents"], pid, credit["congregation_id"])
    return {"ok": True, "refunded": changed, "credit_id": credit["id"], "refunded_cents": credit["refunded_cents"]}


def _paid_minutes(meta: dict[str, Any], cents: int) -> float | None:
    """The minutes a paid Checkout session buys: those its bundle said when it was made (metadata, if it matches the
    amount paid), else None (add_credit then uses the bundle at that price, or $5 per hour for a session made before
    the bundles)."""
    try:
        if int(meta.get("cents") or 0) == cents and int(meta.get("minutes") or 0) > 0:
            return float(int(meta["minutes"]))
    except (TypeError, ValueError):
        pass
    return None


def credit_top_up(cid: int, payment_id: str, cents: int,
                  minutes: float | None = None) -> tuple[dict[str, Any], bool]:
    """Credit a paid top-up to a congregation, once per payment: called by the Stripe webhook
    (checkout.session.completed → its payment_intent id, amount_total and bundle) and the operator's admin route.
    `minutes` defaults to the bundle at that price, else $5 per hour. Returns (credit, created). KeyError for an
    unknown congregation, ValueError for a bad amount or a payment id already used differently."""
    h = _hear()
    if h.db.congregation(cid) is None:
        raise KeyError(cid)
    credit, created = h.db.add_credit(cid, payment_id, cents, None if minutes is None else minutes * 60)
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
    c = _hear().db.by_slug_or_alias(slug)
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

    ok = SLUG_RE.fullmatch(slug) and slug not in RESERVED_SLUGS
    c = _hear().db.by_slug(slug) if ok else None
    if c is None and ok and (moved := _hear().db.slug_alias(slug)) is not None:
        return RedirectResponse(f"/{moved['slug']}", status_code=301)   # an old link or printed QR code
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
    c = h.db.by_slug_or_alias(slug)
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
    init.update(session=s.code, live=True, lang=lang, languages=s.languages_public(), **s.listen_state(),
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
    aliases: dict[int, list] = {}
    for a in h.db.aliases():
        aliases.setdefault(a["congregation_id"], []).append(a)
    for c in h.db.list_congregations():
        s = h.live.get_any(c["id"])
        out.append({**{k: c[k] for k in ("id", "slug", "church", "name", "unit_type", "stake", "address", "email",
                                         "email_verified", "hidden", "created_at", "billing")},
                    "devices": len(h.db.devices(c["id"])), "live": s is not None,
                    "wifi_only": bool(s and s.private),
                    "listeners": s.listeners() if s else 0,
                    "last_session": (h.db.sessions(c["id"]) or [{}])[-1].get("started_at"),
                    "balance": h.db.balance(c["id"]),
                    "aliases": [a["slug"] for a in aliases.get(c["id"], [])]})
    return {"congregations": out}


class BillingMode(BaseModel):
    billing: Any = None   # what Siteboard sends (checked by hand: 400, not 422)
    mode: Any = None      # the older name, still accepted


@router.post("/api/admin/congregations/{cid}/billing")
async def admin_billing(request: Request, cid: int, body: BillingMode):
    """Set how the congregation pays: "credits", "own_key" (unlock its own API key) or "internal" (no charge,
    through the hub). Body {"billing": …} or {"mode": …}. 400 for another type, 404 for an unknown congregation.
    Meetings already started keep their type; the Mac shows the AI tab, or the Billing tab, the next time it asks
    GET /api/hc/billing."""
    from .. import main
    from .db import BILLING_TYPES

    main.require_operator(request)
    mode = body.billing if body.billing is not None else body.mode
    if mode not in BILLING_TYPES:
        raise HTTPException(400, f"billing: one of {', '.join(BILLING_TYPES)}.")
    h = _hear()
    if not h.db.set_billing(cid, mode):
        raise HTTPException(404, "No such congregation.")
    log.info("congregation %d billing set to %s by the operator", cid, mode)
    return {"ok": True, "billing": billing_info(h.db.congregation(cid))}


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
    minutes: float | None = Field(default=None, gt=0, le=1_000_000)


@router.post("/api/admin/congregations/{cid}/credits")
async def admin_credit(request: Request, cid: int, body: TopUp):
    """Credit a top-up by hand (or from the Stripe step), idempotent on the payment id."""
    from .. import main

    main.require_operator(request)
    try:
        credit, created = credit_top_up(cid, body.payment_id, body.cents, body.minutes)
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
    s = h.live.get_any(cid)
    if hidden and s is not None:
        h.live.end(s, why="This congregation was hidden by the administrator.")  # it stops being live
    return {"ok": True, "hidden": hidden}
