"""Diagnostic logs for support (app/sharelog.py): the scrubber (nothing that was said reaches the shared log), the
shared log file, the engine's uploader, and the hub's POST /api/hc/logs, storage, retention and admin routes."""
from __future__ import annotations

import asyncio
import json
import logging
import re

import pytest

from app import sharelog as SL
from app.hear.db import LOGS_KEEP_S, Database

from test_hear import client, hear, make_cong  # noqa: F401  (fixtures)

K = "?key=boss"

# Real-looking captions, transcripts and translations of a Sunday meeting: none of their words may be shared.
CAPTIONS = [
    "Brothers and sisters, welcome to sacrament meeting this morning.",
    "We will now hear from Sister Kalniņa, who will speak about forgiveness.",
    "Brāļi un māsas, laipni lūgti šīs dienas sakramenta sanāksmē.",
    "Mes dzirdēsim māsu Kalniņu, kura runās par piedošanu.",
    "Дорогие братья и сёстры, добро пожаловать.",
    "Bishop Ozoliņš announced the ward picnic on Saturday at Mežaparks.",
    "Amen.",
    "Mosiah",
    "well-known",
]


def caption_words() -> set[str]:
    return {w.lower() for c in CAPTIONS for w in re.findall(r"\w+", c) if len(w) >= 3}


TEMPLATE_WORDS: set[str] = set()   # the code's own words in the log messages below (e.g. "from"): not leaks


def assert_no_caption_words(text: str) -> None:
    low = text.lower()
    leaked = sorted(w for w in caption_words() - TEMPLATE_WORDS
                    if re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low))
    assert not leaked, f"caption words reached the shared log: {leaked}\n{text}"


def make(logger: str, level: int, msg, *args, exc=None) -> logging.LogRecord:
    if isinstance(msg, str) and not msg.startswith("caption:"):
        TEMPLATE_WORDS.update(w.lower() for w in re.findall(r"\w+", msg))
    exc_info = None
    if exc is not None:
        try:
            raise exc
        except BaseException as e:
            exc_info = (type(e), e, e.__traceback__)
    return logging.LogRecord(logger, level, "pipeline.py", 42, msg, args or None, exc_info)


def feed_captions() -> list[logging.LogRecord]:
    """Log records the engine makes, with captions in them the ways text can end up in a log."""
    recs = []
    for i, c in enumerate(CAPTIONS):
        recs += [
            make("translate.prompts", logging.WARNING, "dropped markup from a translation: %r", c),   # prompts.py
            make("channel", logging.WARNING, "[%s] %s stage failed on #%s (continuing): %r", "lv", "tts", i,
                 RuntimeError(f"cannot speak '{c}'")),                                                # channel.py
            make("talk", logging.WARNING, "translate failed: %s", ValueError(f'bad reply "{c}"')),
            make("pipeline", logging.INFO, "#%d recognised (%s): %s", i, "en", c),
            make("pipeline", logging.INFO, "#%(seq)d: %(text)s", {"seq": i, "text": c}),
            make("channel", logging.ERROR, "[%s] translate crashed", "lv", exc=RuntimeError(f"translator error: {c}")),
            make("stt", logging.INFO, "words: %s", c.split()),
            make("stt", logging.INFO, "segment %s", {"text": c, "lang": "en", "seq": i}),
            make("glossary", logging.INFO, "glossary hit %s → %s in %s", c.split()[0], c.split()[-1], c),
            make("app", logging.INFO, f"caption: '{c}'"),            # baked into the message, but quoted
        ]
    return recs


