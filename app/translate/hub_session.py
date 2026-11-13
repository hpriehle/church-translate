"""One translation conversation through the Hear Church hub (a congregation on prepaid credits).

The Mac has no Anthropic key: each turn goes to the hub's POST /api/hc/translate with the device token, and the hub
asks Claude with its own key (app/hear/proxy.py) and hands back Anthropic's answer unchanged. Everything else is
ApiSession's: the same model, effort, prompt, cached append-only conversation, timeouts and fallbacks.

The first message of a conversation (the seed: "reply simply: OK", with recent context) is not sent on its own:
it is kept and goes out with the first sentence of the meeting. So an open app with no meeting costs nothing and
needs no live session on the hub, which only translates for a meeting that is live there.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import anthropic
import httpx
from anthropic._models import construct_type   # how the SDK itself builds a reply from JSON (any block type)
from anthropic.types.beta import BetaMessage

from .api_session import ApiSession

log = logging.getLogger("claude")

SEED_REPLY = "OK"
TIMEOUT_S = 60.0     # like the API client's: the router has its own, shorter deadlines


class HubError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def hub_http_url(ws_url: str) -> str:
    """wss://hearchurch.com/ws/engine → https://hearchurch.com"""
    u = urlparse(ws_url)
    return f"{'https' if u.scheme == 'wss' else 'http'}://{u.netloc}"


def hub_credentials() -> tuple[str, str]:
    """(hub base URL, device token) of this Mac, from the settings (the Mac app passes them in the environment)."""
    from .. import main

    eng = main.S.settings.engine
    return (hub_http_url(eng.hub_url) if eng.hub_url else ""), eng.device_token or ""


class HubClient:
    """Stands in for anthropic.AsyncAnthropic: client.beta.messages.create(**params) → the hub → a BetaMessage."""

    def __init__(self, base_url: str, token: str, http: httpx.AsyncClient | None = None):
        self.url = base_url.rstrip("/") + "/api/hc/translate"
        self.token = token
        self.http = http or httpx.AsyncClient(timeout=TIMEOUT_S)

    @property
    def beta(self):
        return self

    @property
    def messages(self):
        return self

    async def create(self, *, model: str, system: str, messages: list, output_config: Any = anthropic.omit,
                     **_: Any) -> BetaMessage:
        effort = output_config.get("effort", "") if isinstance(output_config, dict) else ""
        try:
            r = await self.http.post(self.url, headers={"Authorization": f"Bearer {self.token}"},
                                     json={"model": model, "system": system, "messages": messages, "effort": effort})
        except httpx.HTTPError as e:
            raise HubError(0, "unreachable", f"cannot reach Hear Church ({type(e).__name__})") from e
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code != 200:
            data = data if isinstance(data, dict) else {}
            raise HubError(r.status_code, str(data.get("error") or r.status_code),
                           str(data.get("message") or data.get("detail") or f"Hear Church answered {r.status_code}")[:300])
        return construct_type(type_=BetaMessage, value=data)

    async def close(self) -> None:
        await self.http.aclose()


class HubSession(ApiSession):
    errors = (HubError,)

    def __init__(self, system_prompt: str, model: str, effort: str, workdir=None, client: Any = None):
        super().__init__(system_prompt, model, effort, workdir, client)
        self.auth = "Hear Church credits (through the hub)"

    async def start(self) -> None:
        if self.client is None:
            base, token = hub_credentials()
            if not (base and token):
                raise RuntimeError("this Mac isn't signed in to Hear Church")
            self.client = HubClient(base, token)
        self.alive = True
        log.info("claude hub session #%d started (model %s)", self.id, self.model)

    async def close(self) -> None:
        await super().close()
        if isinstance(self.client, HubClient):
            await self.client.close()

    async def ask(self, text: str) -> str:
        if not self.messages and not self.ready and self.alive and not self.pending:
            # The seed: kept for the first real sentence instead of a request of its own.
            self.messages += [{"role": "user", "content": text}, {"role": "assistant", "content": SEED_REPLY}]
            return SEED_REPLY
        return await super().ask(text)

    def describe(self, e: BaseException) -> str:
        return getattr(e, "message", "") or str(e)

    def fatal(self, e: BaseException) -> bool:
        return isinstance(e, HubError) and e.status in (401, 403)   # this Mac was signed out of Hear Church
