"""Meeting health reports for the hub (POST /api/hc/health): what is happening on a church's Mac during a meeting, so
the desk can see it without asking them.

Every REPORT_EVERY_S while a meeting runs, the engine sends one small report: the speaker and translation languages,
the microphone in use and how loud it is (and for how long it heard something), how many pieces of speech were
recognised and how long, the speech recognition model (and any step-down), how far behind live the meeting runs,
translations and voices per language (asked / done / failed, with the last error message), listeners per language,
and the computer (CPU, app version, the engine's CPU and memory use). A report also goes out at once when the meeting
starts (a settings snapshot), when it ends, and on an engine error.

Never in a report: audio, anything that was said or translated (no transcript or caption text), anything about a
listener but how many there are. Error messages are cut to ERROR_CHARS.

Best effort: reports are made from counters the pipeline bumps as it goes (no work on the audio path but adding a
number), sent from a background task one batch at a time, and dropped when the hub can't be reached: the meeting never
waits for them. At most PENDING_KEEP wait for the next send.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
import threading
import time
from collections import deque
from typing import Any, Awaitable, Callable

log = logging.getLogger("health")

REPORT_EVERY_S = 30.0
LOUD_DB = -45.0          # a mic level above this (dBFS, ~100 ms RMS) counts as "heard something"
ERROR_CHARS = 200
PENDING_KEEP = 20        # reports waiting to be sent (older ones are dropped)
BATCH = 20
ERROR_EVERY_S = 10.0     # at most one immediate error report this often (the rest are counted in the next report)
SEND_TIMEOUT_S = 10.0


def _r(x: float | None, n: int = 1) -> float | None:
    return None if x is None else round(float(x), n)


def _cut(s: Any) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= ERROR_CHARS else s[:ERROR_CHARS - 1] + "…"


_SYSTEM: dict[str, Any] | None = None


def system_info() -> dict[str, Any]:
    """The computer and the app: asked once."""
    global _SYSTEM
    if _SYSTEM is None:
        cpu = ""
        try:
            if platform.system() == "Darwin":
                cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                                     timeout=2).stdout.strip()
            else:
                with open("/proc/cpuinfo") as f:
                    cpu = next((ln.split(":", 1)[1].strip() for ln in f if ln.startswith("model name")), "")
        except Exception:
            pass
        _SYSTEM = {"arch": platform.machine(), "cpu": cpu or platform.processor(), "cores": os.cpu_count() or 0,
                   "os": f"{platform.system()} {platform.mac_ver()[0] or platform.release()}".strip(),
                   "app": os.environ.get("HEARCHURCH_APP_VERSION", ""),
                   "build": os.environ.get("HEARCHURCH_APP_BUILD", "")}
    return dict(_SYSTEM)


class _Proc:
    """The engine's own CPU (% of one core, since the last call) and memory."""

    def __init__(self) -> None:
        try:
            import psutil
            self.p = psutil.Process()
            self.p.cpu_percent(None)
        except Exception:
            self.p = None

    def sample(self) -> dict[str, Any]:
        if self.p is None:
            return {}
        try:
            return {"cpu_pct": _r(self.p.cpu_percent(None)), "rss_mb": int(self.p.memory_info().rss / 2 ** 20)}
        except Exception:
            return {}


