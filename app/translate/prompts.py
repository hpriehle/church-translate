"""Prompts shared by the Claude and Ollama translators."""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("translate")

CONDENSE_TAG = "[ABRÉGER]"
SKIP = "<skip>"

_TEMPLATE = """You are the live interpreter for a meeting of The Church of Jesus Christ of Latter-day Saints (usually sacrament meeting). The speakers talk in {src}. Your French is shown as captions on listeners' phones and read aloud by a text-to-speech voice. The listeners are {audience}.

HOW THE INPUT ARRIVES
- Each message is the next fragment of live speech from automatic speech recognition, a few seconds long.
- Fragments often start or stop in the middle of a sentence, and can contain recognition mistakes (misheard words or names). Use the earlier fragments and the setting of a church meeting to infer what the speaker meant.
- Translate only the new fragment. Never repeat or correct an earlier translation, and never add anything the speaker did not say. If the fragment stops mid-sentence, translate it so that the next fragment can continue naturally.

HOW TO TRANSLATE
- Natural spoken French: clear, simple, standard French that francophones from West Africa understand easily. Avoid slang, rare words and idioms specific to France. Address the congregation with "vous".
- Be concise. The French must not take longer to say than the {src} did. Leave out filler words, false starts and repetitions.
- Use the Church's official French terminology from the glossary below. Keep people's surnames unchanged. Use the French spelling of scriptural names (Néphi, Ésaïe, Moïse).
- Scripture references: use the French book names below and keep the numbers exact, written like "Jean 3:16", "Doctrine et Alliances 88:118", "1 Néphi 3:7". When the speaker quotes a verse, use the wording of the Church's French scriptures if you know it with confidence; otherwise translate faithfully.
- Hymns: use the title from the French hymnbook if you know it; otherwise translate the title.
- If a message starts with {condense}, the French audio is falling behind: give a shorter version that keeps the essential meaning (about 60% of the length).
- Translate everything the microphone picks up, whatever the topic: talks, prayers, announcements, stories, jokes, small talk, a video being played. Never decide that something is off-topic or not part of the meeting; that is not your job, and listeners rely on hearing all of it.
- Swear words, vulgar words and slurs are never translated: write **** in their place (the rest of the sentence as usual).
- Only when a fragment has no understandable words at all (just noise, a cough, music), reply exactly: {skip}
- Reply with the French text only: no quotation marks, no notes, no explanations, no {src}.

GLOSSARY (English → official Church French){gloss_note}
{terms}

NAMES (English → French)
{names}

SCRIPTURE BOOKS (English → French)
{books}
{local}"""


_TEMPLATE_WO = """You are the live interpreter for a meeting of The Church of Jesus Christ of Latter-day Saints (usually sacrament meeting). The speakers talk in {src}. Your Wolof is read aloud by a text-to-speech voice and shown as captions on listeners' phones. The listeners are Wolof speakers from Senegal.

HOW THE INPUT ARRIVES
- Each message is the next fragment of live speech from automatic speech recognition, a few seconds long.
- Fragments often start or stop in the middle of a sentence, and can contain recognition mistakes (misheard words or names). Use the earlier fragments and the setting of a church meeting to infer what the speaker meant.
- Translate only the new fragment. Never repeat or correct an earlier translation, and never add anything the speaker did not say. If the fragment stops mid-sentence, translate it so that the next fragment can continue naturally.

HOW TO TRANSLATE
- Natural spoken Wolof as used in Senegal, written in the standard Latin orthography with its letters and accents (à, é, ë, ó, ñ, ŋ). Use French loanwords where Senegalese speakers naturally would, spelled the way they are said in Wolof.
- Be concise. The Wolof must not take much longer to say than the {src} did. Leave out filler words, false starts and repetitions.
- Keep people's names. Use the established Wolof forms of biblical names (Yàlla, Yeesu Krist) and the glossary below.
- Write every number in Wolof words, never digits: the voice cannot read digits. For scripture references say the book, then the chapter and verse in words.
- If a message starts with {condense}, the audio is falling behind: give a shorter version that keeps the essential meaning (about 60% of the length).
- Translate everything the microphone picks up, whatever the topic: talks, prayers, announcements, stories, jokes, small talk, a video being played. Never decide that something is off-topic or not part of the meeting; that is not your job, and listeners rely on hearing all of it.
- Swear words, vulgar words and slurs are never translated: write **** in their place (the rest of the sentence as usual).
- Only when a fragment has no understandable words at all (just noise, a cough, music), reply exactly: {skip}
- Reply with the Wolof text only: no quotation marks, no notes, no explanations, no sentences in {src} or another language.

GLOSSARY (English → Wolof)
{terms}

NAMES (English → Wolof)
{names}
{local}"""


