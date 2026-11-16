"""Diagnostic logs for Hear Church support: a scrubbed copy of the engine's log, so support can see what went wrong on
a church's Mac without asking them.

Technical only. Every log record goes through `scrub` before it reaches the shared log: the message template (our
own words, from the code) is kept, and every value put into it is checked. Numbers, language codes and names, model
and voice names, versions, paths, error types and short status words stay; any other text (a caption, a transcript,
a translation, a glossary hit, a quoted string inside an error message) becomes its length, e.g. "‹42 chars›".
Tracebacks keep only the exception type and where it was raised (file, line, function). DEBUG records and the hub's
own loggers are left out.

The shared log is written on this Mac whatever the setting (logs/shared.log, JSON lines, about 4 MB at most), so
"Send logs to support" in Settings can send the last 7 days. With "Share diagnostic logs" on (the default), the
engine also sends new lines to the hub (POST /api/hc/logs, device token) in small batches; the hub keeps them 30 days.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import re
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("sharelog")

SHARED_NAME = "shared.log"
MAX_BYTES = 1_000_000     # a shared log file; SHARED_BACKUPS older ones are kept beside it
SHARED_BACKUPS = 3
LINE_CHARS = 500          # a shared message is cut to this
TOKEN_CHARS = 80          # a value longer than this is never kept as is
SKIP = ("hear", "uvicorn", "sharelog", "httpx", "httpcore")

# Values kept as they are: short status and engine words the code puts in messages.
SAFE_WORDS = {
    "on", "off", "true", "false", "none", "failed", "started", "stopped", "running", "idle", "busy", "local",
    "cloud", "hub", "api", "credits", "own_key", "internal", "auto", "ollama", "claude", "piper", "mms", "speecht5",
    "whisper", "faster-whisper", "mlx", "mlx-whisper", "whisper.cpp", "parakeet", "vosk", "original", "aired",
    "dropped", "merged", "skipped", "stt", "tts", "vad", "periodic", "publish", "serve", "tiny", "base", "small",
    "medium", "large", "turbo", "int8", "float16", "float32", "cpu", "gpu", "metal", "cuda",
}
# Words kept in an error message (one from a library, or ours with a value in it): technical words that hardly occur
# in a sermon. Any other word (including "the", "and", "not") becomes part of a "‹N chars›" run, so a sentence
# somebody said can't come through an exception: at most a stray technical word of it could.
ERROR_WORDS = set("""
error errors errno failed failure exception invalid unexpected traceback
connection connect connected disconnected refused reset timeout timed network unreachable dns ssl certificate
handshake http https status server client proxy socket eof url hostname
file directory folder permission unauthorized authentication auth api token overloaded quota exceeded rate
microphone mic audio device input output buffer underflow overflow disk killed terminated exited crashed thread
cancelled canceled hung queue queued installed download downloading downloaded unavailable unsupported
json decode encode utf-8 unicode bytes retry retrying translator recognizer recogniser engine
""".split())
_WORD = re.compile(r"‹[^›\n]*›|\w[\w'’.-]*", re.UNICODE)
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/@+=%#~,-]+$")
_TECH = re.compile(r"[0-9_/:@=%#~]|\.[A-Za-z0-9]")   # a digit, a path, an id: not a word somebody said
_ABBREV = re.compile(r"^(?:[A-Za-z]\.)+[A-Za-z]?\.?$")   # "U.S.", "e.g."
_QUOTED = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"|“[^”\n]*”|‘[^’\n]*’|«[^»\n]*»|„[^“”\n]*[“”]")
_LANG_WORDS: set[str] | None = None


def _lang_words() -> set[str]:
    global _LANG_WORDS
    if _LANG_WORDS is None:
        try:
            from .languages import LANGUAGES
            _LANG_WORDS = {c.lower() for c in LANGUAGES} | {lang.name.lower() for lang in LANGUAGES.values()}
        except Exception:   # pragma: no cover
            _LANG_WORDS = set()
        _LANG_WORDS |= {"en", "english", "fr", "wo"}
    return _LANG_WORDS


def hidden(s: str) -> str:
    return f"‹{len(s)} chars›"


def _home(s: str) -> str:
    h = os.path.expanduser("~")
    return s.replace(h, "~") if h and h != "~" else s


def safe_token(s: str) -> bool:
    """A value that can't be something somebody said: a number, a code, a version, a model or voice name, a path."""
    if not s or len(s) > TOKEN_CHARS:
        return False
    low = s.lower()
    return low in SAFE_WORDS or low in _lang_words() or tech_token(s)


