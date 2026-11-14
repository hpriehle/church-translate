"""The meeting's languages (docs/v3-languages.md).

A meeting has a set of speaker languages (what people may speak at the microphone: SPEAKER_LANGUAGES) and a set of
translation languages (targets: the listen pills, any of LANGUAGES). Each target gets its own translator, voice,
audio stream and captions, and receives every chunk: translated from the chunk's language, or passed through as
the original when the chunk is already in that language."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import user_file
from .glossary_scope import for_church, is_lds_church


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
    # Last: the hub link numbers the languages in this order (engine.py), so new ones are appended.
    "lv": Language("lv", "Latviešu", "none", user_file("glossary.lv.json"),
                   "Labdien. Šī ir tiešraides tulkojuma pārbaude. Ja jūs mani dzirdat, viss darbojas."),
}

# English names, for the interpreters' instructions.
NAMES_EN = {"en": "English", "fr": "French", "es": "Spanish", "ru": "Russian", "pt": "Portuguese", "uk": "Ukrainian",
            "fa": "Persian", "ne": "Nepali", "vi": "Vietnamese", "zh": "Chinese", "wo": "Wolof", "lv": "Latvian"}

# What people can speak at the microphone: speech recognition (Whisper) knows these. Not Wolof: Whisper doesn't.
SPEAKER_LANGUAGES: dict[str, str] = {c: l.name for c, l in LANGUAGES.items() if c != "wo"}

# Legacy (settings without a "meeting" section, the 0.2 Mac app): English follows the speaker, on whenever some
# speaker language isn't English. See config.MeetingCfg.auto_en.
AUTO = {"en"}


def _sources(code: str, sources: str | list[str] | tuple[str, ...]) -> list[str]:
    """The speaker languages that `code` is translated from (its own language is passed through, not translated)."""
    srcs = [sources] if isinstance(sources, str) else list(sources or [])
    return [s for s in dict.fromkeys(srcs) if s and s != code]


def glossary_for(code: str, sources: str | list[str] | tuple[str, ...], glossaries: dict,
                 church: str | None = "") -> dict:
    """The glossary a language's interpreter gets, for a meeting whose speakers talk in `sources`.

    - Only the church's entries (glossary_scope: Latter-day Saints get Christian + LDS, others Christian only).
    - English reads each other speaker language's glossary the other way round (its "fr" side holds the official
      terms in that language).
    - Any other language keeps its own glossary (English → it) and gets "pivot" pairs from each non-English
      speaker language, joined on the English key (e.g. Russian → Latvian).
    The congregation's local names come from all of them."""
    srcs = _sources(code, sources)
    own = for_church(glossaries.get(code) or {}, church)
    meta = {"_sources": srcs, "_church": "lds" if is_lds_church(church) else "christian"}
    others = [s for s in srcs if s != "en" and glossaries.get(s)]
    local = [n for g in [own, *(glossaries[s] for s in others)] for n in (g.get("local_names") or []) if n]
    local = list(dict.fromkeys(local))
    if code == "en":
        if not others:
            return {**own, **meta, "local_names": local}
        merged: dict[str, list] = {"terms": [], "names": [], "books": []}
        for s in others:
            g = for_church(glossaries[s] or {}, church)
            for section, items in merged.items():
                seen = {(i.get("en"), i.get("fr")) for i in items}
                items += [i for i in g.get(section) or [] if (i.get("en"), i.get("fr")) not in seen]
        return {**merged, **meta, "local_names": local}
    pivot: dict[str, list[dict[str, str]]] = {}
    mine = {i.get("en"): i.get("fr") for sec in ("terms", "names", "books") for i in own.get(sec) or []
            if i.get("en") and i.get("fr")}
    for s in others:
        g = for_church(glossaries[s] or {}, church)
        pairs = [{"src": i["fr"], "dst": mine[i["en"]]} for sec in ("terms", "names", "books")
                 for i in g.get(sec) or [] if i.get("en") in mine and i.get("fr") and i["fr"] != mine[i["en"]]]
        if pairs:
            pivot[s] = pairs
    return {**own, **meta, "local_names": local, "pivot": pivot}


def enabled(codes: list[str]) -> list[Language]:
    out = [LANGUAGES[c] for c in codes if c in LANGUAGES]
    return out or [LANGUAGES["fr"]]