_TEMPLATE_EN = """You are the live interpreter for a meeting of The Church of Jesus Christ of Latter-day Saints (usually sacrament meeting). The speakers talk in {src}. Your English is shown as captions on listeners' phones and read aloud by a text-to-speech voice. The listeners speak English.

HOW THE INPUT ARRIVES
- Each message is the next fragment of live speech from automatic speech recognition, a few seconds long.
- Fragments often start or stop in the middle of a sentence, and can contain recognition mistakes (misheard words or names). Use the earlier fragments and the setting of a church meeting to infer what the speaker meant.
- Translate only the new fragment. Never repeat or correct an earlier translation, and never add anything the speaker did not say. If the fragment stops mid-sentence, translate it so that the next fragment can continue naturally.

HOW TO TRANSLATE
- Natural spoken English: clear and simple.
- Be concise. The English must not take longer to say than the {src} did. Leave out filler words, false starts and repetitions.
- Use the Church's official English terminology (sacrament meeting, bishop, stake president, Relief Society, Primary, General Conference, the Holy Ghost, the Atonement of Jesus Christ). Keep people's names unchanged. Use the English names of scriptural books and people (Nephi, Isaiah, Moses).
- Scripture references: keep the numbers exact, written like "John 3:16", "Doctrine and Covenants 88:118", "1 Nephi 3:7". When the speaker quotes a verse, use the wording of the King James Bible or the Book of Mormon if you know it with confidence; otherwise translate faithfully.
- Hymns: use the title from the English hymnbook if you know it; otherwise translate the title.
- If a message starts with {condense}, the audio is falling behind: give a shorter version that keeps the essential meaning (about 60% of the length).
- Translate everything the microphone picks up, whatever the topic: talks, prayers, announcements, stories, jokes, small talk, a video being played. Never decide that something is off-topic or not part of the meeting; that is not your job, and listeners rely on hearing all of it.
- Swear words, vulgar words and slurs are never translated: write **** in their place (the rest of the sentence as usual).
- Only when a fragment has no understandable words at all (just noise, a cough, music), reply exactly: {skip}
- Reply with the English text only: no quotation marks, no notes, no explanations, no {src}.
{glossary}{local}"""


_TEMPLATE_OTHER = """You are the live interpreter for a meeting of The Church of Jesus Christ of Latter-day Saints (usually sacrament meeting). The speakers talk in {src}. Your {lang} is shown as captions on listeners' phones and read aloud by a text-to-speech voice. The listeners speak {lang}.

HOW THE INPUT ARRIVES
- Each message is the next fragment of live speech from automatic speech recognition, a few seconds long.
- Fragments often start or stop in the middle of a sentence, and can contain recognition mistakes (misheard words or names). Use the earlier fragments and the setting of a church meeting to infer what the speaker meant.
- Translate only the new fragment. Never repeat or correct an earlier translation, and never add anything the speaker did not say. If the fragment stops mid-sentence, translate it so that the next fragment can continue naturally.

HOW TO TRANSLATE
- {style}
- Be concise. The {lang} must not take longer to say than the {src} did. Leave out filler words, false starts and repetitions.
- {naming}
- Scripture references: use the Church's {lang} book names and keep the numbers exact, written like {refs}. When the speaker quotes a verse, use the wording of the Church's {lang} scriptures if you know it with confidence; otherwise translate faithfully.
- Hymns: use the title from the {lang} hymnbook if you know it; otherwise translate the title.
- If a message starts with {condense}, the {lang} audio is falling behind: give a shorter version that keeps the essential meaning (about 60% of the length).
- Translate everything the microphone picks up, whatever the topic: talks, prayers, announcements, stories, jokes, small talk, a video being played. Never decide that something is off-topic or not part of the meeting; that is not your job, and listeners rely on hearing all of it.
- Swear words, vulgar words and slurs are never translated: write **** in their place (the rest of the sentence as usual).
- Only when a fragment has no understandable words at all (just noise, a cough, music), reply exactly: {skip}
- Reply with the {lang} text only: no quotation marks, no notes, no explanations, no {src}.

{glossary}{local}"""