def test_no_word_of_what_was_said_reaches_the_shared_log(tmp_path):
    handler = SL.SharedLogHandler(tmp_path)
    for r in feed_captions():
        handler.emit(r)
    handler.close()
    text = (tmp_path / SL.SHARED_NAME).read_text()
    assert text.count("\n") == len(feed_captions())          # every record made a line: just without the words
    assert_no_caption_words(text)
    # What support needs is still there: the code's own words, numbers, language codes, stages, error types, places.
    assert "dropped markup from a translation: ‹" in text
    assert "[lv] tts stage failed on #3 (continuing): RuntimeError: ‹" in text
    assert "translate crashed | RuntimeError: translator error: ‹" in text   # unquoted text in an exception too
    assert re.search(r"test_sharelog\.py:\d+ in make", text)                   # where it was raised
    lines = [json.loads(ln) for ln in text.splitlines()]
    assert {ln["lvl"] for ln in lines} == {"INFO", "WARNING", "ERROR"} and all(ln["t"] > 0 for ln in lines)


def test_an_errors_technical_words_are_kept():
    for exc, want in [(ConnectionRefusedError(61, "Connection refused"), "ConnectionRefusedError: [Errno 61] Connection refused"),
                      (RuntimeError("translator error: HTTP 529 overloaded_error"), "RuntimeError: translator error: HTTP 529 overloaded_error"),
                      (OSError("[Errno -9981] Input overflowed"), "OSError: [Errno -9981] Input ‹10 chars›")]:
        assert SL.scrub_value(exc) == want


def test_unquoted_text_in_an_exception_is_cut_to_its_length():
    line = SL.scrub(make("channel", logging.ERROR, "[%s] translate crashed", "lv",
                         exc=RuntimeError("translator error: Brothers and sisters, welcome")))
    assert_no_caption_words(line["msg"])
    assert line["msg"].startswith("[lv] translate crashed | RuntimeError: translator error: ‹")


def test_technical_values_are_kept():
    def msg(m, *a):
        return SL.scrub(make("engine", logging.INFO, m, *a))["msg"]

    assert msg("claude API session #%d started (model %s)", 3, "claude-sonnet-4-5") == \
        "claude API session #3 started (model claude-sonnet-4-5)"
    assert msg("Piper voice %r not found; using %s", "fr_FR-tom-medium", "fr_FR-siwis-medium") == \
        "Piper voice fr_FR-tom-medium not found; using fr_FR-siwis-medium"
    assert msg("%s translation switched %s", "Latviešu", "on") == "Latviešu translation switched on"
    assert msg("ollama %s warm in %.1fs", "qwen2.5:7b", 2.345) == "ollama qwen2.5:7b warm in 2.3s"
    assert msg("billing %s: translating through %s (was %s)", "credits", "hub", "api") == \
        "billing credits: translating through hub (was api)"
    assert msg("mic level %.1f dB, lag %ss, versions %s", -32.44, 4, "0.3.0") == "mic level -32.4 dB, lag 4s, " \
                                                                                   "versions 0.3.0"
    assert msg("Speech recognition was too slow on this computer (1.40s of work per second of speech with small): "
               "switching to the smaller base model").startswith("Speech recognition was too slow")
    assert msg("loading %s", "/Users/crystal/Library/x.bin".replace("/Users/crystal", __import__("os").path
                                                                     .expanduser("~"))) == "loading ~/Library/x.bin"


def test_debug_lines_and_the_hubs_loggers_are_not_shared():
    assert SL.scrub(make("pipeline", logging.DEBUG, "anything")) is None
    assert SL.scrub(make("hear.api", logging.INFO, "anything")) is None
    assert SL.scrub(make("uvicorn.error", logging.ERROR, "anything")) is None


def test_long_lines_are_cut():
    line = SL.scrub(make("engine", logging.INFO, "x" * 2000))
    assert len(line["msg"]) == SL.LINE_CHARS


def test_health_error_reports_are_scrubbed_too():
    from app import health as H

    seen = []

    class Rep:
        def error(self, kind, message):
            seen.append((kind, message))
    h = H.ErrorHandler(Rep())
    h.emit(make("channel", logging.ERROR, "[%s] translate crashed", "lv", exc=RuntimeError(f"bad '{CAPTIONS[0]}'")))
    h.emit(make("translate.prompts", logging.ERROR, "dropped markup from a translation: %r", CAPTIONS[2]))
    assert len(seen) == 2
    for kind, m in seen:
        assert_no_caption_words(m)
    assert seen[0][0] == "RuntimeError" and seen[0][1].startswith("[lv] translate crashed: ‹")


