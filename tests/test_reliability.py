"""Engine reliability: stages survive bad records, no meeting without a speech model, mic watchdog, no git in the app."""
import asyncio
import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app import audio_in, main
from app.broadcast.mixer import Mixer
from app.channel import Channel
from app.config import Settings
from app.metrics import ChunkRecord, LangResult


async def test_mixer_clock_survives_a_failing_consumer():
    calls = []

    def on_frame(frame):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("bad frame")

    m = Mixer(16000, on_frame=on_frame)
    m.start()
    await asyncio.sleep(0.55)
    assert not m._task.done() and len(calls) >= 4
    m._task.cancel()


def _rec(seq, with_out=True):
    r = ChunkRecord(seq=seq, audio_start_s=0, audio_dur_s=1, voiced_s=1, cut_reason="x",
                    t_speech_start=0, t_speech_end=0, t_cut=0, en="Hello.")
    if with_out:
        r.out = {"fr": LangResult()}
    return r


async def test_channel_stages_keep_going_after_an_exception():
    done = []
    pipeline = SimpleNamespace(settings=Settings(), lang_done=done.append, records={}, session_id="s",
                               publish_rec=lambda r: None)

    class Tr:
        async def translate(self, english, condensed, scripture=""):
            return SimpleNamespace(text="Bonjour.", ms=1.0, engine="claude", note="")

    mixer = SimpleNamespace(sr=16000, on_air=None, on_skip=None, max_age_s=0, backlog_s=0.0)
    lang = SimpleNamespace(code="fr", name="French")
    ch = Channel(pipeline, lang, Tr(), None, mixer, None, SimpleNamespace(publish=lambda *a, **k: None))
    ch.backlog.update_condense = lambda b: False
    bad, good = _rec(1, with_out=False), _rec(2)     # `bad` has no entry for "fr": the stage raises KeyError
    task = asyncio.create_task(ch._mt_stage())
    ch.mt_q.put_nowait(bad)
    ch.mt_q.put_nowait(good)
    rec = await asyncio.wait_for(ch.tts_q.get(), 2)   # the second record still comes through
    assert rec is good and good.out["fr"].text == "Bonjour." and not task.done()
    assert bad in done
    task.cancel()


async def test_start_refuses_without_the_speech_model(monkeypatch):
    monkeypatch.setattr(main, "require_operator", lambda r: None)
    monkeypatch.setattr(main.S, "engine_hub", SimpleNamespace(run_active=False), raising=False)
    monkeypatch.setattr(main.S, "pipeline", SimpleNamespace(stt=SimpleNamespace(ready=False)), raising=False)
    monkeypatch.setattr(main.S, "app_mode", True)

    async def not_downloaded():
        return False

    monkeypatch.setattr(main, "_stt_downloaded", not_downloaded)

    class Req:
        async def json(self):
            return {"source": "mic"}

    with pytest.raises(main.HTTPException) as e:
        await main.api_start(Req())
    assert e.value.status_code == 400 and "speech model isn't downloaded" in e.value.detail


async def test_ensure_stt_waits_for_the_download_in_app_mode(monkeypatch):
    loaded = []

    async def load():
        loaded.append(1)

    pl = SimpleNamespace(stt=SimpleNamespace(ready=False), stt_loading=False, load_stt=load)
    monkeypatch.setattr(main.S, "pipeline", pl, raising=False)
    monkeypatch.setattr(main.S, "app_mode", True)
    present = [False]

    async def downloaded():
        return present[0]

    monkeypatch.setattr(main, "_stt_downloaded", downloaded)
    await main._ensure_stt()
    assert not loaded                  # nothing is downloaded behind the Packages system's back
    present[0] = True
    await main._ensure_stt()           # what on_ready / Start do once the package is there
    assert loaded == [1]


def test_mic_watchdog_warns_reopens_the_same_device_and_clears(monkeypatch):
    src = audio_in.MicSource.__new__(audio_in.MicSource)
    src.name, src.channel, src.native_sr = "USB Audio", 0, 16000
    src.device = {"index": 3, "name": "USB Audio", "channels": 1, "samplerate": 16000, "default": False}
    src.warning, src._q, src._stop = None, queue.Queue(), threading.Event()
    src._stream, src._stream_dead = None, False
    src.STALL_S, src.RETRY_S = 0.3, 0.1
    opened = []

    class Stream:
        def stop(self): pass
        def close(self): pass

    def fake_open():
        opened.append(src.device["index"])
        src._stream = Stream()

    present = [True]
    other = {"index": 9, "name": "AirPods", "channels": 1, "samplerate": 16000, "default": True}
    monkeypatch.setattr(src, "_open", fake_open)
    monkeypatch.setattr(audio_in, "refresh_devices", lambda: None)
    monkeypatch.setattr(audio_in, "list_input_devices", lambda: ([src.device] if present[0] else []) + [other])
    got = []
    t = threading.Thread(target=lambda: [got.append(b) for b in src.blocks()], daemon=True)
    t.start()

    def wait(cond, s=3):
        end = time.time() + s
        while time.time() < end and not cond():
            time.sleep(0.02)
        return cond()

    src._q.put((np.zeros(800, dtype=np.float32), time.monotonic()))
    assert wait(lambda: got) and src.warning is None
    present[0] = False                                   # unplugged; the default (AirPods) is still there
    assert wait(lambda: src.warning and "USB Audio" in src.warning)
    assert opened == [3]                                 # never opened the other device
    present[0] = True                                    # plugged back in
    assert wait(lambda: opened == [3, 3])
    src._q.put((np.zeros(800, dtype=np.float32), time.monotonic()))
    assert wait(lambda: src.warning is None)
    src.stop()
    t.join(2)


def test_mic_info_in_status():
    from app.pipeline import Pipeline

    p = Pipeline.__new__(Pipeline)
    p.worker = None
    assert p.mic_info() == {"mic_name": None, "mic_warning": None}
    p.worker = SimpleNamespace(source=SimpleNamespace(name="USB Audio", warning="stopped"))
    assert p.mic_info() == {"mic_name": "USB Audio", "mic_warning": "stopped"}


def test_no_git_in_app_mode(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("git was run")

    monkeypatch.setattr(main.subprocess, "run", boom)
    monkeypatch.setattr(main.S, "app_mode", True)
    monkeypatch.setattr(main, "ROOT", tmp_path)
    assert main._code_version() == "app"
    (tmp_path / "VERSION").write_text("1.2.3 (abc123)\n")
    assert main._code_version() == "1.2.3 (abc123)"
