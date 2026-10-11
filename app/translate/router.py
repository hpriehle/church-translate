"""Translation router: Claude first, Ollama when Claude errors, is busy, or misses the deadline.

* A fresh Claude conversation is prepared in the background every `claude_session_turns`
  chunks (and after glossary/model changes), seeded with the last few chunks, then swapped in
  between two requests. This keeps context (and token use) bounded for long meetings.
* If Claude misses the deadline, Ollama starts; whichever answers first is used.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import TranslateCfg
from .claude_session import ClaudeSession, Usage, claude_binary
from .ollama import OllamaClient
from .prompts import build_system_prompt, clean_output, seed_message, user_message

log = logging.getLogger("translate")

HUNG_AFTER_S = 30.0
CIRCUIT_ERRORS = 3
CIRCUIT_PAUSE_S = 60.0


@dataclass
class TranslationResult:
    text: str | None
    engine: str          # "claude" | "ollama" | "none"
    ms: float
    note: str = ""


def _swallow(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


class Translator:
    def __init__(self, cfg: TranslateCfg, glossary: dict[str, Any], workdir: Path):
        self.cfg = cfg
        self.workdir = workdir
        self.system_prompt = build_system_prompt(glossary, cfg.audience)
        self.active: ClaudeSession | None = None
        self.standby: ClaudeSession | None = None
        self._standby_task: asyncio.Task | None = None
        self._reload_pending = False
        self._restart_after = 0.0
        self._spawn_failures = 0
        self._consecutive_errors = 0
        self._paused_until = 0.0
        self.history: deque[tuple[str, str]] = deque(maxlen=12)
        self.ollama = OllamaClient(cfg.ollama_url, cfg.ollama_model, cfg.ollama_timeout_s)
        self.retired_usage = Usage()
        self.counts: Counter[str] = Counter()
        self.last_error = ""
        self.has_claude = claude_binary() is not None
        self.sessions_started = 0

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        asyncio.create_task(self.ollama.warm_up(self.system_prompt))
        self._keep_warm = asyncio.create_task(self._ollama_keep_warm())
        if self.cfg.mode != "ollama":
            if not self.has_claude:
                self.last_error = "claude CLI not found — using Ollama only"
                log.warning(self.last_error)
            else:
                self._maintain()

    async def _ollama_keep_warm(self) -> None:
        while True:
            try:
                await self.ollama.keep_warm(self.system_prompt)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                await asyncio.sleep(60)

    async def close(self) -> None:
        if getattr(self, "_keep_warm", None):
            self._keep_warm.cancel()
        for s in (self.active, self.standby):
            if s:
                await s.close()
        await self.ollama.close()

    def reload(self, cfg: TranslateCfg, glossary: dict[str, Any]) -> None:
        new_prompt = build_system_prompt(glossary, cfg.audience)
        restart = (new_prompt != self.system_prompt or cfg.claude_model != self.cfg.claude_model
                   or cfg.claude_effort != self.cfg.claude_effort)
        self.cfg = cfg
        self.system_prompt = new_prompt
        self.ollama.url = cfg.ollama_url.rstrip("/")
        self.ollama.timeout_s = cfg.ollama_timeout_s
        if cfg.ollama_model != self.ollama.model:
            self.ollama.model = cfg.ollama_model
            self.ollama.warm = False
            asyncio.create_task(self.ollama.warm_up(self.system_prompt))
        if restart:
            self._reload_pending = True
            if self.standby:  # standby was built with the old prompt
                asyncio.create_task(self._retire(self.standby))
                self.standby = None
        self._maintain()

    # ------------------------------------------------------------ sessions
    def _maintain(self) -> None:
        if self.cfg.mode == "ollama" or not self.has_claude:
            return
        a = self.active
        need = a is None or not a.alive
        rotate = a is not None and a.alive and a.turns >= self.cfg.claude_session_turns
        if a is not None and a.alive and a.busy and a.busy_for > HUNG_AFTER_S:
            log.warning("claude session #%d hung for %.0fs — restarting", a.id, a.busy_for)
            asyncio.create_task(self._retire(a))
            self.active = None
            need = True
        if ((need or rotate or self._reload_pending) and self._standby_task is None
                and self.standby is None and time.monotonic() >= self._restart_after):
            self._standby_task = asyncio.create_task(self._prepare_standby())
        s = self.standby
        if s and s.ready and s.alive and (self.active is None or not self.active.alive or not self.active.busy):
            old, self.active, self.standby = self.active, s, None
            log.info("claude session #%d is now active", s.id)
            if old:
                asyncio.create_task(self._retire(old))

    async def _prepare_standby(self) -> None:
        s = ClaudeSession(self.system_prompt, self.cfg.claude_model, self.cfg.claude_effort, self.workdir)
        try:
            await s.start()
            self.sessions_started += 1
            await asyncio.wait_for(s.ask(seed_message(list(self.history))), timeout=90)
            s.ready = True
            self.standby = s
            self._reload_pending = False
            self._spawn_failures = 0
        except Exception as e:
            self._spawn_failures += 1
            backoff = min(120.0, 5.0 * 2 ** (self._spawn_failures - 1))
            self._restart_after = time.monotonic() + backoff
            self.last_error = f"Claude start failed: {e} {s.last_error}".strip()
            log.warning("%s (retry in %.0fs)", self.last_error, backoff)
            await s.close()
            self.retired_usage.add(s.usage)
        finally:
            self._standby_task = None
        self._maintain()

    async def _retire(self, s: ClaudeSession) -> None:
        await s.close()
        self.retired_usage.add(s.usage)

    # ------------------------------------------------------------ translate
    async def translate(self, english: str, condense: bool = False) -> TranslationResult:
        t0 = time.monotonic()
        self._maintain()
        note = ""
        claude_task: asyncio.Task | None = None
        s = self.active
        if self.cfg.mode != "ollama" and self.has_claude:
            if time.monotonic() < self._paused_until:
                note = "claude paused after errors"
            elif s and s.alive and s.ready and not s.busy:
                claude_task = asyncio.create_task(s.ask(user_message(english, condense)))
                claude_task.add_done_callback(_swallow)
            elif s and s.busy:
                note = "claude still busy"
            else:
                note = "claude starting" if (self._standby_task or self.standby) else "claude down"

        if claude_task:
            try:
                raw = await asyncio.wait_for(asyncio.shield(claude_task), timeout=self.cfg.claude_timeout_s)
                return self._finish(raw, "claude", t0, english, s, note)
            except asyncio.TimeoutError:
                note = f"claude > {self.cfg.claude_timeout_s:g}s"
            except Exception as e:
                note = "claude error"
                self._claude_error(str(e))

        # Fallback. A late Claude reply still wins if it arrives before Ollama's.
        ollama_task = asyncio.create_task(
            self.ollama.translate(self.system_prompt, list(self.history), english, condense))
        ollama_task.add_done_callback(_swallow)
        waiting: set[asyncio.Task] = {ollama_task}
        if claude_task and not claude_task.done():
            waiting.add(claude_task)
        deadline = time.monotonic() + self.cfg.ollama_timeout_s
        while waiting:
            done, waiting = await asyncio.wait(waiting, timeout=max(0.0, deadline - time.monotonic()),
                                               return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            for t in done:
                if t.exception() is not None:
                    if t is ollama_task:
                        self.ollama.last_error = str(t.exception()) or type(t.exception()).__name__
                        note += "; ollama failed"
                    else:
                        self._claude_error(str(t.exception()))
                    continue
                if t is claude_task:
                    ollama_task.cancel()
                    return self._finish(t.result(), "claude", t0, english, s, note + " (late reply used)")
                return self._finish(t.result(), "ollama", t0, english, None, note)
        ollama_task.cancel()
        self.counts["none"] += 1
        return TranslationResult(None, "none", (time.monotonic() - t0) * 1000, (note + "; no translation").strip("; "))

    def _claude_error(self, err: str) -> None:
        self.last_error = err[:300]
        self._consecutive_errors += 1
        if self._consecutive_errors >= CIRCUIT_ERRORS:
            self._paused_until = time.monotonic() + CIRCUIT_PAUSE_S
            self._consecutive_errors = 0
            log.warning("claude paused for %.0fs after repeated errors: %s", CIRCUIT_PAUSE_S, err)

    def _finish(self, raw: str, engine: str, t0: float, english: str,
                session: ClaudeSession | None, note: str) -> TranslationResult:
        text = clean_output(raw)
        if engine == "claude":
            self._consecutive_errors = 0
            if session:
                session.turns += 1
        if text:
            self.history.append((english, text))
        self.counts[engine] += 1
        self._maintain()
        return TranslationResult(text, engine, (time.monotonic() - t0) * 1000, note.strip())

    # ------------------------------------------------------------ status
    def usage(self) -> Usage:
        u = Usage()
        u.add(self.retired_usage)
        for s in (self.active, self.standby):
            if s:
                u.add(s.usage)
        return u

    def status(self) -> dict[str, Any]:
        a = self.active
        u = self.usage()
        if self.cfg.mode == "ollama":
            claude_state = "disabled"
        elif not self.has_claude:
            claude_state = "missing"
        elif a and a.alive and a.ready:
            claude_state = "busy" if a.busy else "ready"
        elif self._standby_task:
            claude_state = "starting"
        else:
            claude_state = "down"
        return {
            "mode": self.cfg.mode,
            "claude": {
                "state": claude_state,
                "model": self.cfg.claude_model,
                "session": a.id if a else None,
                "turns": a.turns if a else 0,
                "rotate_at": self.cfg.claude_session_turns,
                "busy_for": round(a.busy_for, 1) if a else 0,
                "rate_limit": a.rate_limit if a else None,
            },
            "ollama": {
                "model": self.ollama.model,
                "available": self.ollama.available,
                "model_present": self.ollama.model_present,
                "warm": self.ollama.warm,
                "error": "" if self.ollama.warm else self.ollama.last_error,
            },
            "counts": dict(self.counts),
            "usage": {"input": u.input, "cache_read": u.cache_read, "cache_write": u.cache_write,
                      "output": u.output, "cost_usd": round(u.cost_usd, 4), "turns": u.turns},
            "last_error": self.last_error,
        }
