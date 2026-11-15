"""Meeting health reports: the engine's collector and reporter (app/health.py, with fakes) and the hub's
POST /api/hc/health, its storage and retention, and GET /api/admin/congregations/{id}/health."""
import asyncio
import json
import logging
from types import SimpleNamespace as NS

import pytest

from app import health as H
from app.metrics import ChunkRecord, LangResult
from app.pipeline import Pipeline
from app.hear import api as hear_api
from app.hear.db import HEALTH_KEEP_S, Database

from test_hear import client, hear, make_cong  # noqa: F401  (fixtures)

K = "?key=boss"
SAID = "Brothers and sisters, welcome"          # what was said: must never be in a report
TRANSLATED = "Brāļi un māsas, laipni lūgti"


# ------------------------------------------------------------------ engine: collector

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def rec(seq, src="ru", **out):
    r = ChunkRecord(seq=seq, audio_start_s=0, audio_dur_s=3, voiced_s=2.5, cut_reason="pause", t_speech_start=1,
                    t_speech_end=2, t_cut=2, en=SAID, src=src)
    r.out = out
    return r


def test_the_collector_counts_mic_speech_recognition_translations_and_voices():
    c = H.HealthCollector(Clock())
    for db in (-20.0, -20.0, -60.0, -60.0):   # two loud 100 ms readings, two quiet
        c.level(db)
    c.chunk(2.5)
    c.chunk(1.0)
    c.recognised(2.5, True, 3.0)
    c.recognised(1.0, False, 9.0)
    c.stt_failed()
    c.stt_behind()
    c.finished(rec(1, en=LangResult(text=TRANSLATED, engine="claude", t_caption=5, tts_ms=300, status="aired")))
    c.finished(rec(2, en=LangResult(engine="none", status="failed", note="translator error; no fallback")))
    c.finished(rec(3, en=LangResult(engine="claude", status="skipped", note="empty reply")))
    c.finished(rec(4, en=LangResult(text=TRANSLATED, engine="claude", t_caption=5, status="failed",
                                    note="tts: voice crashed")))
    c.finished(rec(5, en=LangResult(status="merged")))
    c.finished(rec(6, src="en", en=LangResult(text=SAID, engine="original", t_caption=5, tts_ms=1)))   # passed through
    r = c.take()
    assert r["mic"]["peak_db"] == -20.0 and r["mic"]["loud_s"] == 0.2 and r["mic"]["speech_s"] == 3.5
    assert -24 < r["mic"]["rms_db"] < -22          # mean power of the readings
    assert r["asr"] == {"segments": 1, "seconds": 2.5, "empty": 1, "failed": 1, "skipped_behind": 1}
    assert r["lag_max"] == 9.0
    assert r["tr"] == {"en": {"req": 4, "ok": 2, "failed": 1, "skipped": 1, "err": "translator error; no fallback"}}
    assert r["voice"] == {"en": {"ok": 2, "failed": 1, "dropped": 0, "err": "tts: voice crashed"}}
    # a new window starts empty
    r2 = c.take()
    assert r2["asr"]["segments"] == 0 and r2["tr"] == {} and r2["mic"]["rms_db"] is None


def test_pipeline_hooks_feed_the_collector_and_never_break_it():
    p = Pipeline.__new__(Pipeline)   # only the hooks: no models, no audio
    p.health, p.level_db, p.meeting_id = H.HealthCollector(Clock()), -90.0, "m1"
    Pipeline._on_level_thread(p, -30.0)
    assert p.level_db == -30.0 and p.health.n_levels == 1
    p.health = NS()                  # a collector without the method: ignored
    Pipeline._on_level_thread(p, -31.0)
    p.health = None
    Pipeline._on_level_thread(p, -32.0)
    assert p.level_db == -32.0


# ------------------------------------------------------------------ engine: reporter

class Chan:
    def __init__(self, behind=None, phones=0):
        self.behind_s = behind
        self.hub = NS(count=phones)
        self.broadcaster = NS(listeners=0)
        self.tts = NS(cfg=NS(engine="piper"))