class HealthCollector:
    """Counters for one window of a meeting. The pipeline calls level/chunk (capture thread) and recognised/
    stt_failed/finished (event loop); `take` makes the report and starts a new window."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.t0 = self.clock()
            self.n_levels, self.power, self.peak_db, self.loud_s = 0, 0.0, None, 0.0
            self.vad_s = 0.0
            self.segments, self.asr_s, self.asr_empty, self.asr_failed, self.asr_behind = 0, 0.0, 0, 0, 0
            self.lag_max: float | None = None
            self.langs: dict[str, dict[str, int]] = {}   # detected language → what the chunks were recognised as
            self.outside = 0                             # chunks detected in a language that wasn't a speaker one
            self.tr: dict[str, dict[str, Any]] = {}
            self.voice: dict[str, dict[str, Any]] = {}
            self.errors, self.last_error = 0, ""

    # ---------------------------------------------------------------- capture thread
    def level(self, db: float, every_s: float = 0.1) -> None:
        """One mic level reading (dBFS of the loudest ~33 ms in ~100 ms)."""
        with self._lock:
            self.n_levels += 1
            self.power += 10 ** (db / 10)
            self.peak_db = db if self.peak_db is None else max(self.peak_db, db)
            if db > LOUD_DB:
                self.loud_s += every_s

    def chunk(self, voiced_s: float) -> None:
        """The voice detector cut a piece of speech of `voiced_s` voiced seconds."""
        with self._lock:
            self.vad_s += max(0.0, float(voiced_s))

    # ---------------------------------------------------------------- event loop
    def recognised(self, voiced_s: float, has_text: bool, lag_s: float | None) -> None:
        if has_text:
            self.segments += 1
            self.asr_s += max(0.0, float(voiced_s))
        else:
            self.asr_empty += 1
        self.lag(lag_s)

    def language(self, detected: str, recognised: str, speakers: list[str]) -> None:
        """A chunk with words: the language detector's pick vs what it was recognised as, and whether the detected
        language was one of the meeting's speaker languages at the time."""
        d = self.langs.setdefault(detected or "?", {})
        d[recognised or "?"] = d.get(recognised or "?", 0) + 1
        if detected and detected not in speakers:
            self.outside += 1

    def stt_failed(self) -> None:
        self.asr_failed += 1   # the error itself comes through the log (ErrorHandler)

    def stt_behind(self) -> None:
        self.asr_behind += 1

    def lag(self, s: float | None) -> None:
        if s is not None and s >= 0:
            self.lag_max = s if self.lag_max is None else max(self.lag_max, s)

    def error_seen(self, message: str) -> None:
        self.errors += 1
        self.last_error = _cut(message)

    def finished(self, rec: Any) -> None:
        """A chunk is done in every language (metrics.ChunkRecord): count its translations and voices. Only statuses,
        notes (our own messages, never what was said) and timings are read."""
        for code, res in (getattr(rec, "out", None) or {}).items():
            if res.status == "merged":   # translated together with an earlier chunk: counted there
                continue
            if res.engine != "original" and rec.src != code:
                t = self.tr.setdefault(code, {"req": 0, "ok": 0, "failed": 0, "skipped": 0, "err": ""})
                t["req"] += 1
                if res.t_caption:
                    t["ok"] += 1
                elif res.status == "failed":
                    t["failed"] += 1
                    t["err"] = _cut(res.note)
                else:
                    t["skipped"] += 1
            if res.t_caption:
                v = self.voice.setdefault(code, {"ok": 0, "failed": 0, "dropped": 0, "err": ""})
                if res.tts_ms or res.status == "aired":
                    v["ok"] += 1
                elif res.status == "failed":
                    v["failed"] += 1
                    v["err"] = _cut(res.note)
                elif res.status == "dropped":
                    v["dropped"] += 1

    def take(self) -> dict[str, Any]:
        """This window's numbers, and a new window."""
        with self._lock:
            mic = {"rms_db": _r(10 * _log10(self.power / self.n_levels)) if self.n_levels else None,
                   "peak_db": _r(self.peak_db), "loud_s": _r(self.loud_s), "speech_s": _r(self.vad_s)}
            window = self.clock() - self.t0
        out = {"window_s": _r(window), "mic": mic,
               "asr": {"segments": self.segments, "seconds": _r(self.asr_s), "empty": self.asr_empty,
                       "failed": self.asr_failed, "skipped_behind": self.asr_behind},
               "lag_max": _r(self.lag_max), "tr": self.tr, "voice": self.voice, "errors": self.errors}
        if self.langs:
            out["asr"]["langs"] = self.langs           # {"ru": {"ru": 12}, "en": {"en": 30}}: detected → recognised
            out["asr"]["outside_set"] = self.outside
        if self.last_error:
            out["last_error"] = self.last_error
        self.reset()
        return out


