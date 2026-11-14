"""V3.1: a meeting = speaker languages × translation languages, pass-through, Latvian, glossaries by church."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.channel import Channel
from app.config import Settings, tts_for
from app.glossary_scope import for_church, is_lds_church, scope_of
from app.languages import LANGUAGES, SPEAKER_LANGUAGES, glossary_for
from app.metrics import ChunkRecord, LangResult
from app.packages import PIPER_VOICES, SIZES_MB
from app.stt import STICKY, pick_language
from app.translate.prompts import build_system_prompt

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ languages and settings

def test_twelve_languages_eleven_speakable():
    assert set(LANGUAGES) == {"fr", "wo", "es", "ru", "pt", "uk", "fa", "ne", "vi", "zh", "en", "lv"}
    assert set(SPEAKER_LANGUAGES) == set(LANGUAGES) - {"wo"}    # Whisper has no Wolof
    assert list(LANGUAGES)[-1] == "lv"   # the hub link numbers languages in this order: new ones go last


def test_older_settings_become_a_meeting():
    s = Settings(languages=["fr", "wo"], stt={"language": "ru"})
    assert (s.meeting.speakers, s.meeting.targets, s.meeting.auto_en) == (["ru"], ["fr", "wo", "en"], True)
    assert s.languages == ["fr", "wo"] and s.stt.languages == ["ru"]
    assert Settings().meeting.targets == ["fr", "wo"]          # an English speaker: no English target
    assert Settings(languages=[], stt={"language": "ru"}).meeting.targets == ["en"]   # Riga's rush setup


def test_an_explicit_meeting_wins_and_keeps_the_legacy_views_in_sync():
    s = Settings(languages=["fr"], meeting={"speakers": ["ru", "lv", "wo", "xx"], "targets": ["en", "lv", "xx"],
                                            "auto_en": False})
    assert s.meeting.speakers == ["ru", "lv"] and s.meeting.targets == ["en", "lv"]   # unknown / Wolof dropped
    assert s.stt.language == "ru" and s.stt.languages == ["ru", "lv"] and s.languages == ["en", "lv"]
    # English stays a target with an English speaker once the meeting is explicit (pass-through for it)
    assert Settings(meeting={"speakers": ["en"], "targets": ["en", "fr"], "auto_en": False}).meeting.targets == ["en", "fr"]


# ------------------------------------------------------------------ speech recognition

def test_language_detection_is_restricted_to_the_meetings_speaker_languages():
    probs = {"uk": 0.55, "ru": 0.30, "lv": 0.10, "en": 0.05}
    assert pick_language(probs, ["ru", "lv"]) == "ru"           # Ukrainian isn't spoken in this meeting
    assert pick_language(probs, ["lv", "en"]) == "lv"
    assert pick_language({}, ["ru", "lv"]) == "ru"              # nothing known: the first


def test_a_close_call_keeps_the_previous_language():
    probs = {"ru": 0.48, "lv": 0.48 - STICKY / 2}
    assert pick_language(probs, ["ru", "lv"], prev="lv") == "lv"
    assert pick_language({"ru": 0.9, "lv": 0.05}, ["ru", "lv"], prev="lv") == "ru"


def test_one_speaker_language_runs_no_detection():
    from app.config import SttCfg
    from app.stt import Transcriber

    t = Transcriber(SttCfg(engine="faster-whisper"))
    assert t.detect(None, ["lv"]) == "lv"     # no model needed


def test_several_speaker_languages_never_use_an_english_only_model():
    from app.config import SttCfg
    from app.stt import Transcriber

    t = Transcriber(SttCfg(engine="faster-whisper", faster_whisper_model="small.en", language="en", languages=["en"]))
    assert t._fw_wanted() == "small.en"
    t.cfg.languages = ["en", "fr"]
    assert t._fw_wanted() == "small"


def test_faster_whisper_knows_latvian():
    tok = pytest.importorskip("faster_whisper.tokenizer")
    assert "lv" in tok._LANGUAGE_CODES


# ------------------------------------------------------------------ pass-through and billing

def _channel(code, done, published):
    pipeline = SimpleNamespace(settings=Settings(), lang_done=done.append, records={}, session_id="s",
                               publish_rec=lambda r: None)
    calls = []

    class Tr:
        async def translate(self, text, condensed, scripture=""):
            calls.append(text)
            return SimpleNamespace(text=f"<{code}> {text}", ms=1.0, engine="claude", note="")

    mixer = SimpleNamespace(sr=16000, on_air=None, on_skip=None, max_age_s=0, backlog_s=0.0)
    hub = SimpleNamespace(publish=lambda m, remember=False: published.append(m))
    ch = Channel(pipeline, SimpleNamespace(code=code, name=code), Tr(), None, mixer, None, hub)
    ch.backlog.update_condense = lambda b: False
    return ch, calls


def _rec(seq, src, text):
    r = ChunkRecord(seq=seq, audio_start_s=0, audio_dur_s=1, voiced_s=1, cut_reason="x",
                    t_speech_start=0, t_speech_end=0, t_cut=0, en=text, src=src)
    r.out = {"lv": LangResult(), "en": LangResult()}
    return r


async def test_a_chunk_in_the_channels_own_language_passes_through():
    done, published = [], []
    ch, calls = _channel("lv", done, published)
    task = asyncio.create_task(ch._mt_stage())
    lv, ru = _rec(1, "lv", "Labdien, brāļi un māsas."), _rec(2, "ru", "Добрый день.")
    ch.mt_q.put_nowait(lv)
    ch.mt_q.put_nowait(ru)
    a = await asyncio.wait_for(ch.tts_q.get(), 2)
    b = await asyncio.wait_for(ch.tts_q.get(), 2)
    task.cancel()
    assert a is lv and lv.out["lv"].engine == "original" and lv.out["lv"].text == "Labdien, brāļi un māsas."
    assert b is ru and ru.out["lv"].text == "<lv> Добрый день." and calls == ["Добрый день."]   # never merged
    caps = [m for m in published if m["type"] == "caption"]
    assert caps[0]["original"] and caps[0]["src"] == "lv" and "original" not in caps[1] and caps[1]["src"] == "ru"


async def test_pipeline_bills_only_translated_languages(monkeypatch):
    """ru + lv speakers → en + lv: a Latvian chunk is billed for English only (Latvian hears the original)."""
    from app.pipeline import Pipeline

    settings = Settings(meeting={"speakers": ["ru", "lv"], "targets": ["en", "lv"], "auto_en": False})
    pipe = Pipeline.__new__(Pipeline)
    pipe.settings, pipe.records, pipe.hints, pipe._seq = settings, {}, [], 0
    pipe.source, pipe.prev_en, pipe.prev_lang, pipe.metrics = "", "", "", None
    pipe.loop = asyncio.get_running_loop()
    pipe.chunk_q, pipe.stt_ready = asyncio.Queue(), asyncio.Event()
    pipe.stt_ready.set()
    pipe.operator = SimpleNamespace(publish=lambda m: None)
    pipe.refs = SimpleNamespace(feed=lambda t: [])
    queues = {c: asyncio.Queue() for c in ("en", "lv")}
    pipe.channels = {c: SimpleNamespace(on=True, mt_q=q, scripture=SimpleNamespace(enabled=False))
                     for c, q in queues.items()}
    said = iter([("lv", "Labdien."), ("ru", "Здравствуйте.")])
    prompts = []

    class Stt:
        def detect(self, audio, choices, prev):
            assert choices == ["ru", "lv"]
            return next(said)

    def recognise(audio, prev_src):
        lang, text = Stt().detect(audio, pipe.speakers, prev_src)
        prompts.append(prev_src)
        return SimpleNamespace(text=text, ms=1.0, dropped_reason="", language=lang)

    pipe._recognise = recognise
    from concurrent.futures import ThreadPoolExecutor
    pipe.stt_exec = ThreadPoolExecutor(1)
    taps = []
    pipe.speech_taps = [lambda seq, s, langs: taps.append((seq, langs))]
    chunk = SimpleNamespace(start_s=0, duration=1, voiced_s=1.0, reason="x", t_speech_start=0, t_speech_end=0,
                            t_cut=0, audio=None)
    task = asyncio.create_task(pipe._stt_stage())
    pipe.chunk_q.put_nowait(chunk)
    pipe.chunk_q.put_nowait(chunk)
    for _ in range(50):
        if len(taps) == 2:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    assert taps == [(1, ["en"]), (2, ["en", "lv"])]
    assert [r.src for r in (queues["en"].get_nowait(), queues["en"].get_nowait())] == ["lv", "ru"]
    assert pipe.source == "ru" and pipe.speaker == "ru" and prompts == ["", "lv"]


# ------------------------------------------------------------------ glossaries by church and several sources

def test_church_split():
    assert is_lds_church("") and is_lds_church("The Church of Jesus Christ of Latter-day Saints")
    assert not is_lds_church("Baptist") and not is_lds_church("Grace Community Church")
    g = json.loads((ROOT / "glossary.ru.json").read_text())
    christian = for_church(g, "Catholic")
    en_terms = {i["en"] for i in christian["terms"]}
    assert "Relief Society" not in en_terms and "stake president" not in en_terms
    assert {"Holy Ghost", "testimony", "temple", "bishop"} <= en_terms
    assert "Nephi" not in {i["en"] for i in christian["names"]} and "Isaiah" in {i["en"] for i in christian["names"]}
    books = {i["en"] for i in christian["books"]}
    assert "1 Nephi" not in books and "Doctrine and Covenants" not in books and "John" in books
    assert for_church(g, "") is g
    assert scope_of("terms", {"en": "Relief Society", "fr": "x", "scope": "christian"}) == "christian"   # file wins


def test_a_christian_church_gets_no_latter_day_saint_wording():
    g = glossary_for("es", ["en"], {"es": json.loads((ROOT / "glossary.es.json").read_text())}, church="Methodist")
    p = build_system_prompt(g, "", "es", ["en"])
    assert "Latter-day" not in p and "a Christian church service" in p and "Sociedad de Socorro" not in p
    assert "Latter-day" in build_system_prompt(glossary_for("es", ["en"], {"es": {}}, ""), "", "es", ["en"])


def test_english_reads_every_speaker_glossary_backwards():
    ru = {"terms": [{"en": "Relief Society", "fr": "Общество милосердия"}]}
    lv = {"terms": [{"en": "Relief Society", "fr": "Palīdzības biedrība"}], "local_names": ["Bērziņš"]}
    g = glossary_for("en", ["ru", "lv"], {"ru": ru, "lv": lv, "en": {}})
    p = build_system_prompt(g, "", "en", ["ru", "lv"])
    assert "speakers talk in Russian or Latvian" in p
    assert "- Общество милосердия → Relief Society" in p and "- Palīdzības biedrība → Relief Society" in p
    assert "Bērziņš" in p


def test_another_target_gets_pivot_terms_from_each_speaker_language():
    ru = json.loads((ROOT / "glossary.ru.json").read_text())
    lv = json.loads((ROOT / "glossary.lv.json").read_text())
    g = glossary_for("lv", ["ru", "lv"], {"ru": ru, "lv": lv})
    p = build_system_prompt(g, "", "lv", ["ru", "lv"])
    assert "The speakers talk in Russian." in p          # Latvian itself is passed through, not translated
    assert "GLOSSARY (Russian → official Church Latvian)" in p
    assert "- Общество милосердия → Palīdzības biedrība" in p


# ------------------------------------------------------------------ Latvian

def test_latvian_has_a_prompt_a_licence_safe_voice_a_glossary_and_a_package():
    g = json.loads((ROOT / "glossary.lv.json").read_text())
    ru = json.loads((ROOT / "glossary.ru.json").read_text())
    for sec in ("terms", "names", "books"):
        assert [i["en"] for i in g[sec]] == [i["en"] for i in ru[sec]] and all(i["fr"] for i in g[sec])
    terms = {i["en"]: i["fr"] for i in g["terms"]}
    assert terms["The Church of Jesus Christ of Latter-day Saints"] == "Pēdējo Dienu Svēto Jēzus Kristus Baznīca"
    assert terms["Relief Society"] == "Palīdzības biedrība" and terms["bishop"] == "bīskaps"
    p = build_system_prompt(g, "", "lv", ["en"])
    assert "Your Latvian" in p and "{" not in p and "Palīdzības biedrība" in p
    assert PIPER_VOICES["lv"][1] == ("lv_LV-aivars-medium",) and SIZES_MB["lv"] == 64
    assert tts_for(Settings(), "lv").voice == "lv_LV-aivars-medium"
    assert "lv_LV-aivars-medium" in (ROOT / "THIRD_PARTY_NOTICES.md").read_text()


def test_latvian_references_are_read_as_chapter_and_verse():
    from app.tts.normalize import speechify

    assert speechify("lv", "Jāņa 3:16") == "Jāņa 3. nodaļa, 16. pants"


# ------------------------------------------------------------------ hub wire format

def test_session_start_carries_speakers_and_targets(monkeypatch):
    from app import engine

    sent = []
    pipe = SimpleNamespace(running=True, started_at=1.0, on_channels={"en": 1, "lv": 2}, speakers=["ru", "lv"],
                           speaker="lv")
    M = SimpleNamespace(S=SimpleNamespace(pipeline=pipe), legacy_speaker=lambda: "ru")
    monkeypatch.setattr(engine, "_m", lambda: M)
    c = engine.PublishClient.__new__(engine.PublishClient)
    c.meter, c.announced, c.refused_run, c.session_code, c.private = {}, False, 0.0, "", False
    c._send = sent.append
    c._save_meter = lambda force=False: None
    c.private_wanted = lambda: False

    async def one_turn():
        task = asyncio.create_task(c._publish_watch())
        await asyncio.sleep(0.6)
        pipe.speakers = ["ru", "lv", "en"]
        await asyncio.sleep(0.6)
        task.cancel()
    asyncio.run(one_turn())
    start = sent[0]
    assert start["type"] == "session_start" and start["languages"] == start["targets"] == ["en", "lv"]
    assert start["speakers"] == ["ru", "lv"] and start["speaker"] == "ru" and start["passthrough"]
    assert sent[1] == {"type": "languages", "languages": ["en", "lv"], "targets": ["en", "lv"],
                       "speakers": ["ru", "lv", "en"]}