def test_the_shared_log_is_read_back_by_time_across_rotated_files(tmp_path, monkeypatch):
    monkeypatch.setattr(SL, "MAX_BYTES", 2000)
    h = SL.SharedLogHandler(tmp_path)
    for i in range(60):
        r = make("engine", logging.INFO, "line %d", i)
        r.created = 1000.0 + i
        h.emit(r)
    h.close()
    assert (tmp_path / f"{SL.SHARED_NAME}.1").exists()
    got = SL.read_shared(tmp_path, 1040.0)
    assert [ln["msg"] for ln in got] == [f"line {i}" for i in range(41, 60)]


# ------------------------------------------------------------------ the engine's uploader

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_the_shipper_sends_batches_only_while_sharing_is_on():
    sent = []

    async def post(body):
        sent.append(body)
        return 200
    clock = Clock()
    s = SL.LogShipper(post, clock=clock)
    s.add({"t": 1, "lvl": "INFO", "src": "x", "msg": "before"})   # off: not kept
    s.tick()
    assert not s.pending and not sent
    s.set_enabled(True)
    for i in range(SL.BATCH_LINES + 10):
        s.add({"t": i, "lvl": "INFO", "src": "x", "msg": f"m{i}"})
    s.tick()
    await settle()
    assert len(sent) == 1 and sent[0]["kind"] == "stream" and len(sent[0]["lines"]) == SL.BATCH_LINES
    s.tick()                                    # the rest waits SEND_EVERY_S
    await settle()
    assert len(sent) == 1
    s.add({"t": 9, "lvl": "ERROR", "src": "x", "msg": "boom"})   # an error goes at once
    s.tick()
    await settle()
    assert len(sent) == 2 and sent[1]["lines"][-1]["msg"] == "boom" and s.sent == SL.BATCH_LINES + 11
    s.add({"t": 9, "lvl": "INFO", "src": "x", "msg": "later"})
    s.set_enabled(False)                        # turned off: what waits is dropped
    assert not s.pending


@pytest.mark.asyncio
async def test_a_batch_stays_under_the_hubs_size_cap():
    sent = []

    async def post(body):
        sent.append(body)
        return 200
    s = SL.LogShipper(post)
    s.set_enabled(True)
    for i in range(300):
        s.add({"t": i, "lvl": "WARNING", "src": "x", "msg": "y" * 490})
    s.tick()
    await settle()
    assert len(json.dumps(sent[0])) < 64 * 1024


@pytest.mark.asyncio
async def test_a_hub_that_cant_be_reached_drops_the_batch():
    async def post(body):
        raise OSError("no route to host")
    s = SL.LogShipper(post)
    s.set_enabled(True)
    s.add({"t": 1, "lvl": "ERROR", "src": "x", "msg": "m"})
    s.tick()
    await settle()
    assert s.dropped == 1 and not s.pending


# ------------------------------------------------------------------ hub

def lines(n=3, msg="stt: model small loaded"):
    return [{"t": 1794650000.0 + i, "lvl": "INFO", "src": "stt", "msg": msg} for i in range(n)]


def post(client, token, body):
    return client.post("/api/hc/logs", json=body, headers={"Authorization": f"Bearer {token}"} if token else {})


