"""Hear Church operator JSON for the Siteboard desk: stats, the ledger, refunds, own-key codes, one congregation.

Same sign-in as the other /api/admin/* routes (`main.require_operator`). Documented with example answers in
docs/admin-api.md:
  GET  /api/admin/stats
  GET  /api/admin/transactions?congregation=&kind=&limit=&before=
  POST /api/admin/refunds {payment_id, cents?}
  POST /api/admin/own-key-codes/{id}/void
  GET  /api/admin/congregations/{id}
  GET  /api/admin/congregations/{id}/account   its e-mails (main first) and devices (revoked ones too)
  POST /api/admin/devices/{id}/revoke          the device's token stops working; its meeting ends

The ledger has no table of its own: top-ups and manual credits are `credits` rows, refunds are `refunds` rows (one per
change of a top-up's refunded total), and usage is the metered speech of each meeting. The free first language-hour
is spent by a congregation's credit-billed meetings in order, so a meeting's cost is its speech beyond that hour.
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
from .db import CENTS_PER_LANGUAGE_HOUR, FREE_SECONDS

log = logging.getLogger("hear")
router = APIRouter()

DAY = 86400
KINDS = ("top_up", "manual_credit", "refund", "usage")
STRIPE_PREFIXES = ("pi_", "ch_", "cs_", "py_")   # a credit paid through Stripe; anything else was added by hand


def _operator(request: Request) -> api.Hear:
    from .. import main

    main.require_operator(request)
    return api._hear()


def _who(c: dict[str, Any] | None, cid: int) -> dict[str, Any]:
    if c is None:
        return {"id": cid, "slug": None, "name": None}
    return {"id": c["id"], "slug": c["slug"], "name": api.display_name(c)}


def _usd(cents: float) -> float:
    return round(cents / 100, 2)


# ------------------------------------------------------------------ the ledger

def usage(h: api.Hear) -> list[dict[str, Any]]:
    """Every meeting with metered speech, oldest first: language-minutes per language, how much of it the free hour
    covered, and its cost. Own-key meetings are metered but cost nothing."""
    speech = h.db.speech_by_session()
    used: dict[int, float] = {}
    out = []
    for s in h.db.all_sessions():
        langs = speech.get(s["id"])
        if not langs:
            continue
        secs = sum(langs.values())
        cid = s["congregation_id"]
        free = paid = 0.0
        if s["billing"] == "credits":
            before = used.get(cid, 0.0)
            used[cid] = before + secs
            free = max(0.0, min(secs, FREE_SECONDS - before))
            paid = secs - free
        cost = paid / 3600 * CENTS_PER_LANGUAGE_HOUR
        out.append({"id": f"usage:{s['id']}", "kind": "usage", "at": s["started_at"], "congregation_id": cid,
                    "amount_cents": -round(cost), "session_id": s["id"], "code": s["code"], "billing": s["billing"],
                    "started_at": s["started_at"], "ended_at": s["ended_at"],
                    "language_minutes": {k: round(v / 60, 2) for k, v in sorted(langs.items())},
                    "minutes": round(secs / 60, 2), "free_minutes": round(free / 60, 2),
                    "paid_minutes": round(paid / 60, 2), "cost_usd": _usd(cost), "_secs": (secs, free, paid)})
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
        rows.append({"id": f"credit:{c['id']}", "kind": "top_up" if stripe else "manual_credit", "at": c["created_at"],
                     "congregation_id": c["congregation_id"], "amount_cents": c["cents"], "payment_id": c["payment_id"],
                     "cents": c["cents"], "refunded_cents": c["refunded_cents"],
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
    tot = free = paid = own = 0.0
    for r in rows:
        secs, f, p = r["_secs"]
        tot += secs
        if r["billing"] == "credits":
            free, paid = free + f, paid + p
        else:
            own += secs
    return {"total": round(tot / 3600, 2), "paid": round(paid / 3600, 2), "free": round(free / 3600, 2),
            "own_key": round(own / 3600, 2), "paid_usd": _usd(paid / 3600 * CENTS_PER_LANGUAGE_HOUR)}


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
                   "usd_per_language_hour": CENTS_PER_LANGUAGE_HOUR / 100},
        "installs": installs(h, now),
    }


# ------------------------------------------------------------------ refunds

class Refund(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    payment_id: str = Field(min_length=1, max_length=255)
    cents: int | None = Field(default=None, gt=0, le=10_000_000)


def unused_cents(h: api.Hear, credit: dict[str, Any]) -> int:
    """How much of this top-up the congregation hasn't used: what's left of it after refunds, but no more than the
    congregation's balance (credit is one pool, so its unused part is what's still in the pool)."""
    left = credit["cents"] - credit["refunded_cents"]
    bal = round(h.db.balance(credit["congregation_id"])["balance_usd"] * 100)
    return max(0, min(left, bal))


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
                       "minutes": u["minutes"] if u else 0, "free_minutes": u["free_minutes"] if u else 0,
                       "paid_minutes": u["paid_minutes"] if u else 0, "cost_usd": u["cost_usd"] if u else 0})
    return {
        "congregation": {**{k: c[k] for k in ("id", "slug", "church", "name", "unit_type", "stake", "address", "city",
                                              "email", "email_verified", "hidden", "created_at", "billing",
                                              "timezone", "meeting_times")},
                         "title": api.display_name(c)},
        "billing": api.billing_info(c),
        "live": s is not None, "wifi_only": bool(s and s.private), "listeners": s.listeners() if s else 0,
        "devices": _devices(h, cid),
        "emails": h.db.emails(cid),
        "own_key_codes": h.db.own_key_codes(cid),
        "meetings": recent, "meetings_total": len(h.db.sessions(cid)),
        **_page(rows, "", transactions, ""),
    }


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
