"""Hear Church HTTP/WebSocket routes: sign-up, device tokens, /c/<slug> listener pages, admin list.

Sign-up flow (JSON, for the Mac app; sign-up is open, no manual approval):
  POST /api/hc/signup       church, name, unit_type, email, [stake, address, lat, lng, meeting_times, timezone,
                            device_name] → {slug, claim}; an e-mail with a verification link is sent
  GET  /api/hc/verify?token=…   (the link in the e-mail) marks the e-mail verified; the congregation is live-capable
  POST /api/hc/claim        {claim} → 202 {pending} until the link was clicked, then once {device_token, slug}
  POST /api/hc/device-link  {email, [slug, device_name]} → e-mails a magic link to add another Mac; the new Mac
                            then polls /api/hc/claim with the returned {claim} exactly like above
The claim secret stays with the app that asked, so a link opened by a mail scanner yields no token.
meeting_times: [{"day": "sun", "time": "10:00"}], timezone: an IANA name (used for "next meeting").
"""
from __future__ import annotations

import asyncio
import html
import hmac
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import LOG_DIR, ROOT, WEB_DIR
from .db import Database
from .live import Live
from .mail import Mailer, RateLimiter

log = logging.getLogger("hear")
router = APIRouter()

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
LINK_TTL_S = 24 * 3600


class Hear:
    def __init__(self, db: Database, mailer: Mailer, live: Live):
        self.db, self.mailer, self.live = db, mailer, live
        self.limits = {"signup": RateLimiter(5, 3600), "email": RateLimiter(10, 3600),
                       "email_addr": RateLimiter(3, 3600), "claim": RateLimiter(1200, 3600),
                       "verify": RateLimiter(60, 3600)}


H: Hear | None = None


def setup(settings) -> Hear:
    """Open the database and build the registry (called once at server start, and by tests)."""
    global H
    cfg = settings.hearchurch
    path = ROOT / cfg.db_path
    db = Database(path)
    H = Hear(db, Mailer.from_settings(cfg, LOG_DIR / "outbox.log"), Live(db, settings.stream, cfg.grace_s))
    return H


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
    cong = h.db.create_congregation(
        church=body.church, name=body.name, unit_type=body.unit_type, email=body.email, stake=body.stake,
        address=body.address, lat=body.lat, lng=body.lng, timezone=body.timezone,
        meeting_times=[m.model_dump() for m in body.meeting_times])
    claim = _send_link(request, cong, "verify", body.device_name)
    return {"ok": True, "slug": cong["slug"], "claim": claim,
            "message": "Check your e-mail for the verification link."}


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


@router.get("/c/{slug}", response_class=HTMLResponse)
async def listener_page(slug: str):
    from .. import main

    _congregation(slug)
    return main.page(WEB_DIR / "listener" / "index.html")


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
        "congregation": {"name": c["name"], "church": c["church"], "slug": c["slug"]},
        "next_meeting": next_meeting(c["meeting_times"], c["timezone"]),
        "session": None, "live": False, "lang": "", "on": True, "languages": [], "history": []}
    if s is None:  # not live: wait on the congregation's room; the page reloads when a session starts
        await h.live.waiting_hub(c["id"]).serve(ws, [init])
        return
    lang = hello.get("lang") if hello.get("lang") in s.offered else s.offered[0]
    same = hello.get("boot") == h.live.started and hello.get("session") == s.code and hello.get("lang") == lang
    last = int(hello.get("last_seq") or 0) if same else 0
    init.update(session=s.code, live=True, lang=lang, languages=s.languages_public(),
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
                                         "email_verified", "hidden", "created_at")},
                    "devices": len(h.db.devices(c["id"])), "live": s is not None,
                    "listeners": s.listeners() if s else 0,
                    "last_session": (h.db.sessions(c["id"]) or [{}])[-1].get("started_at")})
    return {"congregations": out}


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