def tech_token(s: str) -> bool:
    """A number, a version, a code, a model or voice name, a path: has a digit or a path/id character in it."""
    return 0 < len(s) <= TOKEN_CHARS and bool(_TOKEN.match(s)) and not _ABBREV.match(s) and bool(_TECH.search(s))


def scrub_text(s: str) -> str:
    """Free text (an error message, a formatted line): quoted parts become their length, home folders become ~."""
    s = _home(" ".join(str(s).split()))
    return _QUOTED.sub(lambda m: m.group(0) if safe_token(m.group(0)[1:-1]) else hidden(m.group(0)[1:-1]), s)


def scrub_error(s: str) -> str:
    """An error message: its technical words, numbers, codes and punctuation stay; runs of other words (anything
    somebody might have said) become their length."""
    s = scrub_text(s)
    out: list[str] = []
    pos, start, end = 0, -1, -1
    for m in _WORD.finditer(s):
        w = m.group(0)
        if w.startswith("‹") or w.lower() in ERROR_WORDS or tech_token(w):
            if start >= 0:
                out.append(hidden(s[start:end]))
                pos, start = end, -1
            out.append(s[pos:m.end()])
            pos = m.end()
        else:
            if start < 0:
                out.append(s[pos:m.start()])
                start = m.start()
            end = m.end()
    if start >= 0:
        out.append(hidden(s[start:end]))
        pos = end
    out.append(s[pos:])
    return "".join(out)


def scrub_value(v: Any) -> Any:
    """A value going into a log message."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, BaseException):
        return f"{type(v).__name__}: {scrub_error(str(v))}" if str(v) else type(v).__name__
    if isinstance(v, os.PathLike):
        return _home(os.fspath(v))
    if isinstance(v, str):
        return _home(v) if safe_token(_home(v)) else hidden(v)
    if isinstance(v, bytes):
        return f"‹{len(v)} bytes›"
    if isinstance(v, (list, tuple, set, frozenset)):
        items = list(v)
        return "[" + ", ".join(str(scrub_value(x)) for x in items[:20]) + (", …" if len(items) > 20 else "") + "]"
    if isinstance(v, dict):
        items = list(v.items())[:20]
        return "{" + ", ".join(f"{scrub_value(k)}: {scrub_value(x)}" for k, x in items) + "}"
    return f"‹{type(v).__name__}›"


class _Safe:
    """A scrubbed value that %r and %s both print as is."""

    def __init__(self, v: Any) -> None:
        self.v = v

    def __str__(self) -> str:
        return str(self.v)

    __repr__ = __str__

    def __format__(self, spec: str) -> str:
        try:
            return format(self.v, spec)
        except (TypeError, ValueError):
            return str(self.v)


def scrub_message(record: logging.LogRecord) -> str:
    """The record's message with every value scrubbed (the template is the code's own text)."""
    msg = record.msg
    if not isinstance(msg, str):
        return str(scrub_value(msg))
    args = record.args
    if args:
        try:
            def one(v: Any) -> Any:
                return v if isinstance(v, (int, float)) and not isinstance(v, bool) else _Safe(scrub_value(v))
            text = msg % ({k: one(v) for k, v in args.items()} if isinstance(args, dict) else tuple(map(one, args)))
        except Exception:
            text = msg + " " + " ".join(str(scrub_value(a)) for a in (args if isinstance(args, tuple) else (args,)))
    else:
        text = msg
    return scrub_text(text)


def scrub_exception(exc_info: Any) -> str:
    """Type, scrubbed message, and where: "RuntimeError: ‹12 chars› at pipeline.py:381 in _translate < …"."""
    try:
        etype, exc, tb = exc_info
    except Exception:
        return ""
    if etype is None:
        return ""
    where = [f"{Path(f.filename).name}:{f.lineno} in {f.name}" for f in traceback.extract_tb(tb)][-4:]
    head = scrub_value(exc) if exc is not None else etype.__name__
    return f"{head}" + (f" at {' < '.join(reversed(where))}" if where else "")


def scrub(record: logging.LogRecord) -> dict[str, Any] | None:
    """One shared log line for a record, or None for a record that is never shared."""
    if record.levelno < logging.INFO or record.name.split(".")[0] in SKIP:
        return None
    msg = scrub_message(record)
    if record.exc_info:
        exc = scrub_exception(record.exc_info)
        if exc:
            msg += " | " + exc
    if len(msg) > LINE_CHARS:
        msg = msg[:LINE_CHARS - 1] + "…"
    return {"t": round(record.created, 3), "lvl": record.levelname, "src": record.name[:40], "msg": msg}


class SharedLogHandler(logging.Handler):
    """Writes scrubbed lines to logs/shared.log (rotated by size) and hands each to `sink` (the uploader)."""

    def __init__(self, directory: Path, sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        super().__init__(logging.INFO)
        directory.mkdir(parents=True, exist_ok=True)
        self.file = logging.handlers.RotatingFileHandler(directory / SHARED_NAME, maxBytes=MAX_BYTES,
                                                         backupCount=SHARED_BACKUPS, encoding="utf-8")
        self.file.setFormatter(logging.Formatter("%(message)s"))
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = scrub(record)
            if line is None:
                return
            out = logging.makeLogRecord({"msg": json.dumps(line, ensure_ascii=False), "levelno": record.levelno,
                                         "levelname": record.levelname})
            self.file.emit(out)
            if self.sink is not None:
                self.sink(line)
        except Exception:   # a log line never breaks the engine
            pass

    def close(self) -> None:
        self.file.close()
        super().close()


def read_shared(directory: Path, since: float) -> list[dict[str, Any]]:
    """The shared log's lines written after `since` (unix time), oldest first."""
    out: list[dict[str, Any]] = []
    files = [directory / f"{SHARED_NAME}.{i}" for i in range(SHARED_BACKUPS, 0, -1)] + [directory / SHARED_NAME]
    for f in files:
        try:
            for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(d, dict) and float(d.get("t") or 0) > since:
                    out.append(d)
        except OSError:
            continue
    return out