def _log10(x: float) -> float:
    import math
    return math.log10(max(x, 1e-12))


def lag_now(p: Any) -> float | None:
    """How far behind live the meeting is now: the newest caption's delay, or the oldest speech still waiting for
    speech recognition, whichever is more."""
    lags = [ch.behind_s for ch in getattr(p, "on_channels", {}).values() if getattr(ch, "behind_s", None) is not None]
    q = getattr(p, "chunk_q", None)
    try:
        if q is not None and not q.empty():
            lags.append(time.monotonic() - q._queue[0].t_speech_end)
    except Exception:
        pass
    return max(lags) if lags else None


def state(p: Any, hub_listeners: dict[str, dict[str, int]] | None = None) -> dict[str, Any]:
    """What a report says about the running meeting besides the window's counters."""
    chans = getattr(p, "on_channels", {}) or {}
    listeners = {}
    for code, ch in chans.items():
        n = int(getattr(ch.hub, "count", 0) or 0) + int(getattr(ch.broadcaster, "listeners", 0) or 0)
        h = (hub_listeners or {}).get(code) or {}
        listeners[code] = n + int(h.get("stream", 0) or 0) + int(h.get("captions", 0) or 0)
    stt = getattr(p, "stt", None)
    mic = p.mic_info() if hasattr(p, "mic_info") else {}
    q = getattr(p, "chunk_q", None)
    return {"speakers": list(getattr(p, "speakers", []) or []), "targets": list(chans),
            "input": mic.get("mic_name") or getattr(p, "source_label", "") or "",
            "mic_warning": mic.get("mic_warning") or None,
            "asr_engine": getattr(stt, "engine", ""), "asr_model": _model(p),
            "asr_stepped_down": getattr(stt, "stepped_down", "") or "", "asr_rtf": _r(getattr(stt, "rtf", None), 2),
            "asr_queue": q.qsize() if q is not None else 0,
            "lag_now": _r(lag_now(p)), "listeners": listeners}


def _model(p: Any) -> str:
    try:
        return p._stt_model()
    except Exception:
        return ""


def settings_snapshot(p: Any) -> dict[str, Any]:
    """The meeting's settings, for the start report (no names, no keys, no text)."""
    s = getattr(p, "settings", None)
    out: dict[str, Any] = {}
    try:
        out = {"input_device": s.audio.input_device or "default", "input_channel": s.audio.input_channel,
               "input_gain": s.audio.input_gain, "sharing": s.server.sharing, "translate": s.translate.backend,
               "asr_engine_setting": s.stt.engine}
    except Exception:
        pass
    voices = {}
    for code, ch in (getattr(p, "on_channels", {}) or {}).items():
        try:
            voices[code] = ch.tts.cfg.engine
        except Exception:
            pass
    out["voices"] = voices
    if getattr(p, "last_error", ""):
        out["last_error"] = _cut(p.last_error)
    return out


