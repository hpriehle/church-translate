"""Translation for "internal" congregations (Harrison's own): through the hub, at no charge, with Claude Code.

The Mac's hub backend sends each turn to POST /api/hc/translate as a Messages API request (the whole conversation so
far, app/translate/hub_session.py). For a congregation on credits the hub asks the API with its key (proxy.py). For
an "internal" one it asks instead a long-lived `claude -p` process (app/translate/claude_session.py) signed in with
Harrison's Claude Code login: CLAUDE_CODE_OAUTH_TOKEN from data/secrets.env, or the server user's `claude` login.
ANTHROPIC_API_KEY is taken out of that process's environment, so it never falls back to the paid key.

One process per conversation: keyed by the Mac, model, effort, system prompt and the conversation's first message
(the seed, which carries the meeting's recent context). The process keeps its own context, so each turn only sends
the newest sentence. A conversation the hub hasn't seen (new, or the hub restarted) starts a process, which first
gets the seed (its reply is dropped), then the sentence. The reply goes back shaped like a Messages API reply, so the
Mac handles it exactly like one from the API.

Processes idle for IDLE_S are closed; at most MAX_SESSIONS run at once (the least recently used idle one goes).
Privacy as in proxy.py: nothing here stores or logs the speech or its translation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import HOME
from ..translate.claude_session import ClaudeSession

log = logging.getLogger("hear")

IDLE_S = 600.0          # a conversation not used for 10 minutes is over (the Mac replaces its conversations sooner)
MAX_SESSIONS = 24       # each is a Claude Code process (~150 MB): a few congregations' languages at once
TURN_TIMEOUT_S = 60.0   # like the proxy's client timeout: the Mac's translator has its own, shorter deadlines
WORKDIR = HOME / "data" / "internal-claude"


class InternalError(Exception):
    pass


class _HubClaude(ClaudeSession):
    subscription_only = True


@dataclass
class _Conv:
    session: ClaudeSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    used: float = field(default_factory=time.monotonic)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def conversation_key(device_id: int, model: str, effort: str, system: str, first: Any) -> str:
    h = hashlib.sha256(json.dumps([system, _text(first)], ensure_ascii=False).encode()).hexdigest()[:32]
    return f"{device_id}:{model}:{effort}:{h}"


class Bridge:
    session_type: type[ClaudeSession] = _HubClaude

    def __init__(self) -> None:
        self.convs: dict[str, _Conv] = {}
        self.turns = 0

    async def _evict(self) -> None:
        now = time.monotonic()
        for k, c in list(self.convs.items()):
            if now - c.used > IDLE_S and not c.lock.locked():
                del self.convs[k]
                await c.session.close()
        while len(self.convs) >= MAX_SESSIONS:
            idle = [(c.used, k) for k, c in self.convs.items() if not c.lock.locked()]
            if not idle:
                raise InternalError("busy")
            _, k = min(idle)
            await self.convs.pop(k).session.close()

    async def _ask(self, key: str, conv: _Conv, text: str) -> str:
        try:
            return await asyncio.wait_for(conv.session.ask(text), TURN_TIMEOUT_S)
        except (asyncio.TimeoutError, RuntimeError) as e:
            if self.convs.get(key) is conv:
                del self.convs[key]
            await conv.session.close()
            raise InternalError(type(e).__name__) from e

    async def turn(self, device_id: int, model: str, system: str, messages: list[dict[str, Any]],
                   effort: str) -> dict[str, Any]:
        """One translation turn: the reply to messages[-1] (a user message), shaped like a Messages API reply."""
        key = conversation_key(device_id, model, effort, system, messages[0]["content"])
        conv = self.convs.get(key)
        fresh = conv is None or not conv.session.alive
        if fresh:
            if conv is not None:
                del self.convs[key]
            await self._evict()
            s = self.session_type(system, model, effort, WORKDIR)
            try:
                await s.start()
            except Exception as e:  # noqa: BLE001 (no claude binary, …)
                raise InternalError(str(e)[:200]) from e
            if key in self.convs:          # another request started this conversation meanwhile
                await s.close()
                conv, fresh = self.convs[key], False
            else:
                conv = self.convs[key] = _Conv(s)
        async with conv.lock:
            conv.used = time.monotonic()
            before = (conv.session.usage.input, conv.session.usage.output, conv.session.usage.cache_read,
                      conv.session.usage.cache_write)
            if fresh and len(messages) > 1:
                await self._ask(key, conv, _text(messages[0]["content"]))   # the seed: its context, reply dropped
            reply = await self._ask(key, conv, _text(messages[-1]["content"]))
            conv.used = time.monotonic()
        u = conv.session.usage
        self.turns += 1
        return {"id": f"msg_hub_internal_{self.turns}", "type": "message", "role": "assistant", "model": model,
                "content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": u.input - before[0], "output_tokens": u.output - before[1],
                          "cache_read_input_tokens": u.cache_read - before[2],
                          "cache_creation_input_tokens": u.cache_write - before[3]}}

    async def close(self) -> None:
        for c in self.convs.values():
            await c.session.close()
        self.convs.clear()


bridge = Bridge()