class FakePipeline:
    def __init__(self):
        self.running, self.meeting_id = False, ""
        self.speakers, self.on_channels = ["ru"], {"en": Chan(behind=4.0, phones=2)}
        self.stt = NS(engine="faster-whisper", stepped_down="", rtf=0.8)
        self.chunk_q = asyncio.Queue()
        self.source_label, self.last_error = "MacBook Pro Microphone", ""
        self.settings = NS(audio=NS(input_device="", input_channel=0, input_gain=1.0), server=NS(sharing="online"),
                           translate=NS(backend="hub"), stt=NS(engine="auto"))

    def mic_info(self):
        return {"mic_name": "MacBook Pro Microphone", "mic_warning": None}

    def _stt_model(self):
        return "small"


class Hub:
    def __init__(self, status=200, raises=None):
        self.bodies, self.status, self.raises = [], status, raises

    async def __call__(self, body):
        if self.raises:
            raise self.raises
        self.bodies.append(json.loads(json.dumps(body)))   # what would go over the wire
        return self.status

    @property
    def reports(self):
        return [r for b in self.bodies for r in b["reports"]]


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_reporter_sends_start_periodic_and_end_reports_with_the_session_code():
    clock, hub, p = Clock(), Hub(), FakePipeline()
    rep = H.HealthReporter(hub, clock=clock, wall=lambda: 1794650000.0)
    rep.tick(p)                                   # no meeting: nothing
    await settle()
    assert hub.bodies == []
    p.running, p.meeting_id = True, "20261115-093200"
    rep.tick(p)                                   # meeting starts: start report at once (the hub hasn't named it yet)
    await settle()
    start = hub.reports[0]
    assert start["kind"] == "start" and start["meeting"] == "20261115-093200" and start["session"] == ""
    assert start["speakers"] == ["ru"] and start["targets"] == ["en"] and start["input"] == "MacBook Pro Microphone"
    assert start["settings"]["translate"] == "hub" and start["settings"]["voices"] == {"en": "piper"}
    assert start["system"]["arch"] and "cpu" in start["system"]
    p.health = rep.collector
    rep.collector.level(-30.0)
    rep.collector.finished(rec(1, en=LangResult(text=TRANSLATED, engine="claude", t_caption=5, status="aired",
                                                tts_ms=1)))
    clock.t += 10
    rep.tick(p, "ABC123", {"en": {"stream": 1, "captions": 0}})
    await settle()
    assert len(hub.reports) == 1                  # nothing due yet
    clock.t += 21
    rep.tick(p, "ABC123", {"en": {"stream": 1, "captions": 0}})
    await settle()
    per = hub.reports[1]
    assert per["kind"] == "periodic" and per["session"] == "ABC123" and per["window_s"] == 31.0
    assert per["listeners"] == {"en": 3} and per["lag_now"] == 4.0 and per["lag_max"] == 4.0
    assert per["tr"]["en"]["ok"] == 1 and per["asr_model"] == "small"
    p.running = False
    clock.t += 3
    rep.tick(p, "")                               # the session code is already cleared at session_stop
    await settle()
    end = hub.reports[2]
    assert end["kind"] == "end" and end["session"] == "ABC123" and end["window_s"] == 3.0
    text = json.dumps(hub.bodies)
    assert SAID not in text and TRANSLATED not in text   # never what was said or translated


@pytest.mark.asyncio
async def test_reports_made_before_the_session_code_get_it_when_sent():
    clock, p = Clock(), FakePipeline()
    gate = asyncio.Event()
    sent = []

    async def slow(body):
        await gate.wait()
        sent.append(body)
        return 200
    rep = H.HealthReporter(slow, clock=clock)
    p.running, p.meeting_id = True, "m1"
    rep.tick(p)                  # start: sending (held)
    clock.t += 31
    rep.tick(p)                  # periodic waits behind it
    rep.tick(p, "S1")
    gate.set()
    await settle()
    clock.t += 31
    rep.tick(p, "S1")
    await settle()
    assert sent[0]["reports"][0]["session"] == ""          # left before the code was known
    assert [r["session"] for r in sent[1]["reports"]] == ["S1", "S1"]


