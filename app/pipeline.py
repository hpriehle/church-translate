"""Orchestrates: capture → VAD chunks → STT → translation → TTS → mixer/stream, plus captions.

Each stage is one asyncio task working through its queue in order, so chunk N+1 can be
transcribed while chunk N is being translated, but captions/audio always stay in order.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .audio_in import CaptureWorker, FileSource, MicSource, Source
from .backlog import BacklogPolicy
from .broadcast.hub import Hub
from .broadcast.mixer import Clip, Mixer
from .broadcast.stream import Mp3Broadcaster, encode_clip
from .chunker import Chunk, Chunker, SileroVad
from .config import LOG_DIR, Settings
from .metrics import ChunkRecord, MetricsLog
from .stt import Transcriber, build_prompt
from .translate.prompts import whisper_hints
from .translate.router import Translator
from .tts.engines import TtsRunner
from .tts.speech_fr import speechify

log = logging.getLogger("pipeline")


class Pipeline:
    def __init__(self, settings: Settings, glossary: dict[str, Any], listen: Hub, operator: Hub,
                 mixer: Mixer, broadcaster: Mp3Broadcaster, translator: Translator,
                 stt: Transcriber, tts: TtsRunner):
        self.settings = settings
        self.glossary = glossary
        self.listen = listen
        self.operator = operator
        self.mixer = mixer
        self.broadcaster = broadcaster
        self.translator = translator
        self.stt = stt
        self.tts = tts
        self.backlog = BacklogPolicy(settings.backlog)
        self.hints = whisper_hints(glossary)
        self.vad = SileroVad()

        self.chunk_q: asyncio.Queue[Chunk] = asyncio.Queue()
        self.mt_q: asyncio.Queue[ChunkRecord] = asyncio.Queue()
        self.tts_q: asyncio.Queue[ChunkRecord] = asyncio.Queue()
        self.records: dict[int, ChunkRecord] = {}
        self.clips: OrderedDict[int, bytes] = OrderedDict()
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
        self.recording = None
        self.prev_en = ""
        self.avg_clip_s = 4.0
        self._seq = 0
        self._caption_no = 0
        self._tasks: list[asyncio.Task] = []
        self.last_error = ""
        self.last_report: Path | None = None
        self._caff = None
        self._usage_at_start: dict[str, float] = {}
        self.on_drained: Callable[[], None] | None = None

    # ------------------------------------------------------------ lifecycle
    def start_tasks(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.mixer.on_air = self._on_air
        self._tasks = [asyncio.create_task(self._stt_stage()), asyncio.create_task(self._mt_stage()),
                       asyncio.create_task(self._tts_stage())]

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
        if record:
            self.recording = self.broadcaster.start_recording(self.metrics.path.with_name(self.metrics.path.stem + "-fr.mp3"))
        self.backlog = BacklogPolicy(self.settings.backlog)
        u = self.translator.usage()
        self._usage_at_start = {k: getattr(u, k) for k in ("input", "cache_read", "cache_write", "output", "cost_usd", "turns")}
        self.worker = CaptureWorker(source, chunker, a.input_gain, self._on_chunk_thread,
                                    self._on_level_thread, self._on_end_thread)
        self.capturing = True
        self.running = True
        self.source_label = source.label
        self.started_at = time.time()
        self.last_error = ""
        self.worker.start()
        self._hold_awake()
        self.translator.prepare_fallback()
        log.info("started: %s", source.label)
        self.listen.publish({"type": "status", "live": True})
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
        # Let in-flight chunks finish, then mark the run as stopped.
        while (not self.chunk_q.empty() or not self.mt_q.empty() or not self.tts_q.empty()
               or any(r.status in ("pending", "captioned") for r in self.records.values())
               or self.mixer.backlog_s > 0.05):
            if self.capturing:
                return
            await asyncio.sleep(0.25)
        if self.capturing:
            return
        self.running = False
        self.release_awake()
        self.listen.publish({"type": "status", "live": False})
        if self.recording:
            await asyncio.sleep(1.0)  # tail of the last clip
            self.broadcaster.stop_recording(self.recording)
            self.recording = None
        if self.metrics:
            try:
                self._write_report()
            except Exception:  # pragma: no cover
                log.exception("report failed")
        log.info("run finished; metrics in %s", self.metrics.path if self.metrics else "-")
        if self.on_drained:
            self.on_drained()

    # ------------------------------------------------------------ stages
    def total_backlog(self) -> float:
        waiting = self.mt_q.qsize() + self.tts_q.qsize()
        return self.mixer.backlog_s + waiting * self.avg_clip_s

    def _publish_rec(self, rec: ChunkRecord) -> None:
        self.operator.publish({"type": "chunk", **rec.public()})

    def _finalize(self, rec: ChunkRecord) -> None:
        self._publish_rec(rec)
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
            self._publish_rec(rec)
            await self.mt_q.put(rec)

    async def _mt_stage(self) -> None:
        while True:
            rec = await self.mt_q.get()
            # Falling behind? Translate the waiting chunks together in one request.
            merged: list[ChunkRecord] = []
            while not self.mt_q.empty() and len(merged) < 3:
                merged.append(self.mt_q.get_nowait())
            if merged:
                rec.en = " ".join([rec.en] + [m.en for m in merged])
                rec.merged = [m.seq for m in merged]
                rec.t_speech_end = merged[-1].t_speech_end
                rec.audio_dur_s = round(rec.audio_dur_s + sum(m.audio_dur_s for m in merged), 2)
                for m in merged:
                    m.status, m.note = "merged", f"merged into #{rec.seq}"
                    self._finalize(m)
            rec.condensed = self.backlog.update_condense(self.total_backlog())
            try:
                res = await self.translator.translate(rec.en, rec.condensed)
            except Exception as e:  # pragma: no cover
                log.exception("translate crashed")
                rec.status, rec.note = "failed", f"translate: {e}"
                self._finalize(rec)
                continue
            rec.mt_ms = round(res.ms, 1)
            rec.engine = res.engine
            rec.note = res.note
            if not res.text:
                rec.status = "failed" if res.engine == "none" else "skipped"
                self._finalize(rec)
                continue
            rec.fr = res.text
            rec.t_caption = time.monotonic()
            rec.status = "captioned"
            self._caption_no += 1
            rec.caption_no = self._caption_no
            self.listen.publish({"type": "caption", "seq": rec.caption_no, "fr": rec.fr, "ts": time.time()}, remember=True)
            self._publish_rec(rec)
            await self.tts_q.put(rec)

    async def _tts_stage(self) -> None:
        sr = self.mixer.sr
        while True:
            rec = await self.tts_q.get()
            speed = self.backlog.speed_for(self.total_backlog())
            try:
                pcm, ms = await self.tts.synth(speechify(rec.fr), speed)
            except Exception as e:
                log.warning("tts failed: %s", e)
                rec.status, rec.note = "failed", (rec.note + f" tts: {e}").strip()
                self._finalize(rec)
                continue
            rec.tts_ms = round(ms, 1)
            rec.speed = speed
            rec.t_audio_ready = time.monotonic()
            clip = Clip(rec.seq, pcm)
            dropped = self.mixer.enqueue(clip, self.backlog.drop_above_s, self.backlog.drop_to_s)
            rec.clip_s = round(clip.duration(sr), 2)
            self.avg_clip_s = 0.8 * self.avg_clip_s + 0.2 * rec.clip_s
            rec.backlog_at_enqueue = round(self.mixer.backlog_s, 2)
            self._publish_rec(rec)
            for d in dropped:
                drec = self.records.get(d.seq)
                if drec:
                    drec.status, drec.note = "dropped", "audio dropped (more than %.0fs behind)" % self.backlog.drop_above_s
                    self._finalize(drec)
            # Clip-by-clip fallback mode for phones that use it.
            mp3 = await asyncio.to_thread(encode_clip, clip.pcm, sr, self.settings.stream.bitrate_kbps)
            self.clips[rec.seq] = mp3
            while len(self.clips) > 80:
                self.clips.popitem(last=False)
            self.listen.publish({"type": "clip", "seq": rec.caption_no, "url": f"/clips/{rec.seq}.mp3"})

    def _on_air(self, clip: Clip, t: float) -> None:
        rec = self.records.get(clip.seq)
        if rec:
            self.listen.publish({"type": "onair", "seq": rec.caption_no})
            rec.t_on_air = t
            rec.status = "aired"
            self._finalize(rec)

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
        u = self.translator.usage()
        u0 = self._usage_at_start
        usage = {k: getattr(u, k) - u0.get(k, 0) for k in ("input", "cache_read", "cache_write", "output", "cost_usd", "turns")}
        stt_model = st.stt.model if st.stt.engine == "mlx" else st.stt.faster_whisper_model
        return {
            "source": self.source_label,
            "stt": f"{st.stt.engine} {stt_model}",
            "translator": (f"mode={st.translate.mode}, Claude {st.translate.claude_model} (effort {st.translate.claude_effort}, "
                           f"fallback after {st.translate.claude_timeout_s:g}s), Ollama {st.translate.ollama_model}"),
            "tts": f"{st.tts.engine} voice={self.tts.voice} rate={st.tts.rate}",
            "usage": usage,
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

    async def say_test(self, text: str) -> None:
        pcm, _ = await self.tts.synth(speechify(text), 1.0)
        self.mixer.enqueue(Clip(0, pcm))

    def apply_settings(self, settings: Settings, glossary: dict[str, Any]) -> None:
        old = self.settings
        self.settings = settings
        self.glossary = glossary
        self.hints = whisper_hints(glossary)
        self.backlog.cfg = settings.backlog
        self.mixer.volume = settings.tts.volume
        if settings.tts.model_dump() != old.tts.model_dump():
            self.tts.reload(settings.tts)
        self.translator.reload(settings.translate, glossary)

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "capturing": self.capturing,
            "source": self.source_label,
            "started_at": self.started_at,
            "level_db": round(self.level_db, 1),
            "backlog_s": round(self.mixer.backlog_s, 1),
            "pipeline_backlog_s": round(self.total_backlog(), 1),
            "queues": {"stt": self.chunk_q.qsize(), "translate": self.mt_q.qsize(), "tts": self.tts_q.qsize()},
            "speed": self.backlog.speed_for(self.total_backlog()),
            "condense": self.backlog.condense,
            "stt_ready": self.stt.ready,
            "stt_engine": self.stt.engine,
            "tts": {"engine": self.settings.tts.engine, "voice": self.tts.voice, "error": self.tts.error},
            "metrics_file": str(self.metrics.path.name) if self.metrics else "",
            "recording": bool(self.recording),
            "last_error": self.last_error,
        }