class HealthReporter:
    """Makes the reports of the meetings on this computer and sends them in batches with `post` (an async callable
    taking {"reports": [...]} and returning the HTTP status; raising when the hub can't be reached). `tick` is
    called about once a second; it never waits for the network."""

    def __init__(self, post: Callable[[dict[str, Any]], Awaitable[int]], clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.post, self.clock, self.wall = post, clock, wall
        self.collector = HealthCollector(clock)
        self.proc = _Proc()
        self.pending: deque[dict[str, Any]] = deque(maxlen=PENDING_KEEP)
        self.meeting = ""          # the meeting being reported (Pipeline.meeting_id), "" between meetings
        self.code = ""             # its hub session code, once known
        self.last_report = 0.0
        self.last_sent_at = -1e9
        self.last_error_report = -1e9
        self.urgent = False
        self._sending: asyncio.Task | None = None
        self.sent = self.dropped = 0

    # ---------------------------------------------------------------- hooks (Pipeline.health)
    def error(self, kind: str, message: str) -> None:
        """An engine error during a meeting: counted, and reported at once (at most every ERROR_EVERY_S)."""
        if not self.meeting:
            return
        msg = _cut(f"{kind}: {message}" if message else kind)
        self.collector.error_seen(msg)
        now = self.clock()
        if now - self.last_error_report >= ERROR_EVERY_S:
            self.last_error_report = now
            self._queue("error", {"error": {"type": _cut(kind)[:80], "message": _cut(message)}})

    # ---------------------------------------------------------------- the loop
    def tick(self, p: Any, session_code: str = "", hub_listeners: dict | None = None) -> None:
        now = self.clock()
        running = bool(getattr(p, "running", False))
        mid = getattr(p, "meeting_id", "") or ""
        if self.meeting and (not running or mid != self.meeting):   # the meeting is over
            self._report(p, "end", hub_listeners)
            self.meeting, self.code = "", ""
        if running and not self.meeting:
            self.meeting, self.code = mid, ""
            self.collector.reset()
            self.last_report = now
            self._report(p, "start", hub_listeners, {"settings": settings_snapshot(p)})
        if self.meeting:
            if session_code and not self.code:
                self.code = session_code
            self.collector.lag(lag_now(p))
            if now - self.last_report >= REPORT_EVERY_S:
                self._report(p, "periodic", hub_listeners)
        if self.pending and (self._sending is None or self._sending.done()):
            if self.urgent or now - self.last_sent_at >= REPORT_EVERY_S:
                self._send()

    def _report(self, p: Any, kind: str, hub_listeners: dict | None, extra: dict | None = None) -> None:
        self.last_report = self.clock()
        try:
            body = {**state(p, hub_listeners), **self.collector.take(),
                    "system": {**system_info(), **self.proc.sample()}, **(extra or {})}
        except Exception as e:   # a report never breaks a meeting
            log.debug("health report not made: %s", e)
            return
        self._queue(kind, body)

    def _queue(self, kind: str, body: dict[str, Any]) -> None:
        if len(self.pending) == self.pending.maxlen:
            self.dropped += 1
        self.pending.append({"v": 1, "kind": kind, "at": round(self.wall(), 1), "meeting": self.meeting,
                             "session": self.code, **body})
        if kind != "periodic":
            self.urgent = True

    def _send(self) -> None:
        batch = [self.pending.popleft() for _ in range(min(BATCH, len(self.pending)))]
        for r in batch:   # reports made before the hub named the session get its code now
            if not r["session"] and r["meeting"] == self.meeting:
                r["session"] = self.code
        self.urgent = False
        self.last_sent_at = self.clock()
        self._sending = asyncio.ensure_future(self._post(batch))

    async def _post(self, batch: list[dict[str, Any]]) -> None:
        try:
            status = await asyncio.wait_for(self.post({"reports": batch}), SEND_TIMEOUT_S)
            if status == 200:
                self.sent += len(batch)
            else:
                self.dropped += len(batch)
                log.debug("health reports not taken: HTTP %s", status)
        except asyncio.CancelledError:
            raise
        except Exception as e:   # offline, slow hub: these reports are dropped
            self.dropped += len(batch)
            log.debug("health reports not sent: %s", e)


class ErrorHandler(logging.Handler):
    """Engine log records at ERROR and above → HealthReporter.error (type and message; never a traceback). The hub's
    own loggers (when the engine runs in the hub) and the web server's are left out."""

    SKIP = ("hear", "uvicorn", "health")

    def __init__(self, reporter: HealthReporter) -> None:
        super().__init__(logging.ERROR)
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.split(".")[0] in self.SKIP:
            return
        try:
            exc = record.exc_info[1] if record.exc_info else None
            if exc is not None:
                self.reporter.error(type(exc).__name__, f"{record.getMessage()}: {exc}")
            else:
                self.reporter.error(record.name, record.getMessage())
        except Exception:
            pass
