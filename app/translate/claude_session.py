"""One long-lived `claude -p` process speaking stream-json.

Only one request is in flight at a time (Claude Code may merge messages that arrive while it is
busy, which would break the one-reply-per-chunk mapping). The router decides what to do while a
request is slow; a late reply simply frees the session again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("claude")

_HELP_CACHE: str | None = None


def claude_binary() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for p in ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        pp = Path(p).expanduser()
        if pp.exists():
            return str(pp)
    return None


def _supported_flags(binary: str) -> str:
    global _HELP_CACHE
    if _HELP_CACHE is None:
        try:
            _HELP_CACHE = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=20).stdout
        except Exception:  # pragma: no cover
            _HELP_CACHE = ""
    return _HELP_CACHE


@dataclass
class Usage:
    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    cost_usd: float = 0.0
    turns: int = 0

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.output += other.output
        self.cost_usd += other.cost_usd
        self.turns += other.turns


@dataclass
class _Pending:
    future: asyncio.Future
    sent_at: float = field(default_factory=time.monotonic)


class ClaudeSession:
    _counter = 0

    def __init__(self, system_prompt: str, model: str, effort: str, workdir: Path):
        ClaudeSession._counter += 1
        self.id = ClaudeSession._counter
        self.system_prompt = system_prompt
        self.model = model
        self.effort = effort
        self.workdir = workdir
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: _Pending | None = None
        self.turns = 0                    # translation turns answered (seed turn excluded)
        self.usage = Usage()
        self.alive = False
        self.ready = False
        self.last_error = ""
        self.rate_limit: dict | None = None
        self._last_cost_total = 0.0
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle
    def _command(self, binary: str) -> list[str]:
        help_text = _supported_flags(binary)
        cmd = [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--model", self.model, "--system-prompt", self.system_prompt]
        if "--effort" in help_text and self.effort:
            cmd += ["--effort", self.effort]
        if "--tools" in help_text:
            cmd += ["--tools", ""]
        else:
            cmd += ["--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit"]
        if "--safe-mode" in help_text:
            cmd += ["--safe-mode"]          # no CLAUDE.md, hooks, plugins or MCP servers
        if "--strict-mcp-config" in help_text:
            cmd += ["--strict-mcp-config"]
        if "--no-session-persistence" in help_text:
            cmd += ["--no-session-persistence"]
        return cmd

    async def start(self) -> None:
        binary = claude_binary()
        if not binary:
            raise RuntimeError("the `claude` CLI is not installed")
        self.workdir.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE") and k != "CLAUDE_CODE_ENTRYPOINT"}
        self.proc = await asyncio.create_subprocess_exec(
            *self._command(binary), cwd=str(self.workdir), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        self.alive = True
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr = asyncio.create_task(self._read_stderr())
        log.info("claude session #%d started (pid %s, model %s)", self.id, self.proc.pid, self.model)

    async def close(self) -> None:
        self.alive = False
        self.ready = False
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
        self._fail_pending("session closed")

    # ------------------------------------------------------------ requests
    @property
    def busy(self) -> bool:
        return self.pending is not None

    @property
    def busy_for(self) -> float:
        return time.monotonic() - self.pending.sent_at if self.pending else 0.0

    async def ask(self, text: str) -> str:
        if not self.alive or not self.proc or self.proc.returncode is not None:
            raise RuntimeError("claude session is not running")
        if self.pending:
            raise RuntimeError("claude session is busy")
        fut = asyncio.get_running_loop().create_future()
        self.pending = _Pending(fut)
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        try:
            self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
        except Exception as e:
            self.pending = None
            self.alive = False
            raise RuntimeError(f"cannot write to claude: {e}") from e
        return await fut

    def _fail_pending(self, reason: str) -> None:
        if self.pending and not self.pending.future.done():
            self.pending.future.set_exception(RuntimeError(reason))
        self.pending = None

    # ------------------------------------------------------------ readers
    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type")
                if etype == "result":
                    self._on_result(ev)
                elif etype == "rate_limit_event":
                    self.rate_limit = ev.get("rate_limit_info") or ev
        except Exception as e:  # pragma: no cover
            log.warning("claude reader error: %s", e)
        finally:
            self.alive = False
            self.ready = False
            code = await self.proc.wait()
            log.warning("claude session #%d exited (code %s) %s", self.id, code, self.last_error)
            self._fail_pending(f"claude exited (code {code}) {self.last_error}".strip())

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for raw in self.proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                self.last_error = line[-300:]
                log.debug("claude stderr: %s", line)

    def _on_result(self, ev: dict) -> None:
        u = ev.get("usage") or {}
        total = float(ev.get("total_cost_usd") or 0.0)
        step = Usage(
            input=int(u.get("input_tokens") or 0),
            cache_read=int(u.get("cache_read_input_tokens") or 0),
            cache_write=int(u.get("cache_creation_input_tokens") or 0),
            output=int(u.get("output_tokens") or 0),
            cost_usd=max(0.0, total - self._last_cost_total),
            turns=1,
        )
        self._last_cost_total = total
        self.usage.add(step)
        p, self.pending = self.pending, None
        if p is None or p.future.done():
            return
        if ev.get("is_error") or ev.get("subtype") not in (None, "success"):
            msg = ev.get("result") or ev.get("subtype") or "error"
            self.last_error = str(msg)[:300]
            p.future.set_exception(RuntimeError(f"claude error: {msg}"))
        else:
            p.future.set_result(ev.get("result") or "")
