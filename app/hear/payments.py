"""Hear Church: a saved card, 1-click and automatic top-ups, and codes (own key, free hours, % off).

The card never touches the hub: it is entered on a Stripe-hosted page (Checkout in setup mode, or Checkout for a
bundle with "save this card"). The hub keeps the congregation's Stripe Customer id, the saved payment method's id and
its brand/last 4 for display (db.payment_profiles). The Customer carries the main (billing) e-mail, so Stripe's
receipts go there. Credit is only ever added by the webhook (payment_intent.succeeded, checkout.session.completed),
idempotent on the payment intent id.

Device routes (Authorization: Bearer <device token>):
  POST /api/hc/card/setup      → {url, id}: a Stripe Checkout page (setup mode) that saves a card; open it in the
                                 browser. It also works right after sign-up (onboarding).
  POST /api/hc/card/remove     → {ok, card: null, auto_topup}: the card is detached at Stripe and forgotten here;
                                 auto top-up goes off with it.
  POST /api/hc/topup {usd, request_id?} → 1-click: charges the saved card for the bundle at that price (less a
                                 pending % off). {status: "succeeded"|"processing"} (the credit shows once Stripe's
                                 webhook arrives, within seconds), {status: "requires_action", url} (3-D Secure: open
                                 url, Stripe's page), 402 card_declined {message}, 409 no_card (buy through
                                 /api/hc/checkout instead), 400 not_a_bundle, 503 not_configured.
  POST /api/hc/auto-topup {enabled, usd?} → {ok, auto_topup}: optional, off by default. Needs a saved card.
  POST /api/hc/codes/check {code} (no device needed; onboarding) → {ok, type, value, message}, 403 bad_code
  POST /api/hc/codes/redeem {code} → {ok, type, value, message, already, billing, …billing answer}; 403 bad_code
                                 ("That code isn't valid." for unknown, expired, used, voided or another
                                 congregation's), 409 pending_discount, 429 locked.
Sign-up (POST /api/hc/signup) takes {code} too: checked before the congregation is made (403 bad_code), redeemed
right after.

Auto top-up: after a meeting ends (never during one), when the congregation is on credits, auto top-up is on, a card
is saved and its paid hours left are under AUTO_TOPUP_BELOW_S (1 h), the hub charges the card off-session for the
chosen bundle, at most once per AUTO_TOPUP_EVERY_S (24 h, counted from the attempt). Each auto top-up e-mails the main
address (Resend, no-reply@hearchurch.com) when it is credited; a failure turns auto top-up off and e-mails that.

Codes: see db.Database.create_code / redeem_code. Admin (operator) routes are in app/hear/admin.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import api
from .db import AUTO_TOPUP_BELOW_S, BUNDLES, CodeError, bundle

log = logging.getLogger("hear")
router = APIRouter()

BAD_CODE = "That code isn't valid."


def _g(obj: Any, name: str, default: Any = None) -> Any:
    """A field of a Stripe object or a plain dict (webhook payloads, test fakes)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _stripe():
    key = api.stripe_keys()[0]
    return api._stripe_client(key) if key else None


def _not_configured() -> JSONResponse:
    return JSONResponse({"error": "not_configured", "message": "Buying credit isn't set up on this server yet."},
                        status_code=503)


def _credits_only(cong: dict[str, Any]) -> None:
    if cong["billing"] != "credits":
        raise HTTPException(409, api.INCLUDED_MESSAGE if cong["billing"] == "internal"
                            else "This congregation pays with its own API key.")


# ------------------------------------------------------------------ what the app shows

def card_json(cid: int) -> dict[str, Any] | None:
    p = api._hear().db.payment_profile(cid)
    if not p["payment_method_id"]:
        return None
    return {"brand": p["card_brand"], "last4": p["card_last4"], "exp_month": p["card_exp_month"],
            "exp_year": p["card_exp_year"], "saved_at": p["card_saved_at"]}


