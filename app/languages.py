"""Output languages. Each enabled language gets its own translator, voice, audio stream and captions.

The speaker talks in one of SPEAKER_LANGUAGES (switched live next to Start meeting). English is an output
language only while the speaker isn't speaking English; the output in the speaker's own language pauses."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import user_file


@dataclass(frozen=True)
class Language:
    code: str
    name: str            # as listeners see it in the picker
    fallback: str        # "ollama" = local model if Claude fails; "none" = Claude only
    glossary_file: Path
    test_sentence: str   # "Play a test sentence" on the operator page


LANGUAGES: dict[str, Language] = {
    "fr": Language("fr", "Français", "ollama", user_file("glossary.json"),
                   "Bonjour. Ceci est un essai de la traduction en direct. Si vous m'entendez, tout fonctionne."),
    # The local fallback model's Wolof is poor, so Wolof relies on Claude only.
    "wo": Language("wo", "Wolof", "none", user_file("glossary.wo.json"),
                   "Salaam aleekum. Lii ab natt la ngir xool ndax tekki bi dafay dox. Su ngeen ma déggee, lépp baax na."),
    "es": Language("es", "Español", "none", user_file("glossary.es.json"),
                   "Hola. Esta es una prueba de la traducción en directo. Si me escuchan, todo funciona bien."),
    "ru": Language("ru", "Русский", "none", user_file("glossary.ru.json"),
                   "Здравствуйте. Это проверка синхронного перевода. Если вы меня слышите, всё работает."),
    "pt": Language("pt", "Português", "none", user_file("glossary.pt.json"),
                   "Olá. Este é um teste da tradução ao vivo. Se vocês estão me ouvindo, tudo está funcionando."),
    "uk": Language("uk", "Українська", "none", user_file("glossary.uk.json"),
                   "Вітаю. Це перевірка синхронного перекладу. Якщо ви мене чуєте, усе працює."),
    "fa": Language("fa", "فارسی", "none", user_file("glossary.fa.json"),
                   "سلام. این یک آزمایش ترجمه زنده است. اگر صدای مرا می‌شنوید، همه چیز درست کار می‌کند."),
    "ne": Language("ne", "नेपाली", "none", user_file("glossary.ne.json"),
                   "नमस्ते। यो प्रत्यक्ष अनुवादको परीक्षण हो। यदि तपाईंले मलाई सुन्न सक्नुहुन्छ भने सबै ठीक छ।"),
    "vi": Language("vi", "Tiếng Việt", "none", user_file("glossary.vi.json"),
                   "Xin chào. Đây là bài kiểm tra dịch trực tiếp. Nếu anh chị em nghe được tôi thì mọi thứ đều hoạt động tốt."),
    "zh": Language("zh", "中文", "none", user_file("glossary.zh.json"),
                   "大家好。这是实时翻译的测试。如果您能听到我的声音，说明一切正常。"),
    # Shares the French glossary, read the other way round (official Church French → English).
    "en": Language("en", "English", "ollama", user_file("glossary.json"),
                   "Hello. This is a test of the live translation. If you can hear me, everything is working."),
}

# What the speaker can talk in: speech recognition (Whisper) knows these. Not Wolof: Whisper doesn't.
SPEAKER_LANGUAGES: dict[str, str] = {"en": "English", "fr": "Français", "es": "Español", "ru": "Русский"}
SPEAKER_NAMES_EN = {"en": "English", "fr": "French", "es": "Spanish", "ru": "Russian"}

# English follows the speaker: it is translated whenever the speaker isn't speaking English.
AUTO = {"en"}


def glossary_for(code: str, speaker: str, glossaries: dict) -> dict:
    """The glossary a language's interpreter gets. English, translated from another speaker language, reads that
    language's glossary the other way round (its "fr" side holds the official terms in that language), keeping the
    congregation's local names from both."""
    own = glossaries.get(code) or {}
    if code != "en" or speaker == "en" or speaker not in glossaries:
        return own
    src = glossaries[speaker] or {}
    local = list(dict.fromkeys([n for n in (src.get("local_names") or []) + (own.get("local_names") or []) if n]))
    return {**src, "local_names": local}


def enabled(codes: list[str]) -> list[Language]:
    out = [LANGUAGES[c] for c in codes if c in LANGUAGES]
    return out or [LANGUAGES["fr"]]
