"""Local fallback translator through Ollama's HTTP API."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .prompts import user_message

log = logging.getLogger("ollama")


class OllamaClient:
    def __init__(self, url: str, model: str, timeout_s: float):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.available = False
        self.model_present = False
        self.warm = False
        self.last_error = ""
        self.last_ms = 0.0
        self.last_used = 0.0
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=2.0))

    async def check(self) -> bool:
        try:
            r = await self._client.get(f"{self.url}/api/tags", timeout=3)
            r.raise_for_status()
            names = {m.get("name") for m in r.json().get("models", [])}
            self.available = True
            wanted = self.model if ":" in self.model else f"{self.model}:latest"
            self.model_present = wanted in names or self.model in names
            if not self.model_present:
                self.last_error = f"model {self.model} not pulled (run: ollama pull {self.model})"
        except Exception as e:
            self.available = False
            self.model_present = False
            self.last_error = f"Ollama not reachable at {self.url} ({type(e).__name__})"
        return self.available and self.model_present

    def _messages(self, system: str, history: list[tuple[str, str]], english: str, condense: bool) -> list[dict]:
        msgs = [{"role": "system", "content": system}]
        for en, fr in history[-3:]:
            msgs.append({"role": "user", "content": en})
            msgs.append({"role": "assistant", "content": fr})
        msgs.append({"role": "user", "content": user_message(english, condense)})
        return msgs

    async def translate(self, system: str, history: list[tuple[str, str]], english: str,
                        condense: bool = False, timeout_s: float | None = None) -> str:
        t0 = time.monotonic()
        body = {
            "model": self.model,
            "messages": self._messages(system, history, english, condense),
            "stream": False,
            "keep_alive": "3h",
            "options": {"temperature": 0.2, "num_predict": 300, "num_ctx": 8192},
        }
        r = await self._client.post(f"{self.url}/api/chat", json=body, timeout=timeout_s or self.timeout_s)
        r.raise_for_status()
        self.last_used = time.monotonic()
        self.last_ms = (time.monotonic() - t0) * 1000
        return r.json().get("message", {}).get("content", "")

    async def warm_up(self, system: str) -> None:
        """Load the model and cache the (long) system prompt so the first real fallback is fast."""
        if not await self.check():
            log.warning("ollama: %s", self.last_error)
            return
        try:
            t0 = time.monotonic()
            await self.translate(system, [], "Good morning, brothers and sisters.", timeout_s=180)
            self.warm = True
            log.info("ollama %s warm in %.1fs", self.model, time.monotonic() - t0)
        except Exception as e:
            self.last_error = f"warm-up failed: {e}"
            log.warning("ollama warm-up failed: %s", e)

    async def keep_warm(self, system: str, idle_s: float = 1200) -> None:
        """While Claude does the work, touch Ollama now and then so a fallback never starts cold."""
        while True:
            await asyncio.sleep(60)
            if time.monotonic() - self.last_used > idle_s:
                try:
                    await self.translate(system, [], "Thank you, brothers and sisters.", timeout_s=180)
                    self.warm = True
                except Exception as e:
                    self.warm = False
                    self.last_error = f"keep-warm failed: {e}"
                    self.last_used = time.monotonic()  # don't retry every minute

    async def close(self) -> None:
        await self._client.aclose()
