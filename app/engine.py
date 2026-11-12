"""Let a faster computer (e.g. a Mac) run the meetings of a public server (e.g. a VPS).

The public server (the "hub") keeps the pages, passwords and listener streams. A computer set up as
its engine connects out to it (no tunnel or open port needed). While the engine is connected and
idle, the hub hands each meeting to it: the speaker's audio goes to the engine, and the engine's
captions and voice come back through the hub to every phone. If the engine is offline or busy, the
hub runs the meeting itself, and if the engine drops out mid-meeting the hub carries on.

One WebSocket, /ws/engine, with "Authorization: Bearer <engine.token>":
  engine → hub   {"type": "hello", name, languages, stream, busy}   {"type": "busy", busy}
                 {"type": "started"} | {"type": "error", message}     {"type": "done"}
                 {"type": "hub", lang, msg, remember}  (a listener message for that language)
                 {"type": "op", msg}                   (operator feed: status, chunk, log)
                 binary: one byte = index of the language in hello.languages, then MP3 bytes
  hub → engine   {"type": "start", sample_rate, languages, device, record}   binary: int16 PCM
                 {"type": "stop"}   {"type": "languages", languages}
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse

import numpy as np

log = logging.getLogger("engine")

PCM_RATE = 16000          # what the hub forwards: speech recognition needs no more
DONE_TIMEOUT_S = 90.0     # a stopped meeting still finishing on the engine


def _m():
    from . import main  # late import: main imports this module
    return main


class EngineHub:
    """Public-server side: the engine's connection and the meeting it is running for us."""

    def __init__(self) -> None:
        self.ws = None
        self.name = ""
        self.languages: list[str] = []
        self.busy = False
        self.compatible = True
        self.run_active = False
        self.stopped = False
        self.status: dict[str, Any] | None = None
        self._out: asyncio.Queue | None = None
        self._started: asyncio.Future | None = None
        self._resampler = None

    # ------------------------------------------------------------ state
    @property
    def connected(self) -> bool:
        return self.ws is not None

    def available(self) -> bool:
        return (self.connected and self.compatible and not self.busy and not self.run_active
                and _m().S.settings.engine.use)

    def info(self) -> dict[str, Any]:
        return {"connected": self.connected, "name": self.name, "busy": self.busy, "running": self.run_active,
                "use": _m().S.settings.engine.use, "compatible": self.compatible}

    # ------------------------------------------------------------ connection
    async def serve(self, ws) -> None:
        token = _m().S.settings.engine.token
        if not token or not hmac.compare_digest(ws.headers.get("authorization", ""), f"Bearer {token}"):
            await ws.close(code=4403)
            return
        await ws.accept()
        if self.ws is not None:  # the engine reconnected: the new link replaces the old one
            old = self.ws
            self._lost()
            try:
                await old.close()
            except Exception:
                pass
        self.ws = ws
        self._out = asyncio.Queue(maxsize=3000)
        sender = asyncio.create_task(self._sender(ws, self._out))
        try:
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    break
                if m.get("bytes") is not None:
                    self._mp3(m["bytes"])
                elif m.get("text"):
                    self._message(json.loads(m["text"]))
        except Exception as e:
            log.info("engine link closed: %s", e)
        finally:
            sender.cancel()
            if self.ws is ws:
                self._lost()

    async def _sender(self, ws, q: asyncio.Queue) -> None:
        try:
            while True:
                item = await q.get()
                if isinstance(item, bytes):
                    await ws.send_bytes(item)
                else:
                    await ws.send_text(json.dumps(item, ensure_ascii=False))
        except Exception:
            pass

    def _send(self, item) -> None:
        if self._out is not None:
            try:
                self._out.put_nowait(item)
            except asyncio.QueueFull:
                pass

    def _lost(self) -> None:
        M = _m()
        if self.connected:
            log.info("engine %s disconnected", self.name or "?")
            M.S.operator.publish({"type": "log", "level": "warn", "text": f"{self.name or 'The engine'} went offline."})
        self.ws = None
        self._out = None
        if self._started and not self._started.done():
            self._started.set_result({"type": "error", "message": "engine disconnected"})
        self._end_run()

    # ------------------------------------------------------------ from the engine
    def _message(self, m: dict[str, Any]) -> None:
        M = _m()
        t = m.get("type")
        if t == "hello":
            mine, theirs = M.S.settings.stream, m.get("stream") or {}
            self.compatible = (theirs.get("sample_rate") == mine.sample_rate
                               and theirs.get("bitrate_kbps") == mine.bitrate_kbps)
            self.name = str(m.get("name") or "engine")[:60]
            self.languages = list(m.get("languages") or [])
            self.busy = bool(m.get("busy"))
            if not self.compatible:
                log.warning("engine %s streams %s, this server %s/%s: not used", self.name, theirs,
                            mine.sample_rate, mine.bitrate_kbps)
            log.info("engine connected: %s (%s)", self.name, ", ".join(self.languages))
            M.S.operator.publish({"type": "log", "level": "info",
                                  "text": f"{self.name} connected: meetings run on it while it is online."})
        elif t == "busy":
            self.busy = bool(m.get("busy"))
        elif t in ("started", "error"):
            if self._started and not self._started.done():
                self._started.set_result(m)
        elif t == "hub" and self.run_active:
            ch = M.S.pipeline.channels.get(m.get("lang"))
            msg = m.get("msg") or {}
            kind = msg.get("type")
            if ch is None or kind in ("languages", "lang_state"):  # this server announces its own
                return
            if kind == "session":  # a new meeting: phones clear, and late joiners get this one
                ch.hub.history.clear()
                M.S.pipeline.session_id = msg.get("session") or M.S.pipeline.session_id
            ch.hub.publish(msg, remember=bool(m.get("remember")))
        elif t == "op":
            msg = m.get("msg") or {}
            if msg.get("type") == "status":
                self.status = msg
            elif msg.get("type") in ("chunk", "log") and self.run_active:
                M.S.operator.publish(msg)
        elif t == "done":
            self._end_run()

    def _mp3(self, data: bytes) -> None:
        if not self.run_active or len(data) < 2 or data[0] >= len(self.languages):
            return
        ch = _m().S.pipeline.channels.get(self.languages[data[0]])
        if ch is not None:
            ch.broadcaster.feed_mp3(data[1:])

    # ------------------------------------------------------------ meetings
    async def start_run(self, sample_rate: int, device: str, record: bool, languages: list[str]) -> bool:
        """Ask the engine to run a meeting; False if it can't (the caller then runs it here)."""
        import soxr

        M = _m()
        self._started = asyncio.get_running_loop().create_future()
        self._resampler = soxr.ResampleStream(sample_rate, PCM_RATE, 1, dtype="int16", quality="HQ")
        self.run_active, self.stopped, self.status = True, False, None
        self._send({"type": "start", "sample_rate": PCM_RATE, "languages": languages, "device": device,
                    "record": record})
        try:
            r = await asyncio.wait_for(self._started, 8)
        except asyncio.TimeoutError:
            r = {"type": "error", "message": "no answer"}
        if r.get("type") != "started" or not self.connected:
            log.warning("engine %s could not start: %s", self.name, r.get("message"))
            self._end_run()
            return False
        for ch in M.S.pipeline.channels.values():
            ch.broadcaster.external = True  # phones now hear the engine's stream
        log.info("meeting running on %s", self.name)
        M.S.operator.publish({"type": "log", "level": "info", "text": f"Meeting running on {self.name}."})
        return True

    def push(self, data: bytes) -> bool:
        """Speaker audio for the engine. False if the meeting is no longer running there."""
        if not self.run_active or not self.connected:
            return False
        out = self._resampler.resample_chunk(np.frombuffer(data, dtype=np.int16))
        if len(out):
            self._send(np.ascontiguousarray(out, dtype=np.int16).tobytes())
        return True

    def stop_run(self) -> None:
        """End the meeting; it finishes the last sentences on the engine, then sends "done"."""
        if not self.run_active or self.stopped:
            return
        self.stopped = True
        self._send({"type": "stop"})
        asyncio.get_running_loop().call_later(DONE_TIMEOUT_S, self._stop_timeout)

    def _stop_timeout(self) -> None:
        if self.run_active and self.stopped:
            self._end_run()

    def set_languages(self, codes: list[str]) -> None:
        if self.run_active:
            self._send({"type": "languages", "languages": codes})

    def _end_run(self) -> None:
        if not self.run_active:
            return
        M = _m()
        self.run_active = False
        self.status = None
        for ch in M.S.pipeline.channels.values():
            ch.broadcaster.external = False
        log.info("meeting on %s ended", self.name or "engine")