# ------------------------------------------------------------------ sending to the hub

SEND_EVERY_S = 60.0
PENDING_KEEP = 2000       # lines waiting to be sent (older ones are dropped)
BATCH_LINES = 300
BATCH_BYTES = 48 * 1024   # the hub takes at most 64 KB a request
SEND_TIMEOUT_S = 15.0


class LogShipper:
    """New shared lines → the hub, in batches, while sharing is on. Best effort: a batch the hub doesn't take is
    dropped; nothing here ever waits on the meeting."""

    def __init__(self, post: Callable[[dict[str, Any]], Awaitable[int]], clock: Callable[[], float] = time.monotonic):
        self.post, self.clock = post, clock
        self.pending: deque[dict[str, Any]] = deque(maxlen=PENDING_KEEP)
        self.enabled = False
        self.last_sent_at = -1e9
        self._sending: asyncio.Task | None = None
        self.sent = self.dropped = 0

    def add(self, line: dict[str, Any]) -> None:
        if self.enabled:
            self.pending.append(line)

    def set_enabled(self, on: bool) -> None:
        if not on:
            self.pending.clear()
        self.enabled = bool(on)

    def tick(self) -> None:
        if not (self.enabled and self.pending) or (self._sending is not None and not self._sending.done()):
            return
        urgent = any(ln.get("lvl") in ("ERROR", "CRITICAL") for ln in list(self.pending)[:BATCH_LINES])
        if not urgent and self.clock() - self.last_sent_at < SEND_EVERY_S:
            return
        batch, size = [], 0
        while self.pending and len(batch) < BATCH_LINES:
            n = len(json.dumps(self.pending[0], ensure_ascii=False)) + 2
            if batch and size + n > BATCH_BYTES:
                break
            batch.append(self.pending.popleft())
            size += n
        self.last_sent_at = self.clock()
        self._sending = asyncio.ensure_future(self._post(batch))

    async def _post(self, batch: list[dict[str, Any]]) -> None:
        try:
            status = await asyncio.wait_for(self.post({"kind": "stream", "lines": batch}), SEND_TIMEOUT_S)
            if status == 200:
                self.sent += len(batch)
            else:
                self.dropped += len(batch)
        except asyncio.CancelledError:
            raise
        except Exception:   # offline, slow hub: dropped
            self.dropped += len(batch)


SHIPPER: LogShipper | None = None
_HANDLER: SharedLogHandler | None = None


def install(directory: Path) -> SharedLogHandler:
    """The shared log for this process (once): scrubbed lines to logs/shared.log, and to the uploader once one is
    attached (attach)."""
    global _HANDLER
    if _HANDLER is None:
        _HANDLER = SharedLogHandler(directory, lambda line: SHIPPER.add(line) if SHIPPER is not None else None)
        logging.getLogger().addHandler(_HANDLER)
    return _HANDLER


def attach(shipper: LogShipper | None) -> None:
    global SHIPPER
    SHIPPER = shipper
