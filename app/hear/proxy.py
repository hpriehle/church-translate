"""Translation on credits: the Mac has no Anthropic key, so the hub asks Claude for it with Hear Church's key.

  POST /api/hc/translate   (device token) {model, system, messages, effort} → Anthropic's reply, unchanged (JSON).
      The engine's "hub" backend (app/translate/hub_session.py) sends one turn of its translation conversation;
      the hub builds the same request the Mac would with its own key (api_session.request_params) and answers
      with the Messages API's reply.

Who may: a congregation on credits, from a Mac whose meeting is live on this hub (during the reconnect grace too),
or that ended one in the last TRAILING_S (its last sentences). A "this Wi-Fi only" meeting counts too: the Mac
opens a private, unlisted session for it (app/hear/live.py), so it is metered like an online one. A meeting under way is never
cut off, whatever the balance; a Mac with no credit left gets 402 out_of_credit (its meeting was refused anyway).
Own-key congregations get 409 own_key (they translate straight from the Mac).

Privacy: the speech and its translation pass through memory only. Nothing here stores or logs them: logs carry
the congregation, sizes, timing and status codes, never a request or reply body or Anthropic's error text.

Server key: ANTHROPIC_API_KEY in the hub's environment. Without it → 503 not_configured (everything else works).
Limits per congregation: REQUESTS_PER_MIN, at most IN_FLIGHT at once, bodies up to MAX_BODY bytes.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Literal

import anthropic
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from ..translate.api_session import MODEL_IDS, request_params
from . import api as hear_api
from .mail import RateLimiter

log = logging.getLogger("hear")
router = APIRouter()

TRAILING_S = 120.0          # after a meeting ended: its last chunks may still be in the translator
REQUESTS_PER_MIN = 600      # up to 20 languages, one sentence every few seconds each, plus new conversations
IN_FLIGHT = 40              # each language has at most one turn out (two while a fresh conversation takes over)
MAX_BODY = 2_000_000        # a 150-turn conversation with its thinking blocks is a few hundred KB
MAX_MESSAGES = 1000

limiter = RateLimiter(REQUESTS_PER_MIN, 60)
in_flight: dict[int, int] = {}
_clients: dict[str, anthropic.AsyncAnthropic] = {}


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]


class TranslateTurn(BaseModel):
    model: Literal[tuple(MODEL_IDS.values())]   # type: ignore[valid-type]
    system: str = Field(max_length=200_000)
    messages: list[Turn] = Field(min_length=1, max_length=MAX_MESSAGES)
    effort: Literal["", "low", "medium", "high", "xhigh", "max"] = ""


def server_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _anthropic_client(key: str) -> anthropic.AsyncAnthropic:
    if key not in _clients:
        # Like the Mac's: one quick retry; the Mac's translator has its own deadlines.
        _clients.clear()
        _clients[key] = anthropic.AsyncAnthropic(api_key=key, max_retries=1, timeout=60.0)
    return _clients[key]


def _no(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status)


def may_translate(device: dict[str, Any], cong: dict[str, Any]) -> JSONResponse | None:
    """None when this Mac may translate on credits now, else the refusal."""
    h = hear_api._hear()
    if cong["billing"] != "credits":
        return _no(409, "own_key", "This congregation translates with its own Anthropic key.")
    live = h.live.get_any(cong["id"])   # private ("this Wi-Fi only") sessions too
    if live is not None and live.device_id == device["id"]:
        return None                       # a meeting under way is never cut off
    if h.db.ended_lately(cong["id"], device["id"], TRAILING_S):
        return None
    if not h.db.can_start(cong):
        return _no(402, "out_of_credit", hear_api.billing_info(cong)["message"])
    return _no(409, "not_live", "Translation on credit works only during a meeting started on this Mac while it "
                                "can reach Hear Church. Check the internet connection, then start the meeting again.")


@router.post("/api/hc/translate")
async def translate(request: Request):
    device, cong = hear_api.device_and_congregation(request)
    cid = cong["id"]
    if not limiter.allow(f"cong{cid}"):
        return _no(429, "rate_limited", "Too many translation requests: slow down.")
    key = server_key()
    if not key:
        return _no(503, "not_configured", "Translation on credit isn't set up on this server yet.")
    refused = may_translate(device, cong)
    if refused is not None:
        return refused
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        return _no(413, "too_large", "That conversation is too long.")
    body = await request.body()
    if len(body) > MAX_BODY:
        return _no(413, "too_large", "That conversation is too long.")
    try:
        turn = TranslateTurn.model_validate_json(body)
    except ValidationError:
        return _no(400, "bad_request", "Not a translation request.")   # no details: they would quote the text
    if in_flight.get(cid, 0) >= IN_FLIGHT:
        return _no(429, "busy", "Too many translations at once.")
    in_flight[cid] = in_flight.get(cid, 0) + 1
    t0 = time.monotonic()
    try:
        params = request_params(turn.model, turn.system, [m.model_dump() for m in turn.messages], turn.effort)
        resp = await _anthropic_client(key).beta.messages.create(**params)
    except anthropic.APIError as e:
        status = getattr(e, "status_code", None)
        log.warning("translate for %s: Anthropic %s %s after %.1fs (%d bytes)", cong["slug"], type(e).__name__,
                    status or "", time.monotonic() - t0, len(body))
        if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            log.error("the hub's ANTHROPIC_API_KEY was refused")
            return _no(503, "unavailable", "Translation on credit is down on Hear Church's side.")
        if isinstance(e, anthropic.RateLimitError):
            return _no(429, "anthropic_busy", "Translation is busy right now: retrying.")
        if isinstance(e, anthropic.BadRequestError):
            return _no(400, "bad_request", "Anthropic refused this request.")
        return _no(502, "unreachable", "Hear Church can't reach Anthropic right now.")
    finally:
        in_flight[cid] -= 1
        if not in_flight[cid]:
            del in_flight[cid]
    u = resp.usage
    log.debug("translate for %s: %s, %d bytes in, %s+%s tokens, %.1fs", cong["slug"], turn.model, len(body),
              u.input_tokens, u.output_tokens, time.monotonic() - t0)
    return JSONResponse(resp.model_dump(mode="json", exclude_none=True, warnings=False))
