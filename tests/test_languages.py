"""Spanish and Russian as languages listeners can choose."""
import asyncio
import json
from pathlib import Path

import pytest

from app.config import Settings, tts_for
from app.languages import LANGUAGES
from app.packages import PIPER_VOICES, SIZES_MB, _piper_files, package_for
from app.translate.prompts import build_system_prompt
from app.tts.normalize import speechify

ROOT = Path(__file__).parent.parent


@pytest.mark.parametrize("code,word,term", [("es", "Spanish", "Sociedad de Socorro"), ("ru", "Russian", "Общество милосердия")])
def test_prompt_is_in_the_listeners_language_and_uses_its_glossary(code, word, term):
    g = json.loads((ROOT / f"glossary.{code}.json").read_text())
    p = build_system_prompt(g, "", code, "en")
    assert f"Your {word} is shown" in p and term in p and "French" not in p


def test_prompt_follows_a_speaker_who_is_not_speaking_english():
    g = json.loads((ROOT / "glossary.es.json").read_text())
    assert "The speakers talk in French" in build_system_prompt(g, "", "es", "fr")


@pytest.mark.parametrize("code", ["es", "ru"])
def test_language_has_a_downloadable_voice(code):
    assert code in LANGUAGES
    pkg = package_for(code, "any")
    assert pkg and pkg.size_mb == SIZES_MB[code] and pkg.piper_voices == PIPER_VOICES[code][1]
    cfg = tts_for(Settings(), code)
    assert cfg.engine == "piper" and cfg.lang == code and cfg.voice in pkg.piper_voices


def test_voice_files_keep_their_full_names():
    url, dest = _piper_files("es_MX-claude-high")[0]
    assert url.endswith("/es/es_MX/claude/high/es_MX-claude-high.onnx") and dest.name == "es_MX-claude-high.onnx"
    url, dest = _piper_files("fr_FR-tom-medium")[1]
    assert url.endswith("/fr/fr_FR/tom/medium/fr_FR-tom-medium.onnx.json") and dest.name == "fr_FR-tom-medium.onnx.json"


def test_scripture_references_are_read_as_chapter_and_verse():
    assert speechify("es", "Juan 3:16") == "Juan capítulo 3, versículo 16"
    assert speechify("ru", "От Иоанна 3:16") == "От Иоанна глава 3, стих 16"


@pytest.mark.parametrize("code", ["es", "ru"])
def test_glossary_covers_every_scripture_book(code):
    books = json.loads((ROOT / f"glossary.{code}.json").read_text())["books"]
    french = json.loads((ROOT / "glossary.json").read_text())["books"]
    assert [b["en"] for b in books] == [b["en"] for b in french] and all(b["fr"] for b in books)


async def _downloaded(monkeypatch, tmp_path):
    from app import packages

    monkeypatch.setattr(packages, "PIPER_DIR", tmp_path)
    monkeypatch.setattr(packages, "_hf_cached", lambda repo: True)

    async def fake_fetch(url, dest):
        dest.write_text("x")

    monkeypatch.setattr(packages, "_fetch", fake_fetch)
    monkeypatch.setattr("huggingface_hub.snapshot_download", lambda repo, **kw: None)
    p = packages.Packages()
    await p.status(["es", "ru"])
    p.download_missing(["es", "ru"])
    await asyncio.gather(*(x.task for x in p.items.values() if x.task))
    return p


async def test_removing_a_language_deletes_only_its_voices(monkeypatch, tmp_path):
    p = await _downloaded(monkeypatch, tmp_path)
    await p.remove("es")
    assert not list(tmp_path.glob("es_*")) and len(list(tmp_path.glob("ru_*"))) == 4
    st = {x["id"]: x["state"] for x in await p.status(["es", "ru"])}
    assert st == {"speech": "ready", "es": "missing", "ru": "ready"}


async def test_a_removed_language_can_be_downloaded_again(monkeypatch, tmp_path):
    p = await _downloaded(monkeypatch, tmp_path)
    await p.remove("es")
    await p.status(["es"])
    p.download_missing(["es"])
    await asyncio.gather(*(x.task for x in p.items.values() if x.task))
    assert {x["id"]: x["state"] for x in await p.status(["es"])}["es"] == "ready"


