"""Published scripture wording for quoted verses, from scripture files stored on this computer.

When the English transcript names a verse ("John 3:16", "Alma 32:21", "D&C section 88, verse 118"), every
output language looks it up in its own scripture files. For the next few translations Claude gets the
published wording as a note, so a quoted verse comes out as the congregation knows it rather than as a
fresh translation. A caption that reproduces the verse carries the source's credit.

Bibles come from eBible.org, one openly licensed translation per language (SOURCES), downloaded once into
HOME/scripture. Other scripture (e.g. the Church's editions of the Book of Mormon, Doctrine and Covenants and
Pearl of Great Price) is used when a file for it is present there: `<lang>.<name>.txt.gz` in the same format
(see Pack). None ships with HearChurch: the Church's translations need its written permission.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import logging
import re
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger("scripture")

# Book code → English names as a speaker says them. Bible books use USFM codes. Books that come in numbered
# parts list the bare name; the ordinal ("First", "2", "II") is parsed separately.
_BOOKS: dict[str, tuple[str, ...]] = {
    "GEN": ("genesis",), "EXO": ("exodus",), "LEV": ("leviticus",), "NUM": ("numbers",),
    "DEU": ("deuteronomy",), "JOS": ("joshua",), "JDG": ("judges",), "RUT": ("ruth",),
    "SA": ("samuel",), "KI": ("kings",), "CH": ("chronicles",), "EZR": ("ezra",), "NEH": ("nehemiah",),
    "EST": ("esther",), "JOB": ("job",), "PSA": ("psalm", "psalms"), "PRO": ("proverbs",),
    "ECC": ("ecclesiastes",), "SNG": ("song of solomon", "song of songs"), "ISA": ("isaiah",),
    "JER": ("jeremiah",), "LAM": ("lamentations",), "EZK": ("ezekiel",), "DAN": ("daniel",),
    "HOS": ("hosea",), "JOL": ("joel",), "AMO": ("amos",), "OBA": ("obadiah",), "JON": ("jonah",),
    "MIC": ("micah",), "NAM": ("nahum",), "HAB": ("habakkuk",), "ZEP": ("zephaniah",),
    "HAG": ("haggai",), "ZEC": ("zechariah",), "MAL": ("malachi",),
    "MAT": ("matthew",), "MRK": ("mark",), "LUK": ("luke",), "JHN": ("john",), "ACT": ("acts",),
    "ROM": ("romans",), "CO": ("corinthians",), "GAL": ("galatians",), "EPH": ("ephesians",),
    "PHP": ("philippians",), "COL": ("colossians",), "TH": ("thessalonians",), "TI": ("timothy",),
    "TIT": ("titus",), "PHM": ("philemon",), "HEB": ("hebrews",), "JAS": ("james",),
    "PE": ("peter",), "JUD": ("jude",), "REV": ("revelation", "revelations"),
    # Book of Mormon
    "NE": ("nephi",), "JAC": ("jacob",), "ENO": ("enos",), "JAR": ("jarom",), "OMN": ("omni",),
    "WOM": ("words of mormon",), "MSH": ("mosiah",), "ALM": ("alma",), "HEL": ("helaman",),
    "MRM": ("mormon",), "ETH": ("ether",), "MRO": ("moroni",),
    # Doctrine and Covenants (chapter = section) and Pearl of Great Price
    "DC": ("doctrine and covenants", "doctrine & covenants", "d&c", "d & c", "d and c"),
    "MSE": ("moses",), "ABR": ("abraham",), "JSM": ("joseph smith matthew",),
    "JSH": ("joseph smith history",), "AOF": ("articles of faith",),
}
# Numbered books: how many parts each has. "JN" is the epistles of John (1–3 John); bare "John" is the gospel.
_NUMBERED = {"SA": 2, "KI": 2, "CH": 2, "CO": 2, "TH": 2, "TI": 2, "PE": 2, "JN": 3, "NE": 4}
_NAME_TO_CODE = {name: code for code, names in _BOOKS.items() for name in names}
_DISPLAY = {"DC": "Doctrine and Covenants", "JSM": "Joseph Smith—Matthew", "JSH": "Joseph Smith—History",
            "SNG": "Song of Solomon", "JN": "John"}

_ORD = {"1": 1, "2": 2, "3": 3, "4": 4, "i": 1, "ii": 2, "iii": 3, "iv": 4, "first": 1, "second": 2,
        "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4}
_ORD_RE = "|".join(sorted(_ORD, key=len, reverse=True))
_SEP = r"[\s—–-]+"   # "Joseph Smith—History", "Joseph Smith History"
_BOOK_RE = "|".join(sorted((re.escape(n).replace(r"\ ", _SEP) for n in _NAME_TO_CODE), key=len, reverse=True))
# "16-18", "16 through 18", "16 and 17", but not the "2" of "and 2 Kings".
_RANGE = rf"(?:\s*(?:-|–|—|to|through|thru|and)\s*(?:verse\s+)?(?P<v2>\d{{1,3}})(?!\s*(?:{_BOOK_RE})\b))?"

# "John 3:16", "John 3.16-18", "1 Corinthians 13:4", "James chapter 1, verse 5", "D&C section 88 verse 118"
_REF = re.compile(
    rf"(?<![\w—-])(?:(?P<ord>{_ORD_RE})\s+)?(?P<book>{_BOOK_RE})\s+(?:(?:chapter|section)\s+)?(?P<ch>\d{{1,3}})"
    rf"(?:\s*[:.]\s*|,?\s+verses?\s+)(?P<v>\d{{1,3}}){_RANGE}(?!\d)",
    re.I,
)
# "section 88, verse 118" with no book: in a Latter-day Saint talk, the Doctrine and Covenants.
_SECTION = re.compile(rf"\bsection\s+(?P<ch>\d{{1,3}}),?\s+verses?\s+(?P<v>\d{{1,3}}){_RANGE}(?!\d)", re.I)
# "verse 17" / "verses 17 through 19" with no book: same chapter as the last reference.
_CONT = re.compile(rf"\bverses?\s+(?P<v>\d{{1,3}}){_RANGE}(?!\d)", re.I)
# Any other "Name 12:3" or "section 88": the speaker moved to scripture we don't know.
_OTHER = re.compile(r"\b[A-Z][a-z]+\s+\d{1,3}\s*:\s*\d{1,3}|\bsection\s+\d{1,3}\b", re.I)

CONTINUE_S = 90.0     # how long "verse 17" still refers to the last chapter named


@dataclass(frozen=True)
class Ref:
    book: str           # book code, e.g. "JHN", "1CO", "1NE", "DC"
    chapter: int
    first: int
    last: int

    @property
    def verses(self) -> list[str]:
        return [f"{self.book}.{self.chapter}.{v}" for v in range(self.first, self.last + 1)]

    @property
    def usfm(self) -> str:
        base = f"{self.book}.{self.chapter}.{self.first}"
        return base if self.last == self.first else f"{base}-{self.last}"

    @property
    def label(self) -> str:
        """English reference, e.g. "1 Nephi 3:7", "John 3:16-17"."""
        n, code = (self.book[0], self.book[1:]) if self.book[0].isdigit() else ("", self.book)
        name = _DISPLAY.get(code) or _BOOKS[code][0].title()
        verses = str(self.first) if self.first == self.last else f"{self.first}-{self.last}"
        return f"{n + ' ' if n else ''}{name} {self.chapter}:{verses}"


def _book_code(ordinal: str | None, name: str) -> str | None:
    base = _NAME_TO_CODE[re.sub(_SEP, " ", name.lower())]
    n = _ORD.get(ordinal.lower()) if ordinal else None
    if base == "JHN" and n:
        base = "JN"
    if base in _NUMBERED:
        if not n or n > _NUMBERED[base]:
            return None   # "Corinthians 13:4" with no number, or "Third Peter": unknown book
        return f"{n}{base}"
    return base if not n else None   # "First Romans" isn't a book


def _make_ref(code: str, ch: str, v: str, v2: str | None, max_verses: int) -> Ref | None:
    c, a = int(ch), int(v)
    b = int(v2) if v2 else a
    if b < a:
        b = a   # "verse 20 and 2": not a range
    if not (1 <= c <= 150 and 1 <= a <= 176) or b > 176:
        return None
    return Ref(code, c, a, min(b, a + max_verses - 1))


class RefDetector:
    """Finds scripture references in successive transcript chunks, across chunk boundaries."""

    def __init__(self, max_verses: int = 5):
        self.max_verses = max_verses
        self._tail = ""                  # end of the previous chunk, for a reference split across two
        self._last: tuple[Ref, float] | None = None
        self._seen: dict[Ref, float] = {}

    def feed(self, text: str, now: float | None = None) -> list[Ref]:
        """New references in this chunk. The previous chunk's tail is searched again, so a reference split
        across two chunks is found; each reference is reported once per CONTINUE_S."""
        now = time.monotonic() if now is None else now
        joined = (self._tail + " " + text) if self._tail else text
        new_from = len(self._tail) + 1 if self._tail else 0   # matches ending before this were handled last time
        found: list[tuple[int, Ref]] = []
        spans: list[tuple[int, int]] = []
        for m in _REF.finditer(joined):
            spans.append(m.span())
            if m.end() <= new_from:
                continue
            code = _book_code(m["ord"], m["book"])
            ref = code and _make_ref(code, m["ch"], m["v"], m["v2"], self.max_verses)
            if ref:
                found.append((m.start(), ref))
        for m in _SECTION.finditer(joined):
            if m.end() > new_from and not any(a <= m.start() < b for a, b in spans):
                spans.append(m.span())
                ref = _make_ref("DC", m["ch"], m["v"], m["v2"], self.max_verses)
                if ref:
                    found.append((m.start(), ref))
        for m in _OTHER.finditer(joined):
            if m.end() > new_from and not any(a <= m.start() < b for a, b in spans):
                found.append((m.start(), None))   # other scripture: ends "verse N" continuation
        for m in _CONT.finditer(joined):
            if m.end() <= new_from or any(a <= m.start() < b for a, b in spans):
                continue
            found.append((m.start(), ("cont", m["v"], m["v2"])))
        out: list[Ref] = []
        for pos, item in sorted(found, key=lambda t: t[0]):
            if item is None:
                self._last = None
                continue
            if isinstance(item, tuple):
                if not self._last or now - self._last[1] > CONTINUE_S:
                    continue
                prev = self._last[0]
                item = _make_ref(prev.book, str(prev.chapter), item[1], item[2], self.max_verses)
                if not item:
                    continue
            self._last = (item, now)
            if now - self._seen.get(item, -1e9) < CONTINUE_S:
                continue
            self._seen[item] = now
            out.append(item)
        self._tail = text[-60:]
        return out

    def reset(self) -> None:
        self._tail, self._last = "", None
        self._seen.clear()


# ----------------------------------------------------------------------------- scripture files
@dataclass(frozen=True)
class Source:
    id: str          # eBible.org translation id
    title: str
    notice: str      # credit shown under a caption that quotes it
    versification: str = ""   # "synodal": Psalms numbered as in the Septuagint


EBIBLE_URL = "https://ebible.org/Scriptures/{id}_vpl.zip"
# Openly licensed Bibles a paid app may use (public domain unless noted). DEFAULT_BIBLES are the ones
# congregations in that language actually read; the others are newer or less familiar, so a church
# chooses them in [scripture.bibles]. Wolof's Bible (Kàddug Yàlla) is CC BY-NC: not usable in a paid app.
SOURCES: dict[str, Source] = {s.id: s for s in (
    Source("eng-kjv2006", "King James Version", "King James Version · public domain"),
    Source("fraLSG", "Louis Segond 1910", "Louis Segond 1910 · public domain"),
    Source("spaRV1909", "Reina-Valera 1909", "Reina-Valera 1909 · public domain"),
    Source("russyn", "Russian Synodal Bible", "Russian Synodal Bible · public domain", "synodal"),
    Source("cmn-cu89s", "Chinese Union Version", "Chinese Union Version · public domain"),
    Source("vie1934", "Vietnamese Bible 1923", "Vietnamese Bible 1923 · public domain"),
    Source("pesOPV", "Old Persian Version", "Old Persian Version · public domain"),
    Source("porbrbsl", "World Portuguese Bible", "World Portuguese Bible · public domain"),
    Source("ukr1871", "Ukrainian Bible (Kulish)", "Ukrainian Bible, P. Kulish · public domain"),
    Source("npiulb", "Nepali Bible (Door43)",
           "Nepali Bible © 2019 Door43 World Missions Community · CC BY-SA 4.0 · unchanged"),
)}
DEFAULT_BIBLES = {"en": "eng-kjv2006", "fr": "fraLSG", "es": "spaRV1909", "ru": "russyn",
                  "zh": "cmn-cu89s", "vi": "vie1934", "fa": "pesOPV"}
# eBible's verse files use a few book codes of their own.
_VPL_CODES = {"JOH": "JHN", "MAR": "MRK", "JAM": "JAS", "EZE": "EZK", "JOE": "JOL", "NAH": "NAM",
              "PHI": "PHP", "SOL": "SNG", "1JO": "1JN", "2JO": "2JN", "3JO": "3JN"}
_VPL_LINE = re.compile(r"^([1-4]?[A-Z]{2,3}) (\d+):(\d+) (.*)$")


def bible_for(lang: str, chosen: dict[str, str]) -> str:
    """The Bible a language uses: the church's choice in [scripture.bibles], else the default ("" = none)."""
    b = chosen.get(lang, DEFAULT_BIBLES.get(lang, ""))
    return b if b in SOURCES else ""