@pytest.mark.asyncio
async def test_errors_are_reported_at_once_but_not_too_often():
    clock, hub, p = Clock(), Hub(), FakePipeline()
    rep = H.HealthReporter(hub, clock=clock)
    rep.error("RuntimeError", "before any meeting")   # no meeting: ignored
    p.running, p.meeting_id = True, "m1"
    rep.tick(p)
    await settle()
    handler = H.ErrorHandler(rep)
    lg = logging.getLogger("pipeline")
    lg.addHandler(handler)
    try:
        try:
            raise RuntimeError("model file missing " + "x" * 400)
        except RuntimeError:
            lg.exception("stt failed")
        lg.error("second error")                       # within ERROR_EVERY_S: counted, not sent alone
        logging.getLogger("hear").error("a hub error") # the hub's own loggers are not the engine's
    finally:
        lg.removeHandler(handler)
    clock.t += 1
    rep.tick(p)
    await settle()
    errs = [r for r in hub.reports if r["kind"] == "error"]
    assert len(errs) == 1 and errs[0]["error"]["type"] == "RuntimeError"
    assert errs[0]["error"]["message"].startswith("stt failed: model file missing")
    assert len(errs[0]["error"]["message"]) <= H.ERROR_CHARS
    clock.t += 30
    rep.tick(p)
    await settle()
    per = [r for r in hub.reports if r["kind"] == "periodic"][0]
    assert per["errors"] == 2 and per["last_error"].startswith("pipeline: second error")


@pytest.mark.asyncio
async def test_offline_reports_are_dropped_without_holding_anything_up():
    clock, p = Clock(), FakePipeline()
    rep = H.HealthReporter(Hub(raises=OSError("no route to host")), clock=clock)
    p.running, p.meeting_id = True, "m1"
    rep.tick(p)
    await settle()
    assert rep.dropped == 1 and not rep.pending and rep.sent == 0
    for _ in range(H.PENDING_KEEP + 5):          # a send that never finishes: at most PENDING_KEEP wait
        rep._sending = asyncio.get_running_loop().create_future()
        clock.t += 31
        rep.tick(p)
    assert len(rep.pending) == H.PENDING_KEEP
    rep._sending.cancel()


# ------------------------------------------------------------------ hub

def report(kind="periodic", session="S1", **extra):
    r = {"v": 1, "kind": kind, "at": 1794650000.0, "meeting": "20261115-093200", "session": session,
         "window_s": 30.0, "speakers": ["ru"], "targets": ["en"], "input": "MacBook Pro Microphone",
         "mic": {"rms_db": -32.4, "peak_db": -8.0, "loud_s": 19.0, "speech_s": 18.2},
         "asr": {"segments": 6, "seconds": 17.5, "empty": 0, "failed": 0, "skipped_behind": 0},
         "asr_model": "small", "lag_now": 4.2, "lag_max": 6.0,
         "tr": {"en": {"req": 6, "ok": 6, "failed": 0, "skipped": 0, "err": ""}},
         "voice": {"en": {"ok": 6, "failed": 0, "dropped": 0, "err": ""}}, "listeners": {"en": 3}, "errors": 0,
         "system": {"arch": "x86_64", "cpu": "Intel(R) Core(TM) i7", "app": "0.3.0", "build": "19"}}
    r.update(extra)
    return r


def post(client, token, reports):
    return client.post("/api/hc/health", json={"reports": reports},
                       headers={"Authorization": f"Bearer {token}"} if token else {})


def test_the_hub_stores_reports_from_the_device_and_logs_one_line_each(client, hear, caplog):
    cong, token = make_cong(hear, name="Riga")
    sid = hear.db.start_session(cong["id"], ["en"], code="S1")
    assert post(client, None, [report()]).status_code == 403
    assert post(client, "nope", [report()]).status_code == 403
    with caplog.at_level(logging.INFO, logger="hear"):
        r = post(client, token, [report("start", settings={"translate": "hub"}), report(), report(session=""),
                                 report("error", error={"type": "RuntimeError", "message": "boom"}),
                                 {"kind": "nonsense"}, "junk", report(junk="x" * 9000)])
    assert r.status_code == 200 and r.json() == {"ok": True, "stored": 4}
    lines = [m.getMessage() for m in caplog.records if " health" in m.getMessage()]
    slug = cong["slug"]
    assert f"{slug} health: mic -32 dB speech 18s/30s, asr 6 seg, lag 4s (max 6s), tr 6/6 ok, listeners en:3" in lines
    assert any(ln.startswith(f"{slug} health start: ru→en input 'MacBook Pro Microphone' app 0.3.0 (19) x86_64")
               for ln in lines)
    assert f"{slug} health error: RuntimeError: boom" in lines
    rows = hear.db.health(cong["id"])
    assert [x["kind"] for x in rows] == ["start", "periodic", "periodic", "error"]
    assert rows[0]["session_id"] == sid and rows[2]["session_id"] is None and rows[2]["code"] == ""
    assert rows[0]["report"]["settings"] == {"translate": "hub"}
    assert hear.db.speech_by_language(cong["id"]) == {}          # metering untouched