class EngineClient:
    """Engine side (e.g. the Mac): keep a link to the public server and run the meetings it hands over."""

    def __init__(self, url: str, token: str, name: str) -> None:
        self.url, self.token, self.name = url, token, name
        self.connected = False
        self.running = False
        self.last_error = ""
        self._src = None
        self._prev_langs: list[str] | None = None
        self._out: asyncio.Queue | None = None
        self._taps: list[tuple[list, Any]] = []

    def info(self) -> dict[str, Any]:
        return {"hub": urlparse(self.url).hostname or self.url, "connected": self.connected,
                "running": self.running, "error": "" if self.connected else self.last_error}

    async def run_forever(self) -> None:
        import websockets

        self._stay_awake()
        delay = 2.0
        while True:
            try:
                async with websockets.connect(self.url, additional_headers={"Authorization": f"Bearer {self.token}"},
                                              user_agent_header="church-translate engine", max_size=2 ** 22,
                                              ping_interval=20, ping_timeout=30, open_timeout=15) as ws:
                    delay = 2.0
                    self.connected, self.last_error = True, ""
                    log.info("linked to %s", self.url)
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)[:200]
            finally:
                if self.connected:
                    log.info("link to %s closed", self.url)
                self.connected = False
                self._finish(lost=True)
            await asyncio.sleep(delay)
            delay = min(60.0, delay * 2)

    def _stay_awake(self) -> None:
        """A Mac serving as engine must not fall asleep while plugged in (lid open). In the Hear Church app
        the Mac only stays awake while a meeting runs (the app and the pipeline handle that)."""
        if sys.platform != "darwin" or _m().S.app_mode:
            return
        pid = os.getpid()
        if subprocess.run(["pgrep", "-f", f"caffeinate -is -w {pid}"], capture_output=True).returncode != 0:
            subprocess.Popen(["caffeinate", "-is", "-w", str(pid)])

    async def _session(self, ws) -> None:
        M = _m()
        self._out = asyncio.Queue(maxsize=3000)
        sender = asyncio.create_task(self._sender(ws, self._out))
        watcher = asyncio.create_task(self._watch())
        st = M.S.settings.stream
        self._send({"type": "hello", "name": self.name, "languages": list(M.S.pipeline.channels),
                    "stream": {"sample_rate": st.sample_rate, "bitrate_kbps": st.bitrate_kbps},
                    "busy": M.S.pipeline.capturing})
        try:
            async for m in ws:
                if isinstance(m, bytes):
                    if self._src is not None:
                        self._src.push(m)
                    continue
                msg = json.loads(m)
                t = msg.get("type")
                if t == "start":
                    self._start(msg)
                elif t == "stop" and self._src is not None:
                    self._src.stop()
                elif t == "languages" and self.running:
                    M.set_languages(list(msg.get("languages") or []), persist=False)
        finally:
            sender.cancel()
            watcher.cancel()
            self._out = None

    async def _sender(self, ws, q: asyncio.Queue) -> None:
        try:
            while True:
                item = await q.get()
                await ws.send(item if isinstance(item, bytes) else json.dumps(item, ensure_ascii=False))
        except Exception:
            pass

    def _send(self, item) -> None:
        if self._out is not None:
            try:
                self._out.put_nowait(item)
            except asyncio.QueueFull:
                pass  # a slow link loses some audio rather than everything

    def _start(self, msg: dict[str, Any]) -> None:
        from .audio_in import BrowserSource

        M = _m()
        if self.running or M.S.pipeline.capturing:
            self._send({"type": "error", "message": "busy"})
            return
        host = urlparse(self.url).hostname or "the public server"
        src = BrowserSource(int(msg.get("sample_rate") or PCM_RATE),
                            label=f"speaker on {host} ({msg.get('device') or 'phone'})")
        self._prev_langs = list(M.S.pipeline.on_channels)
        M.set_languages(list(msg.get("languages") or self._prev_langs), persist=False)
        self._attach()
        try:
            M.S.pipeline.start("browser", source=src, record=bool(msg.get("record")))
        except Exception as e:
            self._detach()
            M.set_languages(self._prev_langs, persist=False)
            self._prev_langs = None
            self._send({"type": "error", "message": str(e)})
            return
        self._src, self.running = src, True
        self._send({"type": "started"})
        log.info("running a meeting for %s", host)

    def _attach(self) -> None:
        M = _m()
        for i, (code, ch) in enumerate(M.S.pipeline.channels.items()):
            def hub_tap(msg, remember, code=code):
                self._send({"type": "hub", "lang": code, "msg": msg, "remember": remember})

            def mp3_tap(data, i=i):
                self._send(bytes([i]) + data)
            self._tap(ch.hub.taps, hub_tap)
            self._tap(ch.broadcaster.taps, mp3_tap)

        def op_tap(msg, remember):
            if msg.get("type") in ("status", "chunk", "log"):
                self._send({"type": "op", "msg": msg})
        self._tap(M.S.operator.taps, op_tap)

    def _tap(self, taps: list, fn) -> None:
        taps.append(fn)
        self._taps.append((taps, fn))

    def _detach(self) -> None:
        for taps, fn in self._taps:
            if fn in taps:
                taps.remove(fn)
        self._taps.clear()

    async def _watch(self) -> None:
        """Tell the hub when this machine is busy with its own meeting; notice a handed-over meeting ending."""
        M = _m()
        busy_sent = None
        while True:
            await asyncio.sleep(0.5)
            busy = M.S.pipeline.capturing and not self.running
            if busy != busy_sent:
                self._send({"type": "busy", "busy": busy})
                busy_sent = busy
            if self.running and not M.S.pipeline.running:
                self._finish(lost=False)

    def _finish(self, lost: bool) -> None:
        if not self.running:
            return
        M = _m()
        if lost and self._src is not None:
            self._src.stop()  # the public server is gone: end the meeting here too
        self.running, self._src = False, None
        self._detach()
        if self._prev_langs is not None:
            M.set_languages(self._prev_langs, persist=False)
            self._prev_langs = None
        if not lost:
            self._send({"type": "done"})
        log.info("meeting for %s %s", urlparse(self.url).hostname, "cut off" if lost else "finished")