def _synodal_psalm(chapter: int, verse: int) -> tuple[int, int]:
    """English Psalm chapter:verse → the Synodal Bible's. Verse numbers can still differ where Synodal counts
    a psalm's title as verses; the translator is told to ignore a note that doesn't match the quote."""
    if chapter <= 8 or chapter >= 148:
        return chapter, verse
    if chapter == 10:
        return 9, verse + 21
    if chapter == 115:
        return 113, verse + 8
    if chapter == 116:
        return (114, verse) if verse <= 9 else (115, verse - 9)
    if chapter == 147:
        return (146, verse) if verse <= 11 else (147, verse - 11)
    return (chapter if chapter == 9 else chapter - 1), verse


def _clean_verse(text: str) -> str:
    text = text.replace("\u3000", "").replace("¶", " ").replace("|", " ")
    text = re.sub(r"[\[\]]", "", text)   # [words] the translators added
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Pack:
    """One scripture file: gzip text, "#title\t…" and "#notice\t…" first, then "JHN.3.16\t<text>" lines."""
    title: str
    notice: str
    verses: dict[str, str]
    versification: str = ""

    def key(self, book: str, chapter: int, verse: int) -> str:
        if self.versification == "synodal" and book == "PSA":
            chapter, verse = _synodal_psalm(chapter, verse)
        return f"{book}.{chapter}.{verse}"

    @classmethod
    def load(cls, path: Path) -> "Pack":
        meta: dict[str, str] = {}
        verses: dict[str, str] = {}
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.rstrip("\n").partition("\t")
                if k.startswith("#"):
                    meta[k[1:]] = v
                elif v:
                    verses[k] = v
        return cls(meta.get("title", path.stem), meta.get("notice", ""), verses, meta.get("versification", ""))

    @staticmethod
    def write(path: Path, title: str, notice: str, verses: dict[str, str], versification: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            f.write(f"#title\t{title}\n#notice\t{notice}\n")
            if versification:
                f.write(f"#versification\t{versification}\n")
            for k, v in verses.items():
                f.write(f"{k}\t{v}\n")
        tmp.replace(path)


def verses_from_vpl(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.decode("utf-8-sig").splitlines():
        m = _VPL_LINE.match(line)
        if m and m[4].strip():
            out[f"{_VPL_CODES.get(m[1], m[1])}.{int(m[2])}.{int(m[3])}"] = _clean_verse(m[4])
    return out


async def install(lang: str, source_id: str, folder: Path, client: httpx.AsyncClient | None = None) -> Path:
    """Download one eBible translation and store it as <folder>/<lang>.<id>.txt.gz (about 1.5 MB)."""
    src = SOURCES[source_id]
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True, timeout=60)
    try:
        r = await client.get(EBIBLE_URL.format(id=source_id))
        r.raise_for_status()
        data = r.content
    finally:
        if own:
            await client.aclose()

    def convert() -> Path:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            verses = verses_from_vpl(z.read(f"{source_id}_vpl.txt"))
        if len(verses) < 7900 or "JHN.3.16" not in verses:
            raise ValueError(f"{source_id}: only {len(verses)} verses: not a whole Bible")
        dest = folder / f"{lang}.{source_id}.txt.gz"
        Pack.write(dest, src.title, src.notice, verses, src.versification)
        return dest

    dest = await asyncio.to_thread(convert)
    log.info("installed %s for %s (%d KB)", source_id, lang, dest.stat().st_size // 1024)
    return dest


@dataclass
class Passage:
    ref: Ref
    text: str
    title: str
    notice: str


class Library:
    """The scripture files in one folder, loaded per language on first use and then kept in memory."""

    def __init__(self, folder: Path):
        self.folder = folder
        self._packs: dict[tuple[str, str], list[Pack]] = {}
        self._lock = threading.Lock()

    def files(self, lang: str, bible: str) -> list[Path]:
        """The language's chosen Bible plus any other scripture for it (not the Bibles it doesn't use)."""
        out = [self.folder / f"{lang}.{bible}.txt.gz"] if bible else []
        for p in sorted(self.folder.glob(f"{lang}.*.txt.gz")):
            name = p.name[len(lang) + 1:-len(".txt.gz")]
            if name not in SOURCES:
                out.append(p)
        return [p for p in out if p.exists()]

    def packs(self, lang: str, bible: str) -> list[Pack]:
        with self._lock:
            key = (lang, bible)
            if key not in self._packs:
                self._packs[key] = [Pack.load(p) for p in self.files(lang, bible)]
            return self._packs[key]

    def forget(self) -> None:
        with self._lock:
            self._packs.clear()

    def passage(self, lang: str, bible: str, ref: Ref) -> Passage | None:
        for pack in self.packs(lang, bible):
            texts = [pack.verses.get(pack.key(ref.book, ref.chapter, v)) for v in range(ref.first, ref.last + 1)]
            if texts[0]:
                return Passage(ref, " ".join(t for t in texts if t), pack.title, pack.notice)
        return None


# ----------------------------------------------------------------------------- per-language feed
NOTE_HEADER = "SCRIPTURE NOTE"


@dataclass
class _Pending:
    ref: Ref
    t: float
    task: asyncio.Task | None = None
    passage: Passage | None = None


@dataclass
class ScriptureFeed:
    """One output language: looks up referenced verses and offers them to its translator for a while."""
    library: Library | None
    lang: str = ""
    bible: str = ""
    window_s: float = 120.0
    keep: int = 2
    _recent: list[_Pending] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.library is not None

    def prefetch(self, refs: list[Ref], now: float | None = None) -> None:
        if not self.enabled or not refs:
            return
        now = time.monotonic() if now is None else now
        for ref in refs:
            self._recent = [p for p in self._recent if p.ref != ref]
            p = _Pending(ref, now)
            p.task = asyncio.create_task(self._fetch(p))
            self._recent.append(p)
        del self._recent[:-self.keep]

    async def _fetch(self, p: _Pending) -> None:
        try:
            p.passage = await asyncio.to_thread(self.library.passage, self.lang, self.bible, p.ref)
        except Exception as e:
            log.warning("scripture %s (%s): %s", p.ref.usfm, self.lang, e)

    async def settle(self, timeout_s: float) -> None:
        """Wait (briefly) for verses still being fetched."""
        pending = [p.task for p in self._recent if p.task and not p.task.done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout_s)

    def ready(self, now: float | None = None) -> list[Passage]:
        now = time.monotonic() if now is None else now
        self._recent = [p for p in self._recent if now - p.t <= self.window_s]
        return [p.passage for p in self._recent if p.passage]

    def note(self, now: float | None = None) -> str:
        """Text appended to the translator's message while a referenced verse is still recent."""
        passages = self.ready(now)
        if not passages:
            return ""
        lines = [f'{p.ref.label} ({p.title}): "{p.text}"' for p in passages]
        return f"{NOTE_HEADER}\n" + "\n".join(lines)

    def credit_for(self, text: str, now: float | None = None) -> dict[str, str] | None:
        """The attribution for a caption that reproduces a recent verse, else None."""
        best, score = None, 0.0
        for p in self.ready(now):
            s = overlap(text, p.text)
            if s > score:
                best, score = p, s
        if not best or score < 0.5:
            return None
        return {"reference": best.ref.label, "source": best.title, "notice": best.notice or best.title}

    def clear(self) -> None:
        for p in self._recent:
            if p.task and not p.task.done():
                p.task.cancel()
        self._recent.clear()


def _grams(text: str) -> set[str]:
    s = unicodedata.normalize("NFKC", text).lower()
    s = re.sub(r"[\W_]+", "", s)   # spaces and punctuation out: also works for Chinese
    n = 2 if any("一" <= c <= "鿿" for c in s) else 4
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def overlap(caption: str, verse: str) -> float:
    """Share of the caption's character n-grams found in the verse (0–1)."""
    c = _grams(caption)
    if len(c) < 8:
        return 0.0
    return len(c & _grams(verse)) / len(c)


async def _cli(args: list[str]) -> None:
    from .config import HOME

    folder = HOME / "scripture"
    if not args:
        print("uv run python -m app.scripture install fr es …   (or: lang=eBibleId, e.g. pt=porbrbsl)\n")
        for lang, b in sorted(DEFAULT_BIBLES.items()):
            print(f"  {lang}  {b:<12} {SOURCES[b].notice}")
        print("\nOptional:", ", ".join(f"{k} ({v.title})" for k, v in SOURCES.items() if k not in DEFAULT_BIBLES.values()))
        return
    for a in args[1:] if args[0] == "install" else args:
        lang, _, b = a.partition("=")
        path = await install(lang, b or DEFAULT_BIBLES[lang], folder)
        print(f"{path}  {path.stat().st_size // 1024} KB")


if __name__ == "__main__":   # uv run python -m app.scripture install fr es
    import sys
    asyncio.run(_cli(sys.argv[1:]))