def auto_json(cid: int) -> dict[str, Any]:
    p = api._hear().db.payment_profile(cid)
    cents = p["auto_topup_cents"] or BUNDLES[0][0]
    return {"enabled": p["auto_topup"] and bool(p["payment_method_id"]), "usd": cents // 100,
            "below_hours": AUTO_TOPUP_BELOW_S / 3600, "last_at": p["auto_last_at"]}


def discount_json(cid: int) -> dict[str, Any] | None:
    d = api._hear().db.pending_discount(cid)
    return {"percent": d["percent"], "label": d["label"]} if d else None


def billing_extras(cong: dict[str, Any]) -> dict[str, Any]:
    """Added to GET /api/hc/billing: the saved card, auto top-up and a pending % off."""
    return {"card": card_json(cong["id"]), "auto_topup": auto_json(cong["id"]),
            "discount": discount_json(cong["id"]), "can_save_card": cong["billing"] == "credits"
            and bool(api.stripe_keys()[0])}


def discounted(cents: int, cid: int) -> tuple[int, dict[str, Any] | None]:
    """The price of a bundle for this congregation now: less its pending % off, if any."""
    d = api._hear().db.pending_discount(cid)
    if not d:
        return cents, None
    return max(50, round(cents * (100 - d["percent"]) / 100)), d


