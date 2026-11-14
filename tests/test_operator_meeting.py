"""The operator's transcript and stats (web page and Mac app): a new meeting starts empty, its lines carry its id,
and the meeting's numbers (lines, skips, delays per language, listeners) are in /api/status."""
import asyncio
from types import SimpleNamespace

from app import pipeline as pipeline_mod
from app.config import Settings
from app.metrics import ChunkRecord, LangResult, live_stats
from app.pipeline import Pipeline


class _Usage:
    input = cache_read = cache_write = output = turns = 0
    cost_usd = 0.0


def _pipe(monkeypatch, tmp_path, targets=("en",)):
    monkeypatch.setattr(pipeline_mod, "LOG_DIR", tmp_path)
    monkeypatch.setattr(pipeline_mod, "CaptureWorker",
                        lambda *a, **k: SimpleNamespace(start=lambda: None, stop=lambda: None, source=None))
    settings = Settings(meeting={"speakers": ["ru"], "targets": list(targets), "auto_en": False})
    pipe = Pipeline.__new__(Pipeline)
    pipe.settings, pipe.records, pipe._seq, pipe.metrics = settings, {}, 0, None
    pipe.capturing = pipe.running = False
    pipe.worker, pipe._caff, pipe.meeting_id, pipe.session_id = None, None, "", None
    pipe._stats_cache, pipe._usage_at_start = None, {}
    pipe.vad = SimpleNamespace(reset=lambda: None)
    pipe.refs = SimpleNamespace(reset=lambda: None)
    pipe.stt = SimpleNamespace(events=[], engine="mlx", stepped_down="", model_name="", ready=True, rtf=0.0)
    sent = []
    pipe.operator = SimpleNamespace(publish=sent.append)
    pipe.chunk_q = asyncio.Queue()

    def channel(code):
        return SimpleNamespace(code=code, on=code in targets, behind_s=3.0, recording=None,
                               hub=SimpleNamespace(history=[], publish=lambda m: None),
                               translator=SimpleNamespace(usage=_Usage, prepare_fallback=lambda: None))
    pipe.channels = {c: channel(c) for c in ("en", "es", "lv")}
    return pipe, sent


async def test_each_meeting_announces_a_new_id_and_its_translation_columns(monkeypatch, tmp_path):
    pipe, sent = _pipe(monkeypatch, tmp_path, targets=("en", "lv"))
    pipe.start("mic", source=SimpleNamespace(label="Mic"))
    first = [m for m in sent if m["type"] == "meeting"]
    assert len(first) == 1 and first[0]["targets"] == ["en", "lv"] and first[0]["speakers"] == ["ru"]
    assert first[0]["meeting"] == pipe.meeting_id and pipe.channels["en"].behind_s is None   # old delays gone
    # The meeting ends; the next one translates into Spanish only.
    pipe.capturing = False
    for c, ch in pipe.channels.items():
        ch.on = c == "es"
    pipe.start("mic", source=SimpleNamespace(label="Mic"))
    second = [m for m in sent if m["type"] == "meeting"][1]
    assert second["meeting"] != first[0]["meeting"]             # unique even within the same second
    assert second["targets"] == ["es"]
    assert pipe.meeting_stats()["lines"] == 0                    # the stats start over too


async def test_lines_carry_the_meeting_they_were_heard_in(monkeypatch, tmp_path):
    """A line still finishing from the last meeting keeps its id, so the operator's new transcript leaves it out."""
    pipe, _ = _pipe(monkeypatch, tmp_path)
    pipe.loop = asyncio.get_running_loop()
    pipe.meeting_id = "old"
    chunk = SimpleNamespace(start_s=0, duration=1, voiced_s=1.0, reason="x", t_speech_start=0, t_speech_end=0,
                            t_cut=0, audio=None, meeting="")
    pipe._on_chunk_thread(chunk)
    await asyncio.sleep(0)
    pipe.meeting_id = "new"                                      # the next meeting started before it was recognised
    got = await pipe.chunk_q.get()
    assert got.meeting == "old"
    rec = ChunkRecord(seq=1, audio_start_s=0, audio_dur_s=1, voiced_s=1, cut_reason="x", t_speech_start=0,
                      t_speech_end=0, t_cut=0, meeting=got.meeting)
    assert rec.public()["meeting"] == "old"


def _done(seq, status="done", voiced=2.0, **langs):
    rec = ChunkRecord(seq=seq, audio_start_s=0, audio_dur_s=voiced, voiced_s=voiced, cut_reason="x",
                      t_speech_start=0.0, t_speech_end=100.0, t_cut=0.0, status=status, stt_ms=200.0)
    for code, (st, lag, air, engine) in langs.items():
        rec.out[code] = LangResult(text="t", status=st, engine=engine, t_caption=100.0 + lag if lag else 0.0,
                                   t_on_air=100.0 + air if air else 0.0)
    return rec.public()


def test_live_stats_counts_lines_skips_and_delays_per_language():
    records = [
        _done(1, en=("aired", 2.0, 4.0, "claude"), lv=("aired", 3.0, 5.0, "claude")),
        _done(2, en=("dropped", 6.0, None, "claude"), lv=("failed", None, None, "")),
        _done(3, status="skipped"),
        _done(4, en=("aired", 1.0, 3.0, "original"), lv=("aired", 2.0, 6.0, "claude")),
    ]
    s = live_stats(records)
    assert s["lines"] == 3 and s["skipped"] == 1 and s["speech_s"] == 6.0 and s["stt_ms_p50"] == 200.0
    en, lv = s["langs"]["en"], s["langs"]["lv"]
    assert (en["captioned"], en["aired"], en["dropped"], en["failed"]) == (3, 2, 1, 0)
    assert en["engines"] == {"claude": 2, "original": 1}
    assert en["caption_p50"] == 2.0 and en["caption_p95"] == 6.0 and en["audio_p50"] == 4.0
    assert (lv["captioned"], lv["failed"]) == (2, 1)


def test_status_carries_the_meeting_id_and_stats(monkeypatch, tmp_path):
    pipe, _ = _pipe(monkeypatch, tmp_path)
    pipe.start("mic", source=SimpleNamespace(label="Mic"))
    pipe.metrics.records.append(_done(1, en=("aired", 2.0, 4.0, "claude")))
    stats = pipe.meeting_stats()
    assert stats["lines"] == 1 and stats["langs"]["en"]["caption_p50"] == 2.0 and stats["usage"]["turns"] == 0
    assert pipe._stt_model() == "whisper-large-v3-turbo"