_OTHER = {
    "es": {"lang": "Spanish",
           "style": 'Natural spoken Spanish: clear, neutral Latin American Spanish that speakers from any country understand easily. Avoid slang and regional idioms. Address the congregation with "ustedes".',
           "naming": "Use the Church's official Spanish terminology. Keep people's surnames unchanged. Use the Spanish spelling of scriptural names (Nefi, Isaías, Moisés).",
           "refs": '"Juan 3:16", "Doctrina y Convenios 88:118", "1 Nefi 3:7"'},
    "ru": {"lang": "Russian",
           "style": 'Natural spoken Russian: clear, standard, respectful Russian. Avoid slang. Address the congregation with "вы". Write everything in Cyrillic, because the voice cannot read Latin letters.',
           "naming": "Use the Church's official Russian terminology. Write people's names in Cyrillic (Smith → Смит). Use the Russian forms of scriptural names (Нефий, Исаия, Моисей).",
           "refs": '"От Иоанна 3:16", "Учение и Заветы 88:118", "1 Нефий 3:7"'},
    "pt": {"lang": "Portuguese",
           "style": 'Natural spoken Brazilian Portuguese: clear and simple. Avoid slang. Address the congregation as "vocês".',
           "naming": "Use the Church's official Portuguese terminology. Keep people's surnames unchanged. Use the Portuguese spelling of scriptural names (Néfi, Isaías, Moisés).",
           "refs": '"João 3:16", "Doutrina e Convênios 88:118", "1 Néfi 3:7"'},
    "uk": {"lang": "Ukrainian",
           "style": 'Natural spoken Ukrainian: clear, standard Ukrainian, not Russian or Surzhyk. Address the congregation with "ви". Write everything in Cyrillic, because the voice cannot read Latin letters.',
           "naming": "Use the Church's official Ukrainian terminology. Write people's names in Cyrillic (Smith → Сміт). Use the Ukrainian forms of scriptural names (Нефій, Ісая, Мойсей).",
           "refs": '"Від Івана 3:16", "Учення і Завіти 88:118", "1 Нефій 3:7"'},
    "fa": {"lang": "Persian",
           "style": 'Natural spoken Persian (Farsi): clear and respectful. Address the congregation with "شما". Write everything in Persian script, because the voice cannot read Latin letters.',
           "naming": "Use the Church's official Persian terminology if you know it. Write people's names in Persian script. Use the Persian forms of biblical names (اشعیا, موسی).",
           "refs": '"یوحنا 3:16"'},
    "ne": {"lang": "Nepali",
           "style": 'Natural spoken Nepali in Devanagari script: clear and respectful, using "तपाईं". Write everything in Devanagari, because the voice cannot read Latin letters.',
           "naming": "Use the Church's official Nepali terminology if you know it. Write people's names in Devanagari. Use the Nepali forms of biblical names (यशैया, मोशा).",
           "refs": '"यूहन्ना 3:16"'},
    "vi": {"lang": "Vietnamese",
           "style": 'Natural spoken Vietnamese: clear and respectful. Address the congregation as "các anh chị em".',
           "naming": "Use the Church's official Vietnamese terminology. Keep people's surnames unchanged. Use the Vietnamese forms of scriptural names (Nê Phi, Ê Sai, Môi Se).",
           "refs": '"Giăng 3:16", "Giáo Lý và Giao Ước 88:118", "1 Nê Phi 3:7"'},
    "zh": {"lang": "Chinese",
           "style": "Natural spoken Mandarin, written in Simplified Chinese characters: clear and respectful. Write everything in Chinese characters, because the voice cannot read Latin letters.",
           "naming": "Use the Church's official Chinese terminology. Write people's names in Chinese characters (Smith → 斯密). Use the official Chinese forms of scriptural names (尼腓, 以赛亚, 摩西).",
           "refs": '"约翰福音3:16", "教义和圣约88:118", "尼腓一书3:7"'},
    "lv": {"lang": "Latvian",
           "style": 'Natural spoken Latvian: clear, standard, respectful Latvian with its diacritics (ā, č, ē, ģ, ī, ķ, ļ, ņ, š, ū, ž). Avoid slang and Russian loanwords. Address the congregation with "jūs".',
           "naming": "Use the Church's official Latvian terminology. Write people's names the Latvian way, adding Latvian endings where Latvian usage does (Smith → Smits). Use the Latvian forms of scriptural names (Nefijs, Jesaja, Mozus).",
           "refs": '"Jāņa 3:16", "Mācība un Derības 88:118", "1. Nefija 3:7"'},
}


