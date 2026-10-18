"""Orchestrates: capture → VAD chunks → speech-to-text (once) → one Channel per output language.

Each Channel (see channel.py) translates, voices and streams its language on its own, in order.
A chunk's record is complete when every language has finished with it (aired, dropped, skipped…).
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .audio_in import CaptureWorker, FileSource, MicSource, Source
from .backlog import BacklogPolicy
from .channel import Channel
from .chunker import Chunk, Chunker, SileroVad
from .config import LOG_DIR, Settings, tts_for
from .metrics import ChunkRecord, LangResult, MetricsLog
from .stt import Transcriber, build_prompt, resolve_engine
from .translate.prompts import whisper_hints

log = logging.getLogger("pipeline")


class Pipeline:
    def __init__(self, settings: Settings, glossaries: dict[str, dict[str, Any]], operator, stt: Transcriber):
        self.settings = settings
        self.glossaries = glossaries
        self.operator = operator
        self.stt = stt
        self.channels: dict[str, Channel] = {}
        self.hints = self._hints()
        self.vad = SileroVad()

        self.chunk_q: asyncio.Queue[Chunk] = asyncio.Queue()
        self.records: dict[int, ChunkRecord] = {}
        self.stt_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self.stt_ready = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None

        self.worker: CaptureWorker | None = None
        self.running = False
        self.capturing = False
        self.source_label = ""
        self.started_at = 0.0
        self.level_db = -90.0
        self.metrics: MetricsLog | None = None
        self.prev_en = ""
        self._seq = 0
        self.session_id: str | None = None  # one per run; listeners only ever see the current one
        self._tasks: list[asyncio.Task] = []
        self.last_error = ""
        self.last_report: Path | None = None
        self._caff = None
        self._usage_at_start: dict[str, dict[str, float]] = {}
        self.on_drained: Callable[[], None] | None = None

    def add_channel(self, ch: Channel) -> None:
        self.channels[ch.code] = ch

    def _hints(self) -> list[str]:
        hints: list[str] = []
        for g in self.glossaries.values():
            for h in whisper_hints(g):
                if h not in hints:
                    hints.append(h)
        return hints

    # ------------------------------------------------------------ lifecycle
    def start_tasks(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._tasks = [asyncio.create_task(self._stt_stage())]
        for ch in self.channels.values():
            ch.start_tasks()

    async def load_stt(self) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(self.stt_exec, self.stt.load)
            self.stt_ready.set()
        except Exception as e:
            self.last_error = f"Speech recognition failed to load: {e}"
            log.exception("STT load failed")

    def start(self, kind: str, device: str | None = None, file: str | Path | None = None,
              speed: float = 1.0, start_s: float = 0.0, max_s: float | None = None,
              record: bool = False, source: Source | None = None) -> None:
        if self.capturing:
            raise RuntimeError("already running")
        a = self.settings.audio
        if source is not None:
            pass
        elif kind == "mic":
            source = MicSource(device if device is not None else a.input_device, a.input_channel)
        elif kind == "file":
            if not file:
                raise ValueError("no test file chosen")
            source = FileSource(file, speed=speed, start_s=start_s, max_s=max_s)
        else:
            raise ValueError(f"unknown source {kind!r}")
        self.vad.reset()
        chunker = Chunker(self.settings.vad, self.vad)
        if self.metrics:
            self.metrics.close()
        self.metrics = MetricsLog(LOG_DIR, "session")
        for ch in self.channels.values():
            ch.backlog = BacklogPolicy(self.settings.backlog)
            if record:
                ch.recording = ch.broadcaster.start_recording(
                    self.metrics.path.with_name(f"{self.metrics.path.stem}-{ch.code}.mp3"))
            u = ch.translator.usage()
            self._usage_at_start[ch.code] = {k: getattr(u, k) for k in ("input", "cache_read", "cache_write", "output", "cost_usd", "turns")}
        self.worker = CaptureWorker(source, chunker, a.input_gain, self._on_chunk_thread,
                                    self._on_level_thread, self._on_end_thread)
        self.capturing = True
        self.running = True
        self.source_label = source.label
        self.started_at = time.time()
        self.last_error = ""
        self.worker.start()
        self._hold_awake()
        for ch in self.channels.values():
            ch.translator.prepare_fallback()
        log.info("started: %s → %s", source.label, ", ".join(self.channels))
        # New session: phones clear what they show, and nobody is sent captions from earlier runs.
        self.session_id = time.strftime("%Y%m%d-%H%M%S")
        for ch in self.channels.values():
            ch.hub.history.clear()
            ch.hub.publish({"type": "session", "session": self.session_id, "live": True})
        self.operator.publish({"type": "log", "level": "info", "text": f"Started — {source.label}"})

    def stop(self) -> None:
        if self.worker:
            self.worker.stop()
        self.operator.publish({"type": "log", "level": "info", "text": "Stop requested — finishing what was already said"})

    # ------------------------------------------------------------ capture thread callbacks
    def _on_chunk_thread(self, chunk: Chunk) -> None:
        self.loop.call_soon_threadsafe(self.chunk_q.put_nowait, chunk)

    def _on_level_thread(self, db: float) -> None:
        self.level_db = db

    def _on_end_thread(self, err: str | None) -> None:
        self.loop.call_soon_threadsafe(self._capture_ended, err)

    def _capture_ended(self, err: str | None) -> None:
        self.capturing = False
        self.worker = None
        if err:
            self.last_error = f"Capture: {err}"
            self.operator.publish({"type": "log", "level": "error", "text": self.last_error})
        self.operator.publish({"type": "log", "level": "info", "text": "Capture ended"})
        asyncio.create_task(self._wait_drained())

    async def _wait_drained(self) -> None:
        # Let in-flight chunks finish in every language, then mark the run as stopped.
        while not self.chunk_q.empty() or self.records or not all(ch.idle for ch in self.channels.values()):
            if self.capturing:
                return
            await asyncio.sleep(0.25)
        if self.capturing:
            return
        self.running = False
        self.release_awake()
        # Session over: phones that are open keep their text, but nobody who opens the page later gets it.
        self.session_id = None
        for ch in self.channels.values():
            ch.hub.history.clear()
            ch.hub.publish({"type": "status", "live": False})
        if any(ch.recording for ch in self.channels.values()):
            await asyncio.sleep(1.0)  # tail of the last clip
            for ch in self.channels.values():
                if ch.recording:
                    ch.broadcaster.stop_recording(ch.recording)
                    ch.recording = None
        if self.metrics:
            try:
                self._write_report()
            except Exception:  # pragma: no cover
                log.exception("report failed")
        log.info("run finished; metrics in %s", self.metrics.path if self.metrics else "-")
        if self.on_drained:
            self.on_drained()

    # ------------------------------------------------------------ records
    def publish_rec(self, rec: ChunkRecord) -> None:
        self.operator.publish({"type": "chunk", **rec.public()})

    def lang_done(self, rec: ChunkRecord) -> None:
        """A language finished with this chunk (aired, dropped, skipped…); finalize once all have."""
        if rec.complete:
            self._finalize(rec)
        else:
            self.publish_rec(rec)

    def _finalize(self, rec: ChunkRecord) -> None:
        if rec.status == "pending":
            rec.status = "done"
        self.publish_rec(rec)
        if self.metrics:
            self.metrics.write(rec)
        self.records.pop(rec.seq, None)

    async def _stt_stage(self) -> None:
        while True:
            chunk = await self.chunk_q.get()
            self._seq += 1
            rec = ChunkRecord(seq=self._seq, audio_start_s=round(chunk.start_s, 2), audio_dur_s=round(chunk.duration, 2),
                              voiced_s=round(chunk.voiced_s, 2), cut_reason=chunk.reason,
                              t_speech_start=chunk.t_speech_start, t_speech_end=chunk.t_speech_end, t_cut=chunk.t_cut)
            self.records[rec.seq] = rec
            try:
                await self.stt_ready.wait()
                prompt = build_prompt(self.hints, self.prev_en)
                res = await self.loop.run_in_executor(self.stt_exec, self.stt.transcribe, chunk.audio, prompt)
            except Exception as e:
                log.exception("stt failed")
                rec.status, rec.note = "failed", f"stt: {e}"
                self._finalize(rec)
                continue
            rec.stt_ms = round(res.ms, 1)
            rec.t_stt_done = time.monotonic()
            if not res.text:
                rec.status, rec.note = "skipped", res.dropped_reason
                self._finalize(rec)
                continue
            rec.en = res.text
            self.prev_en = res.text
            rec.out = {code: LangResult() for code in self.channels}
            self.publish_rec(rec)
            for ch in self.channels.values():
                ch.mt_q.put_nowait(rec)

    # ------------------------------------------------------------ misc
    def _hold_awake(self) -> None:
        """Keep the Mac from idle-sleeping while a run is active (not while the server just waits)."""
        import os
        import shutil
        import subprocess
        import sys

        if self._caff is None and sys.platform == "darwin" and shutil.which("caffeinate"):
            self._caff = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])

    def release_awake(self) -> None:
        if self._caff is not None:
            self._caff.terminate()
            self._caff = None

    def _run_info(self) -> dict[str, Any]:
        st = self.settings
        stt_engine = resolve_engine(st.stt.engine)
        stt_model = st.stt.model if stt_engine == "mlx" else st.stt.faster_whisper_model
        langs = {}
        for code, ch in self.channels.items():
            u = ch.translator.usage()
            u0 = self._usage_at_start.get(code, {})
            usage = {k: getattr(u, k) - u0.get(k, 0) for k in ("input", "cache_read", "cache_write", "output", "cost_usd", "turns")}
            langs[code] = {"name": ch.lang.name, "tts": f"{ch.tts.cfg.engine} voice={ch.tts.voice}",
                           "fallback": ch.lang.fallback, "usage": usage}
        return {
            "source": self.source_label,
            "stt": f"{stt_engine} {stt_model}",
            "translator": (f"Claude {st.translate.claude_model} (effort {st.translate.claude_effort}), French fallback "
                           f"after {st.translate.claude_timeout_s:g}s to Ollama {st.translate.ollama_model}"),
            "languages": langs,
        }

    def _write_report(self) -> None:
        import json

        from .report import build_report

        info = self._run_info()
        base = self.metrics.path
        base.with_name(base.stem + "-info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False))
        records = sorted(self.metrics.records, key=lambda r: r["seq"])
        out = base.with_name(base.stem + "-report.md")
        out.write_text(build_report(records, info))
        self.last_report = out
        self.operator.publish({"type": "log", "level": "info", "text": f"Report written: logs/{out.name}"})

    async def say_test(self) -> None:
        await asyncio.gather(*(ch.say_test() for ch in self.channels.values()), return_exceptions=True)

    def apply_settings(self, settings: Settings, glossaries: dict[str, dict[str, Any]]) -> None:
        self.settings = settings
        self.glossaries = glossaries
        self.hints = self._hints()
        for code, ch in self.channels.items():
            ch.backlog.cfg = settings.backlog
            ch.mixer.max_age_s = settings.backlog.drop_above_s + 5
            ch.mixer.volume = settings.tts.volume
            new_tts = tts_for(settings, code)
            if new_tts.model_dump() != ch.tts.cfg.model_dump():
                ch.tts.reload(new_tts)
            ch.translator.reload(settings.translate, glossaries.get(code, {}))

    def status(self) -> dict[str, Any]:
        chans = {code: ch.status() for code, ch in self.channels.items()}
        return {
            "running": self.running,
            "capturing": self.capturing,
            "source": self.source_label,
            "started_at": self.started_at,
            "level_db": round(self.level_db, 1),
            "backlog_s": max((c["backlog_s"] for c in chans.values()), default=0.0),
            "queues": {"stt": self.chunk_q.qsize()},
            "stt_ready": self.stt.ready,
            "stt_engine": self.stt.engine,
            "channels": chans,
            "metrics_file": str(self.metrics.path.name) if self.metrics else "",
            "last_error": self.last_error,
        }
