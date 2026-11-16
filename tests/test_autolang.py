"""A language spoken that isn't a speaker language (docs/riga-metering-2026-11-15.md): never forced into one (that
was Whisper's free rough translation, passed through unbilled), recognised as itself, translated and metered, and
added to the meeting's speaker languages once heard for sure. The operator can take it out again."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from app.config import Settings
from app.health import HealthCollector
from app.languages import SPEAKER_LANGUAGES
from app.pipeline import Pipeline
from app.stt import AutoLanguage, SttResult

RU = {"ru": 0.93, "uk": 0.04, "en": 0.02}
EN = {"en": 0.95, "ru": 0.01}
ODD = {"lv": 0.7, "en": 0.25}     # one chunk the detector gets wrong


def _auto():
    return AutoLanguage(set(SPEAKER_LANGUAGES))


# ------------------------------------------------------------------ the decision, chunk by chunk

def test_russian_in_an_english_only_meeting_is_recognised_as_russian_and_added_on_the_second_chunk():
    a = _auto()
    first = a.decide(RU, ["en"], "")
    assert (first.language, first.detected, first.add) == ("ru", "ru", "")     # not forced to English
    second = a.decide(RU, ["en"], "ru")
    assert (second.language, second.add) == ("ru", "ru") and a.added == ["ru"]
    assert a.decide(RU, ["en", "ru"], "ru").add == ""                           # added once


def test_genuine_english_stays_english():
    a = _auto()
    for _ in range(5):
        d = a.decide(EN, ["en"], "en")
        assert (d.language, d.detected, d.add) == ("en", "en", "")
    assert a.added == []


def test_one_odd_chunk_changes_nothing_in_the_set():
    a = _auto()
    seq = [EN, ODD, EN, ODD, EN]
    prev = "en"
    for probs in seq:
        d = a.decide(probs, ["en"], prev)
        assert d.add == ""
        prev = d.language
    assert a.added == []


def test_a_close_call_keeps_the_speaker_language():
    """The speaker languages are a prior: a language outside them must be clearly more likely."""
    d = _auto().decide({"ru": 0.5, "en": 0.4}, ["en"], "en")
    assert d.language == "en" and d.detected == "ru"


def test_a_language_hear_church_cant_offer_is_recognised_as_itself_but_not_added():
    a = _auto()
    for prev in ("", "de", "de"):
        d = a.decide({"de": 0.9, "en": 0.05}, ["en"], prev)
        assert d.language == "de" and d.add == ""


def test_undo_is_respected_for_the_rest_of_the_meeting():
    a = _auto()
    a.decide(RU, ["en"], "")
    assert a.decide(RU, ["en"], "ru").add == "ru"
    a.removed("ru")                                  # the operator took it out
    for _ in range(4):
        d = a.decide(RU, ["en"], "ru")
        assert d.language == "ru" and d.add == ""    # still recognised (and paid) as Russian, not added again
    a.reset()                                        # next meeting
    a.decide(RU, ["en"], "")
    assert a.decide(RU, ["en"], "ru").add == "ru"


def test_no_detector_keeps_the_old_choice():
    assert _auto().decide({}, ["en"], "").language == "en"
    assert _auto().decide({}, ["ru", "lv"], "lv").language == "lv"


# ------------------------------------------------------------------ the pipeline: translated, metered, counted

class FakeStt:
    """The detector says what `said` says; transcription returns text in the language it is told to use."""
    engine, stepped_down = "mlx", ""

    def __init__(self, said):
        self.said = iter(said)
        self.told: list[str] = []
        self.now = {}

    def language_probs(self, audio):
        self.now = next(self.said)
        return self.now

    def transcribe(self, audio, prompt="", language=None):
        self.told.append(language)
        return SttResult(text=f"words in {language}", ms=1.0, language=language)


async def _run(said, speakers=("en",), targets=("en", "fr")):
    settings = Settings(meeting={"speakers": list(speakers), "targets": list(targets), "auto_en": False})
    pipe = Pipeline.__new__(Pipeline)
    pipe.settings, pipe.records, pipe.hints, pipe._seq = settings, {}, [], 0
    pipe.source, pipe.prev_en, pipe.prev_lang, pipe.metrics = "", "", "", None
    pipe.meeting_id = "m1"
    pipe.loop = asyncio.get_running_loop()
    pipe.chunk_q, pipe.stt_ready = asyncio.Queue(), asyncio.Event()
    pipe.stt_ready.set()
    pipe.operator = SimpleNamespace(publish=lambda m: None)
    pipe.refs = SimpleNamespace(feed=lambda t: [])
    pipe.channels = {c: SimpleNamespace(on=True, mt_q=asyncio.Queue(), scripture=SimpleNamespace(enabled=False))
                     for c in targets}
    pipe.stt = FakeStt(said)
    pipe.stt_exec = ThreadPoolExecutor(1)
    pipe.autolang = _auto()
    pipe.health = HealthCollector()
    added: list[str] = []

    def add(code):   # what main.auto_add_speaker does to the pipeline: a new speaker set
        added.append(code)
        pipe.settings.meeting.speakers = pipe.settings.meeting.speakers + [code]
    pipe.on_new_speaker = add
    taps: list = []
    pipe.speech_taps = [lambda seq, s, langs: taps.append(langs)]
    chunk = SimpleNamespace(start_s=0, duration=2, voiced_s=2.0, reason="x", t_speech_start=0, t_speech_end=0,
                            t_cut=0, audio=None, meeting="")
    task = asyncio.create_task(pipe._stt_stage())
    for _ in said:
        pipe.chunk_q.put_nowait(chunk)
    for _ in range(100):
        if len(taps) == len(said):
            break
        await asyncio.sleep(0.02)
    task.cancel()
    return pipe, taps, added


async def test_forced_english_meeting_with_russian_speech_is_translated_and_metered():
    pipe, taps, added = await _run([RU, RU, RU])
    assert pipe.stt.told == ["ru", "ru", "ru"]                    # Whisper never told "English" for Russian speech
    assert taps == [["en", "fr"]] * 3                              # English listeners get a paid translation
    assert added == ["ru"] and pipe.speakers == ["en", "ru"]
    srcs = [pipe.channels["en"].mt_q.get_nowait().src for _ in range(3)]
    assert srcs == ["ru", "ru", "ru"]                              # so the English channel translates, not passes
    h = pipe.health.take()
    assert h["asr"]["langs"] == {"ru": {"ru": 3}} and h["asr"]["outside_set"] == 2   # third: ru was in the set


async def test_genuine_english_is_passed_through_free():
    pipe, taps, added = await _run([EN, EN, EN])
    assert pipe.stt.told == ["en", "en", "en"]
    assert taps == [["fr"]] * 3 and added == []                   # English listeners hear the original: not billed
    h = pipe.health.take()
    assert h["asr"]["langs"] == {"en": {"en": 3}} and h["asr"]["outside_set"] == 0


async def test_one_odd_chunk_doesnt_flap_the_speaker_set():
    pipe, taps, added = await _run([EN, ODD, EN, EN])
    assert added == [] and pipe.speakers == ["en"]
    assert taps[0] == taps[2] == taps[3] == ["fr"]


# ------------------------------------------------------------------ main: the operator is told and can undo

def _main(monkeypatch, speakers=("en",)):
    from app import main

    settings = Settings(meeting={"speakers": list(speakers), "targets": ["en", "fr"], "auto_en": False})
    pipe = Pipeline.__new__(Pipeline)
    pipe.settings, pipe.channels, pipe.source, pipe.glossaries = settings, {}, "", {}
    pipe.autolang = _auto()
    pipe.apply_settings = lambda st, g: None
    told: list = []
    monkeypatch.setattr(main.S, "pipeline", pipe, raising=False)
    monkeypatch.setattr(main.S, "settings", settings, raising=False)
    monkeypatch.setattr(main.S, "glossaries", {}, raising=False)
    monkeypatch.setattr(main.S, "operator", SimpleNamespace(publish=told.append), raising=False)
    monkeypatch.setattr(main.S, "engine_hub", SimpleNamespace(set_languages=lambda c: None), raising=False)
    monkeypatch.setattr(main, "full_status", lambda: {"type": "status"})
    monkeypatch.setattr(main, "save_override", lambda k, v: None)
    return main, pipe, told


def test_auto_added_language_is_shown_and_undo_takes_it_out(monkeypatch):
    main, pipe, told = _main(monkeypatch)
    pipe.autolang.decide(RU, ["en"], "")
    code = pipe.autolang.decide(RU, ["en"], "ru").add
    main.auto_add_speaker(code)
    assert main.S.settings.meeting.speakers == ["en", "ru"]
    assert {"type": "log", "level": "warning", "text": "Russian detected: added to speaker languages"} in told
    assert {"type": "speaker_added", "code": "ru", "name": "Russian"} in told
    assert [c for c in pipe.autolang.added if c in main.S.settings.meeting.speakers] == ["ru"]   # status.auto_speakers

    main.set_speakers(["en"])                         # Undo in the app = taking it out of the speaker languages
    assert main.S.settings.meeting.speakers == ["en"]
    assert pipe.autolang.added == [] and "ru" in pipe.autolang.dismissed
    assert pipe.autolang.decide(RU, ["en"], "ru").add == ""


def test_the_operator_can_set_several_speaker_languages(monkeypatch):
    main, pipe, told = _main(monkeypatch)
    main.set_speakers(["en", "ru", "lv"])
    assert main.S.settings.meeting.speakers == ["en", "ru", "lv"]
    main.set_speakers(["ru"])
    assert main.S.settings.meeting.speakers == ["ru"]