def test_the_hub_stores_batches_from_the_device_only(client, hear):
    cong, token = make_cong(hear, name="Riga")
    assert post(client, None, {"lines": lines()}).status_code == 403
    assert post(client, "nope", {"lines": lines()}).status_code == 403
    r = post(client, token, {"kind": "stream", "lines": lines() + ["junk", {"msg": 3}, {"lvl": "INFO"}]})
    assert r.status_code == 200 and r.json()["stored"] == 3
    rows = hear.db.logs(cong["id"])
    assert len(rows) == 1 and rows[0]["kind"] == "stream" and rows[0]["n_lines"] == 3 and rows[0]["note"] == ""
    assert rows[0]["lines"][0] == {"t": 1794650000.0, "lvl": "INFO", "src": "stt", "msg": "stt: model small loaded"}
    assert post(client, token, {"lines": []}).json() == {"ok": True, "ref": None, "stored": 0}   # nothing stored
    assert len(hear.db.logs(cong["id"])) == 1


def test_send_logs_to_support_gives_a_reference_and_keeps_the_note(client, hear, caplog):
    cong, token = make_cong(hear, name="Riga")
    with caplog.at_level(logging.INFO, logger="hear"):
        r = post(client, token, {"kind": "manual", "lines": lines(5), "note": "  No sound in the chapel today  "})
    d = r.json()
    assert r.status_code == 200 and isinstance(d["ref"], int) and d["stored"] == 5
    b = hear.db.log_batch(d["ref"])
    assert b["kind"] == "manual" and b["note"] == "No sound in the chapel today" and b["congregation_id"] == cong["id"]
    msgs = " ".join(m.getMessage() for m in caplog.records)
    assert f"ref #{d['ref']}" in msgs and "chapel" not in msgs       # the hub's log has the note's length only
    assert post(client, token, {"kind": "stream", "lines": lines(), "note": "ignored"}).status_code == 200
    assert hear.db.logs(cong["id"], kind="stream")[0]["note"] == ""


def test_bad_bodies_and_sizes_are_refused(client, hear):
    cong, token = make_cong(hear)
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    assert client.post("/api/hc/logs", content=b"nope", headers=h).status_code == 400
    assert post(client, token, {"kind": "other", "lines": lines()}).status_code == 400
    assert post(client, token, {"kind": "stream"}).status_code == 400
    big = lines(200, "x" * 400)
    assert post(client, token, {"kind": "stream", "lines": big}).status_code == 413      # > 64 KB as a stream batch
    assert post(client, token, {"kind": "manual", "lines": big}).status_code == 200      # fine for a manual send
    assert client.post("/api/hc/logs", content=b"x" * (1024 * 1024 + 1), headers=h).status_code == 413
    r = post(client, token, {"kind": "manual", "lines": lines(1, "m" * 5000), "note": "n" * 5000})
    b = hear.db.log_batch(r.json()["ref"])
    assert len(b["lines"][0]["msg"]) == 1000 and len(b["note"]) == 1000


def test_manual_sends_are_rate_limited(client, hear):
    cong, token = make_cong(hear)
    codes = [post(client, token, {"kind": "manual", "lines": lines(1)}).status_code for _ in range(8)]
    assert codes[:6] == [200] * 6 and 429 in codes[6:]


def test_logs_are_kept_thirty_days_and_capped_per_congregation(monkeypatch):
    import app.hear.db as D
    db = Database(":memory:")
    c = db.create_congregation(church="LDS", name="Riga", unit_type="ward", email="a@x.org")
    now = 1794650000.0
    db.record_logs(c["id"], None, "stream", lines(), now=now - LOGS_KEEP_S - 10)
    keep = db.record_logs(c["id"], None, "manual", lines(), "help", now=now - 86400)
    db.record_logs(c["id"], None, "stream", lines(), now=now)
    assert [b["id"] for b in db.logs(c["id"])][0] == keep and len(db.logs(c["id"])) == 2   # the old one is gone
    monkeypatch.setattr(D, "LOGS_KEEP_BYTES", 600)
    for i in range(5):
        db.record_logs(c["id"], None, "stream", lines(), now=now + i)
    rows = db.logs(c["id"])
    assert rows[0]["id"] == keep                        # a manual send is never pushed out by the stream
    assert sum(len(json.dumps(b["lines"], separators=(",", ":"))) for b in rows) <= 600 + 300