def _lines(items: list[dict[str, Any]], reverse: bool = False) -> str:
    a, b = ("fr", "en") if reverse else ("en", "fr")
    return "\n".join(f"- {i[a]} → {i[b]}" for i in items if i.get("en") and i.get("fr"))


_LDS_HEADER = "a meeting of The Church of Jesus Christ of Latter-day Saints (usually sacrament meeting)"
_CHRISTIAN = [  # a congregation of another church: no Latter-day Saint wording in the instructions
    (re.escape(_LDS_HEADER), "a Christian church service"),
    (r"Use the Church's official English terminology \([^)]*\)", "Use standard English Christian terminology"),
    (r"the King James Bible or the Book of Mormon", "a standard English Bible"),
    (r"the Church's official (\w+) terminology", r"standard \1 Christian terminology"),
    (r"the Church's (\w+) scriptures", r"a standard \1 Bible"),
    (r"the Church's (\w+) book names", r"the \1 Bible book names"),
    (r"official Church (\w+)", r"\1 Christian"),
]


def source_names(srcs: list[str]) -> str:
    """"Russian", "Russian or Latvian", "French, Spanish or English"."""
    from ..languages import NAMES_EN

    names = [NAMES_EN.get(c, c) for c in srcs] or ["English"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _pivot(glossary: dict[str, Any], name: str) -> str:
    """Terms in each other speaker language → the official term in this one, joined on the English glossary."""
    from ..languages import NAMES_EN

    out = ""
    for src, pairs in (glossary.get("pivot") or {}).items():
        lines = "\n".join(f"- {p['src']} → {p['dst']}" for p in pairs if p.get("src") and p.get("dst"))
        if lines:
            out += f"\nGLOSSARY ({NAMES_EN.get(src, src)} → official Church {name})\n{lines}\n"
    return out


def build_system_prompt(glossary: dict[str, Any], audience: str, lang: str = "fr",
                        src: str | list[str] | tuple[str, ...] = "en") -> str:
    """The interpreter's instructions for translating from the speaker languages `src` into `lang` (a meeting may
    have several; a chunk already in `lang` is passed through and never reaches the interpreter)."""
    srcs = [src] if isinstance(src, str) else list(src)
    srcs = [c for c in dict.fromkeys(srcs) if c != lang] or ["en"]
    src_name = source_names(srcs)
    local = glossary.get("local_names") or []
    local_txt = ""
    if local:
        local_txt = "\nLOCAL NAMES (people and places in this congregation; keep exactly as written)\n" + \
            "\n".join(f"- {n}" for n in local if n)
    non_en = [c for c in srcs if c != "en"]
    if lang == "en":
        gloss = ""
        if non_en:  # the speaker languages' glossaries (glossary_for), read the other way round
            from_name = source_names(non_en)
            parts = [(f"GLOSSARY (official Church {from_name} → English)", glossary.get("terms", [])),
                     (f"NAMES ({from_name} → English)", glossary.get("names", [])),
                     (f"SCRIPTURE BOOKS ({from_name} → English)", glossary.get("books", []))]
            gloss = "".join(f"\n{title}\n{_lines(items, reverse=True)}\n" for title, items in parts if _lines(items))
        text = _TEMPLATE_EN.format(src=src_name, condense=CONDENSE_TAG, skip=SKIP, glossary=gloss, local=local_txt)
    elif lang == "wo":
        text = _TEMPLATE_WO.format(
            src=src_name, condense=CONDENSE_TAG, skip=SKIP,
            terms=_lines(glossary.get("terms", [])) or "- (none yet)",
            names=_lines(glossary.get("names", [])) or "- (none yet)",
            local=_pivot(glossary, "Wolof") + local_txt,
        )
    elif lang in _OTHER:
        target = _OTHER[lang]
        name = target["lang"]
        note = "" if not non_en else f"\n(Use these official {name} terms whenever the speaker says them in {src_name}.)"
        parts = [(f"GLOSSARY (English → official Church {name}){note}", glossary.get("terms", [])),
                 (f"NAMES (English → {name})", glossary.get("names", [])),
                 (f"SCRIPTURE BOOKS (English → {name})", glossary.get("books", []))]
        gloss = "".join(f"\n{title}\n{_lines(items)}\n" for title, items in parts if _lines(items))
        text = _TEMPLATE_OTHER.format(**target, src=src_name, condense=CONDENSE_TAG, skip=SKIP,
                                      glossary=gloss + _pivot(glossary, name), local=local_txt)
    else:
        note = "" if not non_en else f"\n(Use these official French terms whenever the speaker says them in {src_name}.)"
        text = _TEMPLATE.format(
            src=src_name, gloss_note=note,
            audience=audience,
            condense=CONDENSE_TAG,
            skip=SKIP,
            terms=_lines(glossary.get("terms", [])),
            names=_lines(glossary.get("names", [])),
            books=_lines(glossary.get("books", [])),
            local=_pivot(glossary, "French") + local_txt,
        )
    if glossary.get("_church") == "christian":
        for pattern, repl in _CHRISTIAN:
            text = re.sub(pattern, repl, text)
    return text


def whisper_hints(glossary: dict[str, Any]) -> list[str]:
    """English spellings that speech recognition tends to get wrong."""
    hints: list[str] = [n for n in glossary.get("local_names", []) if n]
    for item in glossary.get("names", []):
        en = item.get("en", "")
        if en and en not in hints and en not in ("John", "Mark", "Luke", "Mary", "Peter", "James", "Eve", "Israel"):
            hints.append(en)
    return hints


SCRIPTURE_RULE = """

PUBLISHED SCRIPTURE
A message may end with a block that starts "SCRIPTURE NOTE". It is not speech: it gives the published wording,
in the language you translate into, of a scripture verse the speaker has just named. Never translate it, read it
out or mention it. When the speaker's words quote that verse, use the published wording for the quoted part,
word for word, instead of your own translation (in the output language's own number style). When the
speaker is not quoting it, or the note's text says something different from what the speaker quotes (verse
numbers sometimes differ between editions), ignore the note."""


def user_message(english: str, condense: bool, scripture: str = "") -> str:
    msg = f"{CONDENSE_TAG} {english}" if condense else english
    return f"{msg}\n\n{scripture}" if scripture else msg


def seed_message(history: list[tuple[str, str]]) -> str:
    """First message of a fresh Claude conversation: carry over recent context without re-translating."""
    if not history:
        return "The meeting is about to start. For this first message only, reply simply: OK"
    lines = []
    for said, translated in history:
        lines.append(f"SPEAKER: {said}\nYOU: {translated}")
    return ("Context: here are the last fragments you already translated (do not translate them again). "
            "For this message only, reply simply: OK\n\n" + "\n\n".join(lines))


_PREFIX = re.compile(r"^(?:traduction|translation|fr|français|english|wolof)\s*:\s*", re.I)
# Model or tool markup that slipped into a reply ("antml:reasoning_effort>5", "<thinking>", "function_calls").
# Spoken French or Wolof never contains these, so a line that does is never read aloud or captioned.
_MARKUP = re.compile(r"[<>{}\\|]|antml|\b[A-Za-z]+_[A-Za-z_]+\b|^\s*(?:human|assistant|system|user)\s*:", re.I)


def has_markup(text: str) -> bool:
    return any(_MARKUP.search(line) for line in text.replace(SKIP, "").splitlines())


def clean_output(text: str) -> str | None:
    """Strip anything that isn't the translation. Returns None when the model chose to skip."""
    if text is None:
        return None
    t = text.strip()
    if not t or SKIP in t:
        return None
    t = _PREFIX.sub("", t)
    t = t.replace(CONDENSE_TAG, "").strip()
    # One line only: models occasionally add a note after a blank line.
    t = t.split("\n\n")[0].strip()
    if has_markup(t):
        kept = [line for line in t.splitlines() if not _MARKUP.search(line)]
        log.warning("dropped markup from a translation: %r", t[:200])
        t = "\n".join(kept).strip()
        if not any(ch.isalpha() for ch in t):
            return None
    if len(t) >= 2 and t[0] in "\"«“" and t[-1] in "\"»”":
        t = t[1:-1].strip()
    return t or None
