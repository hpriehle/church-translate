"""Hear Church operator JSON for the Siteboard desk: stats, the ledger, refunds, own-key codes, one congregation.

Same sign-in as the other /api/admin/* routes (`main.require_operator`). Documented with example answers in
docs/admin-api.md:
  GET  /api/admin/stats
  GET  /api/admin/transactions?congregation=&kind=&limit=&before=
  POST /api/admin/refunds {payment_id, cents?}
  POST /api/admin/own-key-codes/{id}/void      (deprecated: the codes routes below cover own-key codes too)
  POST /api/admin/codes {type, value, label, congregation_id?, max_uses?, expires_at?}
  GET  /api/admin/codes?type=&status=&congregation_id=&q=
  POST /api/admin/codes/{id}/void
  GET  /api/admin/congregations/{id}          (congregation.aliases: old slugs that redirect to the current one)
  GET  /api/admin/congregations/{id}/account   its e-mails (main first) and devices (revoked ones too)
  POST /api/admin/devices/{id}/revoke          the device's token stops working; its meeting ends
  GET  /api/admin/congregations/{id}/health?since=&session=&limit=   the Macs' meeting health reports (30 days)
  GET  /api/admin/congregations/{id}/logs?since=&kind=&device=&limit=   the Macs' diagnostic logs (30 days)
  GET  /api/admin/logs/{ref}   one batch by the reference a church was shown after "Send logs to support"

The ledger has no table of its own: top-ups and manual credits are `credits` rows, refunds are `refunds` rows (one per
change of a top-up's refunded total), and usage is the metered speech of each meeting. Each calendar month (UTC) a
congregation's credit-billed meetings first use that month's free language-hour, in the order they started; the rest
is taken from its top-ups, oldest first, so a meeting's cost is what that time was bought at (db.Database._walk).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import api
from . import payments
from .db import BUNDLES, CODE_CREDIT_PREFIX, CODE_TYPES, bundle, FREE_SECONDS, LEGACY_CENTS_PER_LANGUAGE_HOUR

log = logging.getLogger("hear")
router = APIRouter()

DAY = 86400
KINDS = ("top_up", "manual_credit", "free_hours", "refund", "usage")
STRIPE_PREFIXES = ("pi_", "ch_", "cs_", "py_")   # a credit paid through Stripe; anything else was added by hand


def _operator(request: Request) -> api.Hear:
    from .. import main

    main.require_operator(request)
    return api._hear()


def _who(c: dict[str, Any] | None, cid: int) -> dict[str, Any]:
    if c is None:
        return {"id": cid, "slug": None, "name": None}
    return {"id": c["id"], "slug": c["slug"], "name": api.display_name(c)}


def trial_status(t: dict[str, Any] | None) -> dict[str, Any] | None:
    """The free trial as Siteboard shows it per congregation: None (it has none: made before the trial, or own key),
    else {status: "not_started" (no metered meeting yet) | "running" | "ended", started_at, ends_at, used_hours,
    days_left, hours_left}."""
    if not t:
        return None
    status = "ended" if not t["active"] else "running" if t["started_at"] is not None else "not_started"
    return {"status": status, "started_at": t["started_at"], "ends_at": t["ends_at"],
            "used_hours": round(t["used_min"] / 60, 2), "days_left": t["days_left"], "hours_left": t["hours_left"]}


def _usd(cents: float) -> float:
    return round(cents / 100, 2)


# ------------------------------------------------------------------ the ledger

def usage(h: api.Hear) -> list[dict[str, Any]]:
    """Every meeting with metered speech, oldest first: language-minutes per language, how much of it its month's
    free hour covered, and its cost (at the rates of the top-ups it used). Own-key and internal meetings are metered
    but cost nothing."""
    speech = h.db.speech_by_session()
    walks: dict[int, dict[str, Any]] = {}
    out = []
    for s in h.db.all_sessions():
        langs = speech.get(s["id"])
        if not langs:
            continue
        cid = s["congregation_id"]
        if cid not in walks:
            walks[cid] = h.db._walk(cid)
        w = walks[cid]["sessions"].get(s["id"]) or {"secs": sum(langs.values()), "trial": 0.0, "free": 0.0,
                                                     "paid": 0.0, "cost_cents": 0.0}
        secs, free, paid, cost = w["secs"], w["free"], w["paid"], w["cost_cents"]
        out.append({"id": f"usage:{s['id']}", "kind": "usage", "at": s["started_at"], "congregation_id": cid,
                    "amount_cents": -round(cost), "session_id": s["id"], "code": s["code"], "billing": s["billing"],
                    "started_at": s["started_at"], "ended_at": s["ended_at"],
                    "language_minutes": {k: round(v / 60, 2) for k, v in sorted(langs.items())},
                    "minutes": round(secs / 60, 2), "trial_minutes": round(w["trial"] / 60, 2),
                    "free_minutes": round(free / 60, 2),
                    "paid_minutes": round(paid / 60, 2), "cost_usd": _usd(cost), "_secs": (secs, free, paid, cost)})
    return out


def ledger(h: api.Hear, cid: int | None = None) -> list[dict[str, Any]]:
    """Every ledger entry (of one congregation, or all), newest first."""
    rows: list[dict[str, Any]] = []
    logged: Counter = Counter()
    for r in h.db.refunds():
        logged[r["credit_id"]] += r["cents"]
        rows.append({"id": f"refund:{r['id']}", "kind": "refund", "at": r["created_at"],
                     "congregation_id": r["congregation_id"], "amount_cents": -r["cents"], "payment_id": r["payment_id"],
                     "cents": r["cents"], "refunded_total_cents": r["total_cents"], "source": r["source"],
                     "stripe_refund_id": r["stripe_refund_id"]})
    for c in h.db.credits():
        stripe = c["payment_id"].startswith(STRIPE_PREFIXES)
        kind = ("top_up" if stripe else "free_hours" if c["payment_id"].startswith(CODE_CREDIT_PREFIX)
                else "manual_credit")
        rows.append({"id": f"credit:{c['id']}", "kind": kind, "at": c["created_at"],
                     "congregation_id": c["congregation_id"], "amount_cents": c["cents"], "payment_id": c["payment_id"],
                     "cents": c["cents"], "refunded_cents": c["refunded_cents"],
                     "minutes": round(c["seconds"] / 60, 2), "bundle": c["cents"] // 100 if bundle(c["cents"]) else None,
                     "refundable": stripe and c["refunded_cents"] < c["cents"]})
        if c["refunded_cents"] > logged[c["id"]]:   # refunded before the refund log existed: one entry for it
            gap = c["refunded_cents"] - logged[c["id"]]
            rows.append({"id": f"refund:c{c['id']}", "kind": "refund", "at": c["refunded_at"] or c["created_at"],
                         "congregation_id": c["congregation_id"], "amount_cents": -gap, "payment_id": c["payment_id"],
                         "cents": gap, "refunded_total_cents": c["refunded_cents"], "source": "unlogged",
                         "stripe_refund_id": None})
    for u in usage(h):
        rows.append({k: v for k, v in u.items() if k != "_secs"})
    if cid is not None:
        rows = [r for r in rows if r["congregation_id"] == cid]
    congs = {c["id"]: c for c in h.db.list_congregations()}
    for r in rows:
        r["congregation"] = _who(congs.get(r["congregation_id"]), r["congregation_id"])
        r["amount_usd"] = _usd(r["amount_cents"])
    rows.sort(key=lambda r: (r["at"], r["id"]), reverse=True)
    return rows


def _page(rows: list[dict[str, Any]], kind: str, limit: int, before: str) -> dict[str, Any]:
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if before:
        try:
            at, rid = before.split("|", 1)
            cursor = (float(at), rid)
        except ValueError:
            raise HTTPException(400, "before: use next_before from the previous page.")
        rows = [r for r in rows if (r["at"], r["id"]) < cursor]
    page = rows[:limit]
    more = len(rows) > limit
    return {"transactions": page, "next_before": f"{page[-1]['at']!r}|{page[-1]['id']}" if more else None}


@router.get("/api/admin/transactions")
async def admin_transactions(request: Request, congregation: int | None = None, kind: str = "",
                             limit: int = Query(default=50, ge=1, le=500), before: str = Query(default="", max_length=80)):
    """Every ledger entry, newest first, a page at a time (next_before → ?before= for the next page)."""
    h = _operator(request)
    if kind and kind not in KINDS:
        raise HTTPException(400, f"kind: one of {', '.join(KINDS)}.")
    if congregation is not None and h.db.congregation(congregation) is None:
        raise HTTPException(404, "No such congregation.")
    return _page(ledger(h, congregation), kind, limit, before)


# ------------------------------------------------------------------ stats

def _hours(rows: list[dict[str, Any]]) -> dict[str, float]:
    tot = free = paid = own = internal = cost = 0.0
    for r in rows:
        secs, f, p, c = r["_secs"]
        tot += secs
        if r["billing"] == "credits":
            free, paid, cost = free + f, paid + p, cost + c
        elif r["billing"] == "internal":
            internal += secs
        else:
            own += secs
    return {"total": round(tot / 3600, 2), "paid": round(paid / 3600, 2), "free": round(free / 3600, 2),
            "own_key": round(own / 3600, 2), "internal": round(internal / 3600, 2), "paid_usd": _usd(cost)}


def installs(h: api.Hear, now: float) -> dict[str, Any]:
    devs = h.db.all_devices()
    by: dict[str, dict[str, int]] = {}
    for d in devs:
        v = by.setdefault(d["app_version"] or "unknown", {"devices": 0, "active_7d": 0, "active_30d": 0})
        v["devices"] += 1
        seen = d["last_seen"] or 0
        v["active_7d"] += seen >= now - 7 * DAY
        v["active_30d"] += seen >= now - 30 * DAY
    return {"devices": len(devs), "active_7d": sum(v["active_7d"] for v in by.values()),
            "active_30d": sum(v["active_30d"] for v in by.values()),
            "never_connected": sum(1 for d in devs if not d["last_seen"]),
            "by_version": dict(sorted(by.items()))}


@router.get("/api/admin/stats")
async def admin_stats(request: Request):
    h = _operator(request)
    now = time.time()
    congs = h.db.list_congregations()
    sessions = h.db.all_sessions()
    used = usage(h)

    def window(days: int) -> dict[str, Any]:
        t = now - days * DAY
        recent = [s for s in sessions if s["started_at"] >= t]
        return {"signups": sum(1 for c in congs if c["created_at"] >= t),
                "active_congregations": len({s["congregation_id"] for s in recent}),
                "meetings": len(recent), "language_hours": _hours([u for u in used if u["at"] >= t])}

    credits = h.db.credits()
    sold = sum(c["cents"] for c in credits if c["payment_id"].startswith(STRIPE_PREFIXES))
    manual = sum(c["cents"] for c in credits if not c["payment_id"].startswith(STRIPE_PREFIXES))
    balances = [h.db.balance(c["id"])["balance_usd"] for c in congs]
    visible = [c for c in congs if not c["hidden"]]
    return {
        "at": now,
        "congregations": {"total": len(congs), "hidden": len(congs) - len(visible),
                          "verified": sum(1 for c in visible if c["email_verified"]),
                          "by_billing": dict(Counter(c["billing"] for c in visible)),
                          "live_now": len(h.live.sessions)},
        "last_7d": window(7), "last_30d": window(30),
        "all_time": {"meetings": len(sessions), "language_hours": _hours(used)},
        "credit": {"sold_usd": _usd(sold), "manual_usd": _usd(manual),
                   "refunded_usd": _usd(sum(c["refunded_cents"] for c in credits)),
                   "outstanding_usd": round(sum(b for b in balances if b > 0), 2),
                   "overdrawn_usd": round(sum(b for b in balances if b < 0), 2),
                   "free_hours_per_month": FREE_SECONDS / 3600,
                   "bundles": [api.bundle_json(b) for b in BUNDLES],
                   "legacy_usd_per_language_hour": LEGACY_CENTS_PER_LANGUAGE_HOUR / 100},
        "installs": installs(h, now),
    }


# ------------------------------------------------------------------ refunds

class Refund(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    payment_id: str = Field(min_length=1, max_length=255)
    cents: int | None = Field(default=None, gt=0, le=10_000_000)


def unused_cents(h: api.Hear, credit: dict[str, Any]) -> int:
    """What the unused part of this top-up is worth, pro rata at its own rate: its minutes not used yet (meetings
    use the oldest top-ups first, after the month's free hour) times what it cost per minute. A $50 bundle (11 h)
    with 5.5 h unused refunds $25; a top-up from before the bundles, at $5 per hour."""
    return h.db.unused_cents(credit)


def _err(status: int, error: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": error, "message": message, **extra}, status_code=status)


@router.post("/api/admin/refunds")
async def admin_refund(request: Request, body: Refund):
    """Refund (part of) a top-up through Stripe, then take it off the ledger. cents defaults to the top-up's unused
    part; more is allowed up to what's left of the top-up, with a warning (used credit is final by policy)."""
    import stripe

    h = _operator(request)
    credit = h.db.credit_by_payment(body.payment_id)
    if credit is None:
        return _err(404, "not_found", "No top-up was credited with that payment id.")
    pid = credit["payment_id"]
    if not pid.startswith("pi_"):
        return _err(409, "not_stripe", "That credit wasn't paid through a Stripe payment intent, so there's "
                                       "nothing to refund in Stripe.")
    left = credit["cents"] - credit["refunded_cents"]
    if left <= 0:
        return _err(409, "fully_refunded", "That top-up is already fully refunded.", credit=credit)
    unused = unused_cents(h, credit)
    cents = body.cents if body.cents is not None else unused
    if cents <= 0:
        return _err(409, "nothing_unused", "None of that top-up is unused. To refund used credit anyway, say how "
                                           "much in cents.", unused_cents=0, refundable_cents=left)
    if cents > left:
        return _err(409, "too_much", f"Only {left} cents of that top-up are left to refund.",
                    unused_cents=unused, refundable_cents=left)
    key = api.stripe_keys()[0]
    if not key:
        return _err(503, "not_configured", "STRIPE_SECRET_KEY isn't set on this server.")
    params = {"payment_intent": pid, "amount": cents, "reason": "requested_by_customer",
              "metadata": {"congregation_id": str(credit["congregation_id"]), "credit_id": str(credit["id"]),
                           "by": "hub-admin"}}
    # The same refund asked twice (a double click, a retried request) is one refund at Stripe: the key changes
    # only once the ledger has taken the first one.
    idem = f"hc-refund-{pid}-{credit['refunded_cents']}-{cents}"
    try:
        refund = await asyncio.to_thread(api._stripe_client(key).v1.refunds.create, params,
                                         {"idempotency_key": idem})
    except stripe.StripeError as e:
        log.warning("stripe refund of %s failed: %s", pid, e)
        status = 502 if isinstance(e, (stripe.APIConnectionError, stripe.APIError, stripe.RateLimitError,
                                       stripe.AuthenticationError)) else 409
        return _err(status, "stripe", e.user_message or str(e) or "Stripe refused the refund.",
                    stripe_code=getattr(e, "code", None), stripe_status=getattr(e, "http_status", None))
    except Exception as e:
        log.warning("stripe refund of %s failed: %r", pid, e)
        return _err(502, "stripe", "Stripe didn't answer: check the Stripe dashboard before trying again.")
    status = getattr(refund, "status", None)
    if status in ("failed", "canceled"):
        return _err(409, "stripe", f"Stripe made the refund but it {status}.", stripe_refund_id=refund.id)
    # As the charge.refunded webhook does: the top-up's refunded total grows to include this refund, so when the
    # webhook arrives with the charge's amount_refunded nothing changes.
    credit, _ = h.db.refund_credit(pid, credit["refunded_cents"] + cents, source="admin", stripe_refund_id=refund.id)
    log.info("refund %s: %d cents of %s for congregation %d", refund.id, cents, pid, credit["congregation_id"])
    out = {"ok": True, "refund": {"id": refund.id, "status": status, "cents": cents, "usd": _usd(cents)},
           "credit": credit, "unused_cents_before": unused, "balance": h.db.balance(credit["congregation_id"])}
    if cents > unused:
        out["warning"] = (f"{_usd(cents - unused):.2f} USD of this refund was credit already used for translation "
                          "(used hours are normally final). The balance is now negative if nothing else covers it.")
    return out


# ------------------------------------------------------------------ own-key codes

@router.post("/api/admin/own-key-codes/{kid}/void")
async def admin_void_code(request: Request, kid: int):
    """An unused code stops working. A used code can't be voided (409): the congregation already switched."""
    h = _operator(request)
    state = h.db.void_own_key_code(kid)
    if state is None:
        raise HTTPException(404, "No such code.")
    if state == "used":
        return _err(409, "used", "That code was already used; voiding it changes nothing.")
    log.info("own-key code %d voided", kid)
    code, = [c for c in h.db.own_key_codes() if c["id"] == kid]
    return {"ok": True, "code": code}


# ------------------------------------------------------------------ codes (own key, free hours, % off)

class NewCode(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    type: str = Field(max_length=20)
    value: float = 0
    label: str = Field(default="", max_length=120)
    congregation_id: int | None = None
    max_uses: int = Field(default=1, ge=1, le=100_000)
    expires_at: float | None = None


@router.post("/api/admin/codes")
async def admin_create_code(request: Request, body: NewCode):
    """A new code. Codes are kept in plain so they can be listed and handed out again; never logged."""
    h = _operator(request)
    if body.type not in CODE_TYPES:
        return _err(400, "bad_type", f"type: one of {', '.join(CODE_TYPES)}.")
    if body.expires_at is not None and body.expires_at <= time.time():
        return _err(400, "bad_expiry", "expires_at must be in the future (Unix seconds).")
    try:
        code = h.db.create_code(body.type, body.value, body.label, body.congregation_id, body.max_uses,
                                body.expires_at)
    except KeyError:
        return _err(404, "not_found", "No such congregation.")
    except ValueError as e:
        return _err(400, "bad_value", str(e))
    log.info("code %d made (%s)", code["id"], code["type"])
    return {"ok": True, "code": {**code, "redemptions": [], "message": payments.describe(code)}}


@router.get("/api/admin/codes")
async def admin_codes(request: Request, type: str = "", status: str = "", congregation_id: int | None = None,
                      q: str = Query(default="", max_length=40)):
    h = _operator(request)
    if type and type not in CODE_TYPES:
        raise HTTPException(400, f"type: one of {', '.join(CODE_TYPES)}.")
    if status and status not in ("active", "used", "expired", "void"):
        raise HTTPException(400, "status: one of active, used, expired, void.")
    return {"codes": h.db.codes(type or None, status or None, congregation_id, q)}


@router.post("/api/admin/codes/{kid}/void")
async def admin_void_any_code(request: Request, kid: int):
    """The code stops working (a multi-use code that was partly used too). 409 when it was already used up."""
    h = _operator(request)
    state = h.db.void_code(kid)
    if state is None:
        raise HTTPException(404, "No such code.")
    if state == "used":
        return _err(409, "used", "That code was already used up; voiding it changes nothing.")
    log.info("code %d voided", kid)
    code, = [c for c in h.db.codes() if c["id"] == kid]
    return {"ok": True, "code": code}


# ------------------------------------------------------------------ one congregation

@router.get("/api/admin/congregations/{cid}")
async def admin_congregation(request: Request, cid: int, meetings: int = Query(default=20, ge=1, le=200),
                             transactions: int = Query(default=50, ge=1, le=500)):
    h = _operator(request)
    c = h.db.congregation(cid)
    if c is None:
        raise HTTPException(404, "No such congregation.")
    s = h.live.get_any(cid)
    rows = ledger(h, cid)
    by_session = {r["session_id"]: r for r in rows if r["kind"] == "usage"}
    recent = []
    for m in reversed(h.db.sessions(cid)[-meetings:]):
        u = by_session.get(m["id"])
        recent.append({"session_id": m["id"], "code": m["code"], "started_at": m["started_at"],
                       "ended_at": m["ended_at"], "billing": m["billing"], "languages": m["languages"],
                       "language_minutes": u["language_minutes"] if u else {},
                       "minutes": u["minutes"] if u else 0, "trial_minutes": u["trial_minutes"] if u else 0,
                       "free_minutes": u["free_minutes"] if u else 0,
                       "paid_minutes": u["paid_minutes"] if u else 0, "cost_usd": u["cost_usd"] if u else 0})
    return {
        "congregation": {**{k: c[k] for k in ("id", "slug", "church", "name", "unit_type", "stake", "address", "city",
                                              "email", "email_verified", "hidden", "created_at", "billing",
                                              "timezone", "meeting_times")},
                         "free_trial": trial_status(h.db.balance(cid).get("trial")),
                         "title": api.display_name(c),
                         "aliases": [x["slug"] for x in h.db.aliases(cid)]},
        "billing": api.billing_info(c),
        "live": s is not None, "wifi_only": bool(s and s.private), "listeners": s.listeners() if s else 0,
        "devices": _devices(h, cid),
        "emails": h.db.emails(cid),
        "own_key_codes": h.db.own_key_codes(cid),
        "codes": [{"redemption_id": r["id"], **{k: r[k] for k in ("code_id", "type", "label", "at", "status", "payment_id",
                                                                   "consumed_at")},
                   "value": int(r["value"]) if r["value"] == int(r["value"]) else r["value"]}
                  for r in h.db.code_redemptions(cid=cid)],
        "card": payments.card_json(cid), "auto_topup": payments.auto_json(cid),
        "stripe_customer_id": h.db.payment_profile(cid)["customer_id"],
        "meetings": recent, "meetings_total": len(h.db.sessions(cid)),
        **_page(rows, "", transactions, ""),
    }


@router.get("/api/admin/congregations/{cid}/health")
async def admin_health(request: Request, cid: int, since: float | None = Query(default=None, ge=0),
                       session: str = Query(default="", max_length=40),
                       limit: int = Query(default=1000, ge=1, le=5000)):
    """The meeting health reports its Macs sent (app/health.py), received after `since` (unix time; default the last
    24 hours), oldest first; `session` keeps one session code's. Kept 30 days."""
    h = _operator(request)
    c = h.db.congregation(cid)
    if c is None:
        raise HTTPException(404, "No such congregation.")
    since = time.time() - DAY if since is None else since
    names = {d["id"]: d["name"] for d in h.db.devices(cid, revoked=True)}
    reports = [{**r, "device": names.get(r["device_id"])} for r in h.db.health(cid, since, limit, session)]
    return {"congregation": _who(c, cid), "since": since, "reports": reports}


@router.get("/api/admin/congregations/{cid}/logs")
async def admin_logs(request: Request, cid: int, since: float | None = Query(default=None, ge=0),
                     kind: str = Query(default="", pattern="^(|stream|manual)$"),
                     device: int | None = Query(default=None), limit: int = Query(default=200, ge=1, le=2000)):
    """The diagnostic log batches its Macs sent (app/sharelog.py), received after `since` (unix time; default the
    last 7 days), oldest first; `kind` "manual" keeps the "Send logs to support" ones (their id is the reference the
    church was shown). Kept 30 days."""
    h = _operator(request)
    c = h.db.congregation(cid)
    if c is None:
        raise HTTPException(404, "No such congregation.")
    since = time.time() - 7 * DAY if since is None else since
    names = {d["id"]: d["name"] for d in h.db.devices(cid, revoked=True)}
    batches = [{**b, "device": names.get(b["device_id"])} for b in h.db.logs(cid, since, limit, kind, device)]
    return {"congregation": _who(c, cid), "since": since, "batches": batches}


@router.get("/api/admin/logs/{ref}")
async def admin_log_batch(request: Request, ref: int):
    """One diagnostic log batch by its reference ("Sent. Reference #1234" on the church's Mac), with its
    congregation."""
    h = _operator(request)
    b = h.db.log_batch(ref)
    if b is None:
        raise HTTPException(404, "No such reference (logs are kept 30 days).")
    cid = b["congregation_id"]
    c = h.db.congregation(cid)
    names = {d["id"]: d["name"] for d in h.db.devices(cid, revoked=True)}
    return {"congregation": _who(c, cid), "batch": {**b, "device": names.get(b["device_id"])}}


# ------------------------------------------------------------------ e-mails and devices

def _devices(h: api.Hear, cid: int) -> list[dict[str, Any]]:
    """Every device of a congregation, revoked ones too (revoked_at set), most recently seen first."""
    return sorted(h.db.devices(cid, revoked=True), key=lambda d: (d["revoked_at"] is None, d["last_seen"] or 0),
                  reverse=True)


@router.get("/api/admin/congregations/{cid}/account")
async def admin_account(request: Request, cid: int):
    h = _operator(request)
    c = h.db.congregation(cid)
    if c is None:
        raise HTTPException(404, "No such congregation.")
    return {"congregation": _who(c, cid), "emails": h.db.emails(cid), "main_email": h.db.main_email(cid),
            "devices": _devices(h, cid)}


@router.post("/api/admin/devices/{did}/revoke")
async def admin_revoke_device(request: Request, did: int):
    """The device's token stops working for good (revoking again is fine); a meeting it is running ends and its link
    closes. The Mac can join again with a new code."""
    h = _operator(request)
    if not h.db.revoke_device(did):
        raise HTTPException(404, "No such device.")
    h.live.revoked(did)
    d = h.db.device(did)
    log.info("device %d of congregation %d revoked", did, d["congregation_id"])
    return {"ok": True, "device": d}