class PublishClient(EngineClient):
    """Engine side of Hear Church: publish this computer's own locally started meetings to the hub as the
    congregation's live session. The hub never sends audio here; it only relays what this computer makes."""

    def __init__(self, url: str, token: str, name: str) -> None:
        super().__init__(url, token, name)
        self.announced = False            # session_start sent on this link and not stopped yet
        self.session_code = ""            # the hub's code for the running session, for resuming after a drop
        self.refused_run = 0.0            # started_at of a meeting the hub refused (not asked again)
        self._offered: list[str] = []
        self.notice = ""                  # the hub's last refusal / ending of our session, for the operator page
        self.listeners = {"stream": 0, "captions": 0}  # phones on the hub, as it reports them
        self.heard: set[str] = set()      # languages with a phone on them, as the hub reports them: only these are billed
        self.meter: dict[str, dict[int, dict]] = {}   # session code → seq → speech not yet acknowledged by the hub

    # Metering (credits): seconds of speech per chunk, in the languages someone listens to, are kept here and sent
    # every METER_EVERY_S until the hub acknowledges them (so a dropped link only delays them; the hub stores each
    # chunk once). At most METER_KEEP are kept: a hub that never answers can't grow the Mac's memory.
    METER_EVERY_S = 15.0
    METER_BATCH = 200
    METER_KEEP = 20000

    def info(self) -> dict[str, Any]:
        return {**super().info(), "mode": "publish", "live": self.running, "notice": self.notice,
                "listeners": self.listeners if self.running else {"stream": 0, "captions": 0}}

    async def _session(self, ws) -> None:
        M = _m()
        self._out = asyncio.Queue(maxsize=3000)
        sender = asyncio.create_task(self._sender(ws, self._out))
        st = M.S.settings.stream
        self._send({"type": "hello", "name": self.name, "languages": list(M.S.pipeline.channels),
                    "stream": {"sample_rate": st.sample_rate, "bitrate_kbps": st.bitrate_kbps}, "busy": False})
        self._attach()
        watcher = asyncio.create_task(self._publish_watch())
        try:
            async for m in ws:
                if isinstance(m, bytes):
                    continue
                msg = json.loads(m)
                if msg.get("type") == "session_started":
                    self.session_code, self.running, self.notice = msg.get("session") or "", True, ""
                    log.info("live on %s%s", urlparse(self.url).hostname, " (resumed)" if msg.get("resumed") else "")
                elif msg.get("type") == "session_refused":
                    self.announced, self.running = False, False
                    self.refused_run, self.notice = _m().S.pipeline.started_at, str(msg.get("message") or "refused")
                    log.warning("hub refused the meeting: %s", self.notice)
                elif msg.get("type") == "listeners":
                    self.listeners = {"stream": int(msg.get("stream") or 0), "captions": int(msg.get("captions") or 0)}
                    if isinstance(msg.get("heard"), list):
                        self.heard = {c for c in msg["heard"] if isinstance(c, str)}
                elif msg.get("type") == "meter_ok":
                    self._metered(msg)
                elif msg.get("type") == "session_ended":  # the hub closed it (e.g. hidden): stop sending, don't re-announce
                    self.announced, self.running, self.session_code = False, False, ""
                    self.refused_run, self.notice = _m().S.pipeline.started_at, str(msg.get("message") or "ended by the server")
                    log.warning("hub ended the session: %s", self.notice)
        finally:
            sender.cancel()
            watcher.cancel()
            self._out = None

    def _attach(self) -> None:
        """Relay everything while a session is announced; the hub ignores it otherwise."""
        M = _m()
        for i, (code, ch) in enumerate(M.S.pipeline.channels.items()):
            def hub_tap(msg, remember, code=code):
                if self.announced:
                    self._send({"type": "hub", "lang": code, "msg": msg, "remember": remember})

            def mp3_tap(data, i=i):
                if self.announced:
                    self._send(bytes([i]) + data)
            self._tap(ch.hub.taps, hub_tap)
            self._tap(ch.broadcaster.taps, mp3_tap)
        taps = getattr(M.S.pipeline, "speech_taps", None)
        if taps is not None:
            self._tap(taps, self._speech)

    def _speech(self, seq: int, seconds: float, langs: list[str]) -> None:
        """A chunk of speech went out in `langs`: keep it for the hub's meter if someone hears it in this session."""
        heard = sorted(c for c in langs if c in self.heard)
        if not (self.running and self.session_code and seconds > 0 and heard):
            return
        self.meter.setdefault(self.session_code, {})[seq] = {"seq": seq, "s": round(float(seconds), 2), "langs": heard}
        extra = sum(len(v) for v in self.meter.values()) - self.METER_KEEP
        for code in list(self.meter):
            while extra > 0 and self.meter[code]:
                del self.meter[code][next(iter(self.meter[code]))]
                extra -= 1
            if not self.meter[code]:
                del self.meter[code]

    def _send_meter(self) -> None:
        for code, items in list(self.meter.items()):
            pending = list(items.values())
            for i in range(0, len(pending), self.METER_BATCH):
                self._send({"type": "meter", "session": code, "items": pending[i:i + self.METER_BATCH]})

    def _metered(self, msg: dict[str, Any]) -> None:
        code = msg.get("session")
        items = self.meter.get(code)
        if items is None:
            return
        if msg.get("unknown"):
            items.clear()   # the hub has no such session (another hub, a lost database): nothing can be billed for it
        for seq in msg.get("seqs") or []:
            items.pop(seq, None)
        if not items:
            del self.meter[code]

    async def _publish_watch(self) -> None:
        """Announce a meeting started on this computer, follow its languages, and stop it when it is over."""
        M = _m()
        p = M.S.pipeline
        metered = time.monotonic()
        while True:
            await asyncio.sleep(0.5)
            if self.meter and time.monotonic() - metered >= self.METER_EVERY_S:
                metered = time.monotonic()
                self._send_meter()
            local = M.S.app_mode and M.S.settings.server.sharing == "local"   # this Wi-Fi only: not on the hub
            if local and self.announced:
                self._send({"type": "session_stop"})
                self.announced, self.running, self.session_code = False, False, ""
            elif p.running and not local and not self.announced and p.started_at != self.refused_run:
                self._offered = list(p.on_channels)
                self.announced = True
                self._send({"type": "session_start", "languages": self._offered, "resume": self.session_code,
                            "speaker": getattr(p, "speaker", "en")})
            elif self.announced and not p.running:
                self._send({"type": "session_stop"})
                self.announced, self.running, self.session_code = False, False, ""
                log.info("meeting over: session stopped on %s", urlparse(self.url).hostname)
            elif self.announced and list(p.on_channels) != self._offered:
                self._offered = list(p.on_channels)
                self._send({"type": "languages", "languages": self._offered})

    def _finish(self, lost: bool) -> None:
        """The link dropped: stop relaying, keep the session code so the reconnect can resume the session. Unsent
        meter reports stay and go out on the next link."""
        self.announced, self.running, self.heard = False, False, set()
        self._detach()
        if not _m().S.pipeline.running:
            self.session_code = ""
