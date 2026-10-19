"""One output language: translator → voice → its own mixer/MP3 stream, plus its caption feed.

Every channel receives the same English chunks from the shared speech-recognition stage and
works through them in order, independently of the other languages.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from .backlog import BacklogPolicy
from .broadcast.hub import Hub
from .broadcast.mixer import Clip, Mixer
from .broadcast.stream import Mp3Broadcaster, encode_clip
from .languages import Language
from .metrics import ChunkRecord
from .translate.router import Translator
from .tts.engines import TtsRunner
from .tts.normalize import speechify

if TYPE_CHECKING:  # pragma: no cover
    from .pipeline import Pipeline

log = logging.getLogger("channel")


class Channel:
    def __init__(self, pipeline: "Pipeline", lang: Language, translator: Translator, tts: TtsRunner,
                 mixer: Mixer, broadcaster: Mp3Broadcaster, hub: Hub):
        self.p = pipeline
        self.lang = lang
        self.code = lang.code
        self.translator = translator
        self.tts = tts
        self.mixer = mixer
        self.broadcaster = broadcaster
        self.hub = hub
        self.backlog = BacklogPolicy(pipeline.settings.backlog)
        self.mt_q: asyncio.Queue[ChunkRecord] = asyncio.Queue()
        self.tts_q: asyncio.Queue[ChunkRecord] = asyncio.Queue()
        self.clips: OrderedDict[int, bytes] = OrderedDict()
        self.caption_no = 0
        self.avg_clip_s = 4.0
        self.avg_tts_s = 0.5
        self.recording = None
        self.on = True           # translating into this language (switched live on the operator page)
        self._started = False
        self._tasks: list[asyncio.Task] = []
        mixer.on_air = self._on_air
        mixer.on_skip = self._on_skip
        mixer.max_age_s = pipeline.settings.backlog.drop_above_s + 5

    def start_tasks(self) -> None:
        self._tasks += [asyncio.create_task(self._mt_stage()), asyncio.create_task(self._tts_stage())]

    def activate(self) -> None:
        """Start this language's Claude session and load its voice, the first time it is switched on."""
        if self._started:
            return
        self._started = True
        self._tasks += [asyncio.create_task(self.translator.start()), asyncio.create_task(self.tts.load())]

    # ------------------------------------------------------------ state
    def total_backlog(self) -> float:
        return self.mixer.backlog_s + (self.mt_q.qsize() + self.tts_q.qsize()) * self.avg_clip_s

    @property
    def idle(self) -> bool:
        return self.mt_q.empty() and self.tts_q.empty() and self.mixer.backlog_s <= 0.05

    def status(self) -> dict[str, Any]:
        return {
            "name": self.lang.name,
            "on": self.on,
            "backlog_s": round(self.mixer.backlog_s, 1),
            "pipeline_backlog_s": round(self.total_backlog(), 1),
            "queues": {"translate": self.mt_q.qsize(), "tts": self.tts_q.qsize()},
            "speed": self.backlog.speed_for(self.total_backlog()),
            "condense": self.backlog.condense,
            "tts": {"engine": self.tts.cfg.engine, "voice": self.tts.voice, "error": self.tts.error},
            "listeners": {"stream": self.broadcaster.listeners, "captions": self.hub.count},
            "underruns": self.mixer.underruns,
        }

    # ------------------------------------------------------------ stages
    async def _mt_stage(self) -> None:
        while True:
            rec = await self.mt_q.get()
            res = rec.out[self.code]
            english = rec.en
            # Falling behind? Translate the waiting chunks together in one request.
            merged: list[ChunkRecord] = []
            while not self.mt_q.empty() and len(merged) < 3:
                merged.append(self.mt_q.get_nowait())
            if merged:
                english = " ".join([rec.en] + [m.en for m in merged])
                res.merged = [m.seq for m in merged]
                res.t_ref_end = merged[-1].t_speech_end
                for m in merged:
                    mr = m.out[self.code]
                    mr.status, mr.note = "merged", f"merged into #{rec.seq}"
                    self.p.lang_done(m)
            res.condensed = self.backlog.update_condense(self.total_backlog())
            try:
                tr = await self.translator.translate(english, res.condensed)
            except Exception as e:  # pragma: no cover
                log.exception("[%s] translate crashed", self.code)
                res.status, res.note = "failed", f"translate: {e}"
                self.p.lang_done(rec)
                continue
            res.mt_ms = round(tr.ms, 1)
            res.engine = tr.engine
            res.note = tr.note
            if not tr.text:
                res.status = "failed" if tr.engine == "none" else "skipped"
                self.p.lang_done(rec)
                continue
            res.text = tr.text
            res.t_caption = time.monotonic()
            res.status = "captioned"
            self.caption_no += 1
            res.caption_no = self.caption_no
            self.hub.publish({"type": "caption", "seq": res.caption_no, "session": self.p.session_id,
                              "lang": self.code, "text": tr.text, "ts": time.time()}, remember=True)
            self.p.publish_rec(rec)
            await self.tts_q.put(rec)

    async def _tts_stage(self) -> None:
        sr = self.mixer.sr
        while True:
            rec = await self.tts_q.get()
            res = rec.out[self.code]
            # Hard cap on delay even when the voice itself is the bottleneck: don't voice what is
            # already too far behind the speaker (its caption has been shown).
            age = time.monotonic() - (res.t_ref_end or rec.t_speech_end)
            projected = age + self.avg_tts_s + self.mixer.backlog_s   # when this would start playing
            if projected > self.backlog.drop_above_s:
                res.status, res.note = "dropped", f"audio skipped (would start {projected:.0f}s behind)"
                self.p.lang_done(rec)
                continue
            speed = self.backlog.speed_for(self.total_backlog())
            try:
                pcm, ms = await self.tts.synth(speechify(self.code, res.text), speed)
            except Exception as e:
                log.warning("[%s] tts failed: %s", self.code, e)
                res.status, res.note = "failed", (res.note + f" tts: {e}").strip()
                self.p.lang_done(rec)
                continue
            res.tts_ms = round(ms, 1)
            self.avg_tts_s = 0.7 * self.avg_tts_s + 0.3 * ms / 1000
            res.speed = speed
            res.t_audio_ready = time.monotonic()
            clip = Clip(rec.seq, pcm, {"t_ref": res.t_ref_end or rec.t_speech_end})
            dropped = self.mixer.enqueue(clip, self.backlog.drop_above_s, self.backlog.drop_to_s)
            res.clip_s = round(clip.duration(sr), 2)
            self.avg_clip_s = 0.8 * self.avg_clip_s + 0.2 * res.clip_s
            res.backlog_at_enqueue = round(self.mixer.backlog_s, 2)
            self.p.publish_rec(rec)
            for d in dropped:
                drec = self.p.records.get(d.seq)
                if drec and self.code in drec.out:
                    dr = drec.out[self.code]
                    dr.status, dr.note = "dropped", "audio dropped (more than %.0fs behind)" % self.backlog.drop_above_s
                    self.p.lang_done(drec)
            # Clip-by-clip fallback mode for phones that use it.
            mp3 = await asyncio.to_thread(encode_clip, clip.pcm, sr, self.p.settings.stream.bitrate_kbps)
            self.clips[rec.seq] = mp3
            while len(self.clips) > 80:
                self.clips.popitem(last=False)
            self.hub.publish({"type": "clip", "seq": res.caption_no, "url": f"/clips/{self.code}/{rec.seq}.mp3"})

    def _on_air(self, clip: Clip, t: float) -> None:
        rec = self.p.records.get(clip.seq)
        if rec and self.code in rec.out:
            res = rec.out[self.code]
            self.hub.publish({"type": "onair", "seq": res.caption_no})
            res.t_on_air = t
            res.status = "aired"
            self.p.lang_done(rec)

    def _on_skip(self, clip: Clip, age: float) -> None:
        rec = self.p.records.get(clip.seq)
        if rec and self.code in rec.out:
            res = rec.out[self.code]
            res.status, res.note = "dropped", f"audio skipped ({age:.0f}s behind)"
            self.p.lang_done(rec)

    # ------------------------------------------------------------ misc
    async def say_test(self) -> None:
        pcm, _ = await self.tts.synth(speechify(self.code, self.lang.test_sentence), 1.0)
        self.mixer.enqueue(Clip(0, pcm))