def purchase_meta(cong: dict[str, Any], b: tuple[int, int], charged: int, disc: dict[str, Any] | None,
                  kind: str) -> dict[str, str]:
    """The metadata on a bundle's payment (Checkout's payment intent, 1-click, auto): the webhook credits from it."""
    meta = {"congregation_id": str(cong["id"]), "slug": cong["slug"], "cents": str(charged),
            "minutes": str(b[1] * 60), "bundle": str(b[0] // 100), "kind": kind}
    if disc:
        meta["discount_redemption"] = str(disc["redemption_id"])
        meta["percent_off"] = str(disc["percent"])
    return meta


# ------------------------------------------------------------------ the Stripe customer

async def customer_for(cong: dict[str, Any], client) -> str:
    """The congregation's Stripe Customer (made the first time), with its main e-mail for receipts."""
    h = api._hear()
    p = h.db.payment_profile(cong["id"])
    if p["customer_id"]:
        return p["customer_id"]
    params: dict[str, Any] = {"name": api.display_name(cong), "description": f"Hear Church: {cong['slug']}",
                              "metadata": {"congregation_id": str(cong["id"]), "slug": cong["slug"]}}
    email = h.db.main_email(cong["id"])
    if email:
        params["email"] = email
    c = await asyncio.to_thread(client.v1.customers.create, params,
                                {"idempotency_key": f"hc-customer-{cong['id']}-{cong['created_at']}"})
    h.db.set_customer(cong["id"], c.id)
    log.info("stripe customer %s for %s", c.id, cong["slug"])
    return c.id


async def sync_customer_email(cid: int) -> None:
    """The main e-mail changed: Stripe's receipts follow it (best effort; top-ups also pass receipt_email)."""
    h = api._hear()
    p = h.db.payment_profile(cid)
    client = _stripe()
    email = h.db.main_email(cid)
    if not (p["customer_id"] and client and email):
        return
    try:
        await asyncio.to_thread(client.v1.customers.update, p["customer_id"], {"email": email})
    except Exception as e:
        log.warning("stripe: customer e-mail of congregation %d not updated: %s", cid, e)


async def store_card(cid: int, pm_id: str, client=None) -> dict[str, Any] | None:
    """Remember a saved payment method (brand, last 4) as the congregation's card; also its default at Stripe."""
    h = api._hear()
    client = client or _stripe()
    if not pm_id or client is None:
        return None
    pm = await asyncio.to_thread(client.v1.payment_methods.retrieve, pm_id)
    card = _g(pm, "card")
    brand = _g(card, "brand") or _g(pm, "type")
    h.db.set_card(cid, pm_id, brand, _g(card, "last4"), _g(card, "exp_month"), _g(card, "exp_year"))
    cust = _g(pm, "customer") or h.db.payment_profile(cid)["customer_id"]
    if cust:
        try:
            await asyncio.to_thread(client.v1.customers.update, cust,
                                    {"invoice_settings": {"default_payment_method": pm_id}})
        except Exception as e:
            log.warning("stripe: default card of congregation %d not set: %s", cid, e)
    log.info("card saved for congregation %d (%s)", cid, brand)
    return card_json(cid)


# ------------------------------------------------------------------ save / remove a card

@router.post("/api/hc/card/setup")
async def card_setup(request: Request):
    """(device token) A Stripe-hosted page that saves a card for 1-click and automatic top-ups."""
    cong = api._device_congregation(request)
    api._limit(request, "checkout", f"cong{cong['id']}")
    client = _stripe()
    if client is None:
        return _not_configured()
    _credits_only(cong)
    base = api._base_url(request)
    meta = {"congregation_id": str(cong["id"]), "slug": cong["slug"], "purpose": "save_card"}
    try:
        customer = await customer_for(cong, client)
        params: dict[str, Any] = {
            "mode": "setup", "currency": "usd", "customer": customer, "client_reference_id": str(cong["id"]),
            "metadata": meta, "setup_intent_data": {"metadata": meta, "description": "Hear Church: saved card"},
            "success_url": f"{base}/api/hc/card/done", "cancel_url": f"{base}/api/hc/card/cancelled"}
        if api._web(request):   # hearchurch.com: back to the page that asked (sign-up's last step, or /account)
            back = "/start/done" if request.query_params.get("back") == "start" else "/account?card=saved#billing"
            params["success_url"] = f"{base}{back}"
            params["cancel_url"] = f"{base}{'/start/done' if back == '/start/done' else '/account#billing'}"
        pmc = api.payment_method_config()
        if pmc:
            params["payment_method_configuration"] = pmc
        s = await asyncio.to_thread(client.v1.checkout.sessions.create, params)
    except Exception as e:
        log.warning("stripe card setup for %s failed: %s", cong["slug"], e)
        raise HTTPException(502, "Stripe didn't answer: try again in a minute.")
    return {"url": s.url, "id": s.id}


@router.get("/api/hc/card/done", response_class=HTMLResponse)
async def card_done():
    return api._page("Card saved", "Your card is saved with Stripe. It shows in Hear Church within a minute; you can "
                                   "close this page.")


@router.get("/api/hc/card/cancelled", response_class=HTMLResponse)
async def card_cancelled():
    return api._page("No card saved", "Nothing was saved. You can close this page.")


@router.post("/api/hc/card/remove")
async def card_remove(request: Request):
    """(device token) Forget the saved card (and detach it at Stripe); auto top-up goes off."""
    cong = api._device_congregation(request)
    api._limit(request, "checkout", f"cong{cong['id']}")
    h = api._hear()
    p = h.db.payment_profile(cong["id"])
    client = _stripe()
    if p["payment_method_id"] and client is not None:
        try:
            await asyncio.to_thread(client.v1.payment_methods.detach, p["payment_method_id"])
        except Exception as e:   # already detached, or Stripe away: forgetting it here is what counts
            log.warning("stripe: detaching the card of %s: %s", cong["slug"], e)
    h.db.clear_card(cong["id"])
    log.info("card removed for %s", cong["slug"])
    return {"ok": True, "card": None, "auto_topup": auto_json(cong["id"])}


# ------------------------------------------------------------------ 1-click top-up

class TopUp(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    usd: int = Field(ge=1, le=100_000)
    request_id: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")   # one per click: no double charge


def _pi_answer(pi: Any, extra: dict[str, Any]) -> dict[str, Any]:
    status = _g(pi, "status")
    out = {"ok": status in ("succeeded", "processing"), "status": status, "id": _g(pi, "id"), **extra}
    if status == "requires_action":
        na = _g(pi, "next_action")
        url = _g(_g(na, "redirect_to_url"), "url")
        out["url"] = url
        out["message"] = "Your bank wants to confirm this payment: finish it on the page that opened."
    elif status in ("succeeded", "processing"):
        out["message"] = "Payment made. The hours show in a few seconds."
    return out


@router.post("/api/hc/topup")
async def topup(request: Request, body: TopUp):
    """(device token) 1-click: charge the saved card for a bundle. Credit comes with the webhook."""
    cong = api._device_congregation(request)
    api._limit(request, "checkout", f"cong{cong['id']}")
    client = _stripe()
    if client is None:
        return _not_configured()
    _credits_only(cong)
    b = bundle(body.usd * 100)
    if b is None:
        return JSONResponse({"error": "not_a_bundle", "bundles": [api.bundle_json(x) for x in BUNDLES],
                             "message": f"Credit comes in bundles: {api.bundles_text()}."}, status_code=400)
    h = api._hear()
    p = h.db.payment_profile(cong["id"])
    if not (p["payment_method_id"] and p["customer_id"]):
        return JSONResponse({"error": "no_card", "message": "Save a card first, or buy through the Stripe page."},
                            status_code=409)
    charged, disc = discounted(b[0], cong["id"])
    meta = purchase_meta(cong, b, charged, disc, "one_click")
    params: dict[str, Any] = {
        "amount": charged, "currency": "usd", "customer": p["customer_id"], "payment_method": p["payment_method_id"],
        "confirm": True, "metadata": meta,
        "return_url": f"{api._base_url(request)}{'/account?paid=1#billing' if api._web(request) else '/api/hc/topup/done'}",
        "description": f"Hear Church credit: {api.display_name(cong)} ({b[1]} language-hours)"}
    email = h.db.main_email(cong["id"])
    if email:
        params["receipt_email"] = email
    opts = {"idempotency_key": f"hc-topup-{cong['id']}-{body.request_id}"} if body.request_id else None
    import stripe
    try:
        pi = await asyncio.to_thread(client.v1.payment_intents.create, params, opts)
    except stripe.CardError as e:
        log.info("1-click top-up for %s declined: %s", cong["slug"], getattr(e, "code", None))
        return JSONResponse({"error": "card_declined", "message": e.user_message or "Your card was declined."},
                            status_code=402)
    except Exception as e:
        log.warning("1-click top-up for %s failed: %s", cong["slug"], e)
        raise HTTPException(502, "Stripe didn't answer: try again in a minute.")
    log.info("1-click top-up %s: $%d bundle for %s (%s)", _g(pi, "id"), body.usd, cong["slug"], _g(pi, "status"))
    out = _pi_answer(pi, {"usd": body.usd, "hours": b[1], "charged_usd": charged / 100,
                          **({"percent_off": disc["percent"]} if disc else {})})
    if out["status"] == "requires_action" and not out.get("url"):
        return JSONResponse({**out, "error": "needs_checkout",
                             "message": "Your bank wants to confirm this payment: buy through the Stripe page instead."},
                            status_code=409)
    return out


@router.get("/api/hc/topup/done", response_class=HTMLResponse)
async def topup_done():
    return api._page("Thank you", "If your bank approved the payment, the hours show in Hear Church within a minute. "
                                  "You can close this page.")


# ------------------------------------------------------------------ auto top-up

class AutoTopUp(BaseModel):
    enabled: bool
    usd: int | None = Field(default=None, ge=1, le=100_000)


@router.post("/api/hc/auto-topup")
async def auto_topup_settings(request: Request, body: AutoTopUp):
    """(device token) Turn automatic top-up on or off and pick its bundle. On needs a saved card (409 no_card)."""
    cong = api._device_congregation(request)
    api._limit(request, "emails", f"cong{cong['id']}")
    _credits_only(cong)
    h = api._hear()
    cents = None
    if body.usd is not None:
        if bundle(body.usd * 100) is None:
            return JSONResponse({"error": "not_a_bundle", "bundles": [api.bundle_json(x) for x in BUNDLES],
                                 "message": f"Credit comes in bundles: {api.bundles_text()}."}, status_code=400)
        cents = body.usd * 100
    p = h.db.payment_profile(cong["id"])
    if body.enabled and not p["payment_method_id"]:
        return JSONResponse({"error": "no_card", "message": "Save a card first."}, status_code=409)
    if body.enabled and cents is None and not p["auto_topup_cents"]:
        cents = BUNDLES[0][0]
    h.db.set_auto_topup(cong["id"], body.enabled, cents)
    log.info("auto top-up %s for %s", "on" if body.enabled else "off", cong["slug"])
    return {"ok": True, "auto_topup": auto_json(cong["id"])}


def after_meeting(cid: int) -> None:
    """Live.end calls this when a meeting ends: maybe an automatic top-up, in the background."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(auto_topup(cid))


async def auto_topup(cid: int, now: float | None = None) -> str:
    """Charge the saved card off-session for the chosen bundle if the paid hours left are under 1 h. Returns what
    happened ("skipped: …", "charged", "failed") for logs and tests. Never more than once per 24 h, never while the
    congregation's free trial runs."""
    h = api._hear()
    cong = h.db.congregation(cid)
    if cong is None or cong["billing"] != "credits":
        return "skipped: not on credits"
    p = h.db.payment_profile(cid)
    if not (p["auto_topup"] and p["payment_method_id"] and p["customer_id"]):
        return "skipped: off"
    b = h.db.balance(cid)
    if (b.get("trial") or {}).get("active"):   # nobody is charged during the free trial; it starts once it ends
        return "skipped: free trial"
    if b["balance_min"] * 60 >= AUTO_TOPUP_BELOW_S:
        return "skipped: enough left"
    client = _stripe()
    if client is None:
        return "skipped: no stripe"
    if not h.db.claim_auto_charge(cid, now):
        return "skipped: one per 24 h"
    b = bundle(p["auto_topup_cents"] or BUNDLES[0][0]) or BUNDLES[0]
    meta = purchase_meta(cong, b, b[0], None, "auto")
    params: dict[str, Any] = {
        "amount": b[0], "currency": "usd", "customer": p["customer_id"], "payment_method": p["payment_method_id"],
        "confirm": True, "off_session": True, "metadata": meta,
        "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
        "description": f"Hear Church automatic top-up: {api.display_name(cong)} ({b[1]} language-hours)"}
    email = h.db.main_email(cid)
    if email:
        params["receipt_email"] = email
    day = int((now or time.time()) // 86400)
    import stripe
    try:
        pi = await asyncio.to_thread(client.v1.payment_intents.create, params,
                                     {"idempotency_key": f"hc-auto-{cid}-{day}"})
    except stripe.StripeError as e:
        pid = _g(getattr(e, "error", None), "payment_intent")
        await auto_failed(cid, _g(pid, "id") if pid is not None and not isinstance(pid, str) else pid,
                          e.user_message or "The card was declined.")
        return "failed"
    except Exception as e:
        log.warning("auto top-up for %s: Stripe didn't answer: %s", cong["slug"], e)
        return "failed: no answer"   # left on: the 24 h limit holds; the payment_failed webhook turns it off if so
    h.db.set_auto_last_pi(cid, _g(pi, "id"))
    status = _g(pi, "status")
    log.info("auto top-up %s for %s: $%d (%s)", _g(pi, "id"), cong["slug"], b[0] // 100, status)
    if status not in ("succeeded", "processing"):
        await auto_failed(cid, _g(pi, "id"), "The bank asked to confirm the payment, which can't happen automatically.")
        return "failed"
    return "charged"


async def auto_failed(cid: int, pi: str | None, why: str) -> None:
    """An automatic top-up didn't go through: auto top-up goes off, and the main address hears about it (once)."""
    h = api._hear()
    p = h.db.payment_profile(cid)
    if not p["auto_topup"]:
        return
    h.db.set_auto_topup(cid, False)
    if pi:
        h.db.set_auto_last_pi(cid, pi)
    cong = h.db.congregation(cid)
    log.info("auto top-up failed for %s; turned off", cong["slug"] if cong else cid)
    name = api.display_name(cong) if cong else "your congregation"
    await _email(cid, "Hear Church: automatic top-up didn't go through",
                 f"We tried to add credit for {name} automatically, but the payment didn't go through: {why}\n\n"
                 "Automatic top-up is now off. Open Hear Church → Settings → Billing to check your card, buy credit "
                 "or turn automatic top-up on again.")


async def _email(cid: int, subject: str, text: str) -> bool:
    h = api._hear()
    to = h.db.main_email(cid)
    if not to:
        return False
    try:
        return await h.resend.send(to, subject, text)
    except Exception as e:
        log.warning("e-mail to congregation %d not sent: %s", cid, e)
        return False


# ------------------------------------------------------------------ the webhook (called from api.stripe_webhook)

async def paid(pi_id: str, meta: dict[str, Any], cents: int, pm_id: str | None, created: bool) -> None:
    """After a bundle payment was credited (created: the first time): use up its discount, save the card when the
    church asked to, and e-mail about an automatic top-up."""
    h = api._hear()
    try:
        cid = int(meta.get("congregation_id"))
    except (TypeError, ValueError):
        return
    if meta.get("discount_redemption"):
        try:
            h.db.consume_discount(int(meta["discount_redemption"]), pi_id)
        except (TypeError, ValueError):
            pass
    if meta.get("save_card") == "1" and pm_id and h.db.payment_profile(cid)["payment_method_id"] != pm_id:
        try:
            await store_card(cid, pm_id)
        except Exception as e:
            log.warning("stripe: card of congregation %d not saved: %s", cid, e)
    if created and meta.get("kind") == "auto":
        cong = h.db.congregation(cid)
        hours = int(meta.get("minutes") or 0) // 60
        await _email(cid, "Hear Church: credit added automatically",
                     f"Paid hours for {api.display_name(cong) if cong else 'your congregation'} were under 1, so we "
                     f"charged your saved card ${cents / 100:.2f} for {hours} language-hours.\n\n"
                     "You can turn automatic top-up off in Hear Church → Settings → Billing.")


async def payment_failed(pi: dict[str, Any]) -> dict[str, Any]:
    meta = pi.get("metadata") or {}
    if meta.get("kind") != "auto":
        return {"ok": True}
    try:
        cid = int(meta["congregation_id"])
    except (KeyError, TypeError, ValueError):
        return {"ok": True}
    p = api._hear().db.payment_profile(cid)
    if p["auto_last_pi"] in (None, pi.get("id")):
        err = pi.get("last_payment_error") or {}
        await auto_failed(cid, pi.get("id"), err.get("message") or "The card was declined.")
    return {"ok": True, "auto_topup": False}


async def setup_done(setup_intent: Any, cid: int, client=None) -> dict[str, Any]:
    """A card was saved on Stripe's setup page: remember it."""
    pm = _g(setup_intent, "payment_method")
    pm_id = pm if isinstance(pm, str) else _g(pm, "id")
    if _g(setup_intent, "status") not in (None, "succeeded") or not pm_id:
        return {"ok": True, "card_saved": False}
    cust = _g(setup_intent, "customer")
    h = api._hear()
    if cust and not h.db.payment_profile(cid)["customer_id"]:
        h.db.set_customer(cid, cust if isinstance(cust, str) else _g(cust, "id"))
    await store_card(cid, pm_id, client)
    return {"ok": True, "card_saved": True}


async def checkout_setup_completed(s: dict[str, Any]) -> dict[str, Any]:
    """checkout.session.completed in setup mode: read its SetupIntent and save the card."""
    meta = s.get("metadata") or {}
    try:
        cid = int(meta.get("congregation_id") or s.get("client_reference_id"))
    except (TypeError, ValueError):
        return {"ok": True, "card_saved": False}
    if api._hear().db.congregation(cid) is None:
        return {"ok": True, "card_saved": False}
    si = s.get("setup_intent")
    client = _stripe()
    if not si or client is None:
        return {"ok": True, "card_saved": False}
    if isinstance(si, str):
        si = await asyncio.to_thread(client.v1.setup_intents.retrieve, si)
    return await setup_done(si, cid, client)


async def setup_intent_succeeded(si: dict[str, Any]) -> dict[str, Any]:
    meta = si.get("metadata") or {}
    try:
        cid = int(meta["congregation_id"])
    except (KeyError, TypeError, ValueError):
        return {"ok": True, "card_saved": False}
    if api._hear().db.congregation(cid) is None:
        return {"ok": True, "card_saved": False}
    return await setup_done(si, cid)


# ------------------------------------------------------------------ codes (the church's side)

class CodeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    code: str = Field(max_length=40)


def describe(code: dict[str, Any]) -> str:
    v = code["value"]
    if code["type"] == "own_key":
        return "Your congregation now uses its own API key: translation is paid on your own Anthropic account."
    if code["type"] == "free_hours":
        return f"{v:g} free language-hour{'' if v == 1 else 's'} added. They never expire."
    return f"{int(v)}% off your next bundle."


def _bad_code() -> JSONResponse:
    return JSONResponse({"error": "bad_code", "message": BAD_CODE}, status_code=403)


@router.post("/api/hc/codes/check")
async def codes_check(request: Request, body: CodeIn):
    """Would this code work for a new congregation? (Onboarding shows what it does before sign-up.) Nothing is used
    up. Codes assigned to a congregation only check out from that congregation's Mac (with its device token)."""
    h = api._hear()
    ip = api._ip(request)
    if wait := h.own_key_ip.locked(ip):
        return api._locked(wait)
    cid = None
    if request.headers.get("authorization", "").startswith("Bearer "):
        cid = api._device_congregation(request)["id"]
    c = h.db.check_code(body.code, cid)
    if c is None:
        h.own_key_ip.fail(ip)
        return _bad_code()
    return {"ok": True, "type": c["type"], "value": c["value"], "message": describe(c)}


def redeem_for(request: Request, cong: dict[str, Any], code: str) -> dict[str, Any] | JSONResponse:
    """Redeem `code` for `cong`, behind the per-IP and per-congregation failure limits. The answer, or an error."""
    h = api._hear()
    ip = api._ip(request)
    key = f"cong{cong['id']}"
    for limiter, k in ((h.own_key_ip, ip), (h.codes_cong, key)):
        if wait := limiter.locked(k):
            return api._locked(wait)
    try:
        r = h.db.redeem_code(code, cong["id"])
    except CodeError as e:
        if str(e) == "pending_discount":
            d = h.db.pending_discount(cong["id"])
            return JSONResponse({"error": "pending_discount",
                                 "message": f"You already have {d['percent'] if d else 'a'}% off waiting for your next "
                                            "bundle. Use that first, then this code."}, status_code=409)
        h.own_key_ip.fail(ip)
        h.codes_cong.fail(key)
        return _bad_code()
    c = r["code"]
    if not r["already"]:
        log.info("code %d (%s) redeemed by %s", c["id"], c["type"], cong["slug"])
    return {"ok": True, "type": c["type"], "value": c["value"], "already": r["already"], "message": describe(c)}


@router.post("/api/hc/codes/redeem")
async def codes_redeem(request: Request, body: CodeIn):
    """(device token) Redeem a code for this congregation; the answer also carries the billing answer after it."""
    cong = api._device_congregation(request)
    out = redeem_for(request, cong, body.code)
    if isinstance(out, JSONResponse):
        return out
    cong = api._hear().db.congregation(cong["id"])
    return {**api.billing_info(cong, request), **out}
