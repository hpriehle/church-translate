"""Spanish and Russian as languages listeners can choose."""
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