def test_bad_bodies_are_refused(client, hear):
    cong, token = make_cong(hear)
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    assert client.post("/api/hc/health", content=b"nope", headers=h).status_code == 400
    assert client.post("/api/hc/health", json={"x": 1}, headers=h).status_code == 400
    assert client.post("/api/hc/health", content=b"x" * 70000, headers=h).status_code == 413
    assert post(client, token, []).json()["stored"] == 0


def test_a_session_code_is_only_matched_in_the_congregations_own_sessions(client, hear):
    a, ta = make_cong(hear, name="Riga", email="a@example.org")
    b, _ = make_cong(hear, name="Tallinn", email="b@example.org")
    hear.db.start_session(b["id"], ["en"], code="S1")
    post(client, ta, [report()])
    assert hear.db.health(a["id"])[0]["session_id"] is None and hear.db.health(b["id"]) == []


def test_reports_are_kept_thirty_days_and_the_macs_clock_only_when_sane():
    db = Database(":memory:")
    c = db.create_congregation(church="LDS", name="Riga", unit_type="ward", email="a@x.org")
    now = 1794650000.0
    db.record_health(c["id"], None, [report(at=now - 60)], now=now - HEALTH_KEEP_S - 10)   # long ago
    db.record_health(c["id"], None, [report(at=now - 60), report(at=now - 5 * 86400)], now=now)
    rows = db.health(c["id"])
    assert len(rows) == 2                                        # the old one is gone
    assert rows[0]["at"] == now - 60 and rows[1]["at"] == now    # a clock days off: the hub's time instead


def test_the_admin_api_lists_a_congregations_reports(client, hear):
    cong, token = make_cong(hear, name="Riga")
    assert client.get(f"/api/admin/congregations/{cong['id']}/health").status_code == 403
    assert client.get(f"/api/admin/congregations/9999/health{K}").status_code == 404
    post(client, token, [report("start"), report(), report(session="S2")])
    d = client.get(f"/api/admin/congregations/{cong['id']}/health{K}").json()
    assert d["congregation"]["id"] == cong["id"] and [r["kind"] for r in d["reports"]] == ["start", "periodic",
                                                                                          "periodic"]
    r = d["reports"][1]
    assert r["device"] == "Mac" and r["code"] == "S1" and r["report"]["mic"]["rms_db"] == -32.4
    assert len(client.get(f"/api/admin/congregations/{cong['id']}/health{K}&session=S2").json()["reports"]) == 1
    assert client.get(f"/api/admin/congregations/{cong['id']}/health{K}&since=9999999999").json()["reports"] == []
    assert [x["kind"] for x in
            client.get(f"/api/admin/congregations/{cong['id']}/health{K}&limit=1").json()["reports"]] == ["periodic"]


@pytest.mark.asyncio
async def test_the_engine_posts_reports_to_the_hub_with_its_device_token(monkeypatch, tmp_path):
    from app import main
    from app.engine import PublishClient

    calls = []

    async def hub_api(self, method, path, body=None):
        calls.append((method, path, body, self.token))
        return 200, {"ok": True}
    monkeypatch.setattr(PublishClient, "hub_api", hub_api)
    p = FakePipeline()
    p.running, p.meeting_id = True, "m1"
    monkeypatch.setattr(main.S, "pipeline", p, raising=False)
    c = PublishClient("wss://hub.test/ws/engine", "dev-token", "Mac", meter_path=tmp_path / "meter.json")
    c.session_code = "S1"
    task = asyncio.create_task(c._health_loop())
    try:
        for _ in range(40):
            await asyncio.sleep(0.1)
            if calls:
                break
    finally:
        task.cancel()
    assert calls and calls[0][:2] == ("POST", "/api/hc/health") and calls[0][3] == "dev-token"
    assert calls[0][2]["reports"][0]["kind"] == "start" and calls[0][2]["reports"][0]["session"] == "S1"
    assert p.health is c.health.collector       # the pipeline's hooks count into this reporter
