"""One translation conversation through the Anthropic API directly (the Hear Church Mac app).

Same interface as ClaudeSession (the `claude` CLI), so the router can use either. The key comes from
ANTHROPIC_API_KEY in the environment (the Mac app passes it from the Keychain) or data/secrets.env.
The conversation is append-only and cached, so each turn only pays full price for the new sentence.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import anthropic

from .prompts import has_markup

from .claude_session import Usage, _Pending

log = logging.getLogger("claude")

# The operator page's short names → API model ids.
MODEL_IDS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
# $ per million tokens: input, output, cache read, cache write (5-minute cache = 1.25 x input).
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
MAX_TOKENS = 4000           # a translated chunk is a few sentences; thinking counts against this too
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def model_id(name: str) -> str:
    return MODEL_IDS.get(name, name)


def api_key() -> str:
    from ..config import load_secrets

    return os.environ.get("ANTHROPIC_API_KEY") or load_secrets().get("ANTHROPIC_API_KEY", "")


def request_params(model: str, system: str, messages: list, effort: str) -> dict[str, Any]:
    """One translation turn, as sent to the API: from the Mac with the church's key, or by the hub with ours
    (app/hear/proxy.py), so both behave the same."""
    return dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
        cache_control={"type": "ephemeral"},   # caches the whole conversation so far
        output_config={"effort": effort} if effort else anthropic.omit,
        betas=[FALLBACK_BETA],
        fallbacks="default",                   # a declined sentence is retried on another model
    )


def cost_usd(model: str, u: Usage) -> float:
    p_in, p_out, p_read, p_write = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (u.input * p_in + u.output * p_out + u.cache_read * p_read + u.cache_write * p_write) / 1e6


class ApiSession:
    _counter = 0

    def __init__(self, system_prompt: str, model: str, effort: str, workdir=None, client: Any = None):
        ApiSession._counter += 1
        self.id = ApiSession._counter
        self.system_prompt = system_prompt
        self.model = model_id(model)
        self.effort = effort
        self.client = client
        self.messages: list[dict[str, Any]] = []
        self.pending: _Pending | None = None
        self.turns = 0
        self.usage = Usage()
        self.alive = False
        self.ready = False
        self.last_error = ""
        self.rate_limit: dict | None = None
        self.auth = "ANTHROPIC_API_KEY (direct)"

    async def start(self) -> None:
        if self.client is None:
            key = api_key()
            if not key:
                raise RuntimeError("no Anthropic API key")
            # The router has its own deadlines and replaces stuck sessions, so one quick retry is enough here.
            self.client = anthropic.AsyncAnthropic(api_key=key, max_retries=1, timeout=60.0)
        self.alive = True
        log.info("claude API session #%d started (model %s)", self.id, self.model)

    async def close(self) -> None:
        self.alive = False
        self.ready = False

    @property
    def busy(self) -> bool:
        return self.pending is not None

    @property
    def busy_for(self) -> float:
        return time.monotonic() - self.pending.sent_at if self.pending else 0.0

    async def ask(self, text: str) -> str:
        if not self.alive:
            raise RuntimeError("claude session is not running")
        if self.pending:
            raise RuntimeError("claude session is busy")
        self.pending = _Pending(asyncio.get_running_loop().create_future())
        self.messages.append({"role": "user", "content": text})
        try:
            resp = await self.client.beta.messages.create(
                **request_params(self.model, self.system_prompt, self.messages, self.effort))
        except self.errors as e:
            self.messages.pop()                        # unanswered: keep the conversation well-formed
            self.last_error = self.describe(e)
            if self.fatal(e):
                self.alive = False                     # a bad key won't get better by retrying this session
            raise RuntimeError(f"claude error: {self.last_error}") from e
        finally:
            self.pending = None
        # Append the whole reply (thinking and fallback blocks too): the API expects them back unchanged.
        self.messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        self._count(resp.usage)
        if resp.stop_reason == "refusal":
            raise RuntimeError("claude error: declined to translate this sentence")
        text = "".join(b.text for b in resp.content if b.type == "text")
        if has_markup(text):
            # Forget this exchange, so the slip isn't in the context the next sentence is translated with
            # (clean_output keeps it from listeners).
            del self.messages[-2:]
        return text

    # What a failed request raises, how it reads on the operator page, and whether this session is done for.
    errors: tuple[type[BaseException], ...] = (anthropic.APIError,)

    def describe(self, e: BaseException) -> str:
        return _describe(e)

    def fatal(self, e: BaseException) -> bool:
        return isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError))

    def _count(self, u: Any) -> None:
        step = Usage(input=u.input_tokens or 0, cache_read=getattr(u, "cache_read_input_tokens", 0) or 0,
                     cache_write=getattr(u, "cache_creation_input_tokens", 0) or 0,
                     output=u.output_tokens or 0, turns=1)
        step.cost_usd = cost_usd(self.model, step)
        self.usage.add(step)


def _describe(e: anthropic.APIError) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "the API key was refused: check it in Hear Church settings"
    if isinstance(e, anthropic.PermissionDeniedError):
        return "the API key may not use this model"
    if isinstance(e, anthropic.RateLimitError):
        return "rate limited by Anthropic (check the key's spend limit)"
    if isinstance(e, anthropic.APIConnectionError):
        return "cannot reach Anthropic (internet down?)"
    return str(getattr(e, "message", e))[:300]