async def test_speech_recognition_and_running_downloads_cannot_be_removed(monkeypatch, tmp_path):
    p = await _downloaded(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        await p.remove("speech")
    with pytest.raises(ValueError):
        await p.remove("nope")
    p.items["ru"].state = "downloading"
    with pytest.raises(ValueError, match="still downloading"):
        await p.remove("ru")
    assert len(list(tmp_path.glob("ru_*"))) == 4


async def test_a_voice_comes_back_after_being_removed():
    from app.channel import Channel
    from app.tts.engines import TtsRunner

    tts = TtsRunner(tts_for(Settings(), "ru"), 44100, lazy=True)
    loads = []

    async def fake_load():
        loads.append(1)
        tts.engine = object()

    tts.load = fake_load
    ch = Channel.__new__(Channel)
    ch.tts, ch._started, ch._tasks = tts, True, []
    tts.engine = object()
    ch.activate()
    assert not ch._tasks       # already loaded: nothing to do
    tts.unload()
    assert tts.engine is None and not tts.ready.is_set()
    ch.activate()
    await asyncio.gather(*ch._tasks)
    assert loads == [1] and tts.engine is not None


class _Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def _engine(monkeypatch, on, running=False):
    from types import SimpleNamespace as NS

    from app import main, packages

    removed, unloaded, switched = [], [], []

    async def remove(code):
        removed.append(code)

    channels = {c: NS(on=c in on, lang=LANGUAGES[c], tts=NS(unload=lambda c=c: unloaded.append(c))) for c in ("fr", "es")}
    monkeypatch.setattr(main, "S", NS(pipeline=NS(channels=channels, running=running), settings=NS(languages=list(on))))
    monkeypatch.setattr(main, "require_operator", lambda request: None)
    monkeypatch.setattr(main, "set_languages", lambda codes: switched.append(codes))
    monkeypatch.setattr(packages.PACKAGES, "remove", remove)
    return main, removed, unloaded, switched


async def test_the_only_language_on_can_still_be_removed(monkeypatch):
    main, removed, unloaded, switched = _engine(monkeypatch, ["fr"])
    assert (await main.api_packages_remove(_Request({"language": "fr"})))["ok"]
    assert removed == ["fr"] and unloaded == ["fr"] and switched == [[]]


async def test_a_language_being_translated_in_a_meeting_cannot_be_removed(monkeypatch):
    from fastapi import HTTPException

    main, removed, _, _ = _engine(monkeypatch, ["fr", "es"], running=True)
    with pytest.raises(HTTPException):
        await main.api_packages_remove(_Request({"language": "es"}))
    assert not removed


async def test_removing_a_language_switches_it_off_and_frees_its_voice(monkeypatch):
    main, removed, unloaded, switched = _engine(monkeypatch, ["fr", "es"])
    assert (await main.api_packages_remove(_Request({"language": "es"})))["ok"]
    assert removed == ["es"] and unloaded == ["es"] and switched == [["fr"]]


NEW = ["pt", "uk", "fa", "ne", "vi", "zh"]


@pytest.mark.parametrize("code", NEW)
def test_new_language_has_a_prompt_a_voice_and_a_glossary_file(code):
    g = json.loads((ROOT / f"glossary.{code}.json").read_text())
    p = build_system_prompt(g, "", code, "en")
    assert "Your" in p and "French" not in p and "{" not in p
    pkg = package_for(code, "any")
    cfg = tts_for(Settings(), code)
    assert code in LANGUAGES and pkg.size_mb == SIZES_MB[code] and cfg.engine == "piper" and cfg.voice in pkg.piper_voices


def test_macos_voices_saved_earlier_are_replaced():
    s = Settings.model_validate({"tts": {"engine": "say", "voice": "Thomas"},
                                 "voices": {"en": {"engine": "say", "voice": "Samantha"}}})
    assert tts_for(s, "fr").engine == "piper" and tts_for(s, "fr").voice == "fr_FR-tom-medium"
    assert tts_for(s, "en").engine == "piper" and tts_for(s, "en").voice == "en_US-joe-medium"
    assert tts_for(Settings(), "en").engine == "piper"


def test_chinese_reference_reads_as_chapter_and_verse():
    assert speechify("zh", "约翰福音3:16") == "约翰福音第3章第16节"


def test_voice_names_are_people_never_system_names():
    from app.tts import engines as E

    assert E.voice_label("ru", "ru_RU-denis-medium") == "Male 1 - Ivan"
    assert E.voice_label("ru", "ru_RU-dmitri-medium") == "Male 2 - Alexei"
    assert E.voice_label("fr", "fr_FR-siwis-medium") == "Female 1 - Camille"
    for lang, people in E._VOICE_PEOPLE.items():
        for voice, _, _ in people:
            label = E.voice_label(lang, voice)
            assert label != "Voice" and "piper" not in label.lower() and voice not in label


@pytest.mark.parametrize("code", ["es", "ru", "pt", "uk", "fa", "ne", "vi", "zh", "en", "fr"])
def test_every_downloadable_voice_has_a_name(code):
    from app.tts import engines as E

    assert all(E.voice_label(code, v) != "Voice" for v in PIPER_VOICES[code][1])


def test_every_voice_on_a_language_screen_has_a_built_in_sample():
    from app.tts.engines import voice_catalog
    from mac.make_voice_samples import LANGUAGE_SCREENS, OUT

    catalog = voice_catalog()
    for code in LANGUAGE_SCREENS:
        assert catalog[code], code
        for option in catalog[code]:
            assert (OUT / f"sample-{code}-{option['voice']}.m4a").is_file(), option["label"]


def test_the_paid_mac_app_never_offers_the_non_commercial_wolof_voice(monkeypatch):
    from app.config import Settings, VoiceCfg, tts_for
    from app.tts.engines import voice_catalog

    assert all(o["engine"] != "mms" for o in voice_catalog()["wo"])
    monkeypatch.setattr("app.config.sys.platform", "darwin")
    cfg = tts_for(Settings(voices={"wo": VoiceCfg(engine="mms", voice="default", speed=1.25)}), "wo")
    assert (cfg.engine, cfg.speed) == ("speecht5", 1.25)