def test_the_admin_api_lists_logs_and_finds_a_reference(client, hear):
    cong, token = make_cong(hear, name="Riga")
    other, tok2 = make_cong(hear, name="Tallinn", email="b@example.org")
    assert client.get(f"/api/admin/congregations/{cong['id']}/logs").status_code == 403
    assert client.get(f"/api/admin/congregations/9999/logs{K}").status_code == 404
    post(client, token, {"lines": lines(2)})
    ref = post(client, token, {"kind": "manual", "lines": lines(4), "note": "help"}).json()["ref"]
    post(client, tok2, {"lines": lines(1)})
    d = client.get(f"/api/admin/congregations/{cong['id']}/logs{K}").json()
    assert d["congregation"]["id"] == cong["id"] and [b["kind"] for b in d["batches"]] == ["stream", "manual"]
    assert d["batches"][1]["device"] == "Mac" and d["batches"][1]["note"] == "help"
    assert [b["id"] for b in client.get(f"/api/admin/congregations/{cong['id']}/logs{K}&kind=manual")
            .json()["batches"]] == [ref]
    assert client.get(f"/api/admin/congregations/{cong['id']}/logs{K}&since=9999999999").json()["batches"] == []
    assert client.get(f"/api/admin/logs/{ref}").status_code == 403
    one = client.get(f"/api/admin/logs/{ref}{K}").json()
    assert one["congregation"]["id"] == cong["id"] and one["batch"]["n_lines"] == 4
    assert client.get(f"/api/admin/logs/99999{K}").status_code == 404


@pytest.mark.asyncio
async def test_the_engine_sends_new_lines_only_with_sharing_on(monkeypatch, tmp_path):
    from app import main
    from app.config import Settings
    from app.engine import PublishClient

    calls = []

    async def hub_api(self, method, path, body=None):
        calls.append((method, path, body, self.token))
        return 200, {"ok": True}
    monkeypatch.setattr(PublishClient, "hub_api", hub_api)
    monkeypatch.setattr(main.S, "pipeline", None, raising=False)
    monkeypatch.setattr(main.S, "settings", Settings.model_validate({"support": {"share_logs": True}}), raising=False)
    monkeypatch.setattr(SL, "SEND_EVERY_S", 0.0)
    handler = SL.SharedLogHandler(tmp_path, lambda ln: SL.SHIPPER.add(ln) if SL.SHIPPER else None)
    lg = logging.getLogger("pipeline")
    lg.addHandler(handler)
    c = PublishClient("wss://hub.test/ws/engine", "dev-token", "Mac", meter_path=tmp_path / "meter.json")
    task = asyncio.create_task(c._health_loop())
    try:
        await asyncio.sleep(0.05)
        lg.warning("dropped markup from a translation: %r", CAPTIONS[0])
        for _ in range(30):
            await asyncio.sleep(0.1)
            if any(x[1] == "/api/hc/logs" for x in calls):
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lg.removeHandler(handler)
        handler.close()
    sent = [x for x in calls if x[1] == "/api/hc/logs"]
    assert sent and sent[0][3] == "dev-token" and sent[0][2]["kind"] == "stream"
    assert sent[0][2]["lines"][0]["msg"].startswith("dropped markup from a translation: ‹")
    assert_no_caption_words(json.dumps(sent[0][2], ensure_ascii=False))
    assert SL.SHIPPER is None                  # detached when the link's loop ends


def test_the_share_setting_comes_from_the_mac_app(monkeypatch, tmp_path):
    from app.config import load_settings

    assert load_settings(tmp_path / "none.toml").support.share_logs is False
    monkeypatch.setenv("HEARCHURCH_SHARE_LOGS", "1")
    assert load_settings(tmp_path / "none.toml").support.share_logs is True
    monkeypatch.setenv("HEARCHURCH_SHARE_LOGS", "0")
    assert load_settings(tmp_path / "none.toml").support.share_logs is False
