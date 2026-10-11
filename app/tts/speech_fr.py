"""Make caption French sound right when read by a TTS voice (scripture references, abbreviations)."""
from __future__ import annotations

import re

# How numbered books are read aloud in French.
_NUMBERED = {
    "Néphi": ("Premier", "Deuxième", "Troisième", "Quatrième"),
    "Samuel": ("Premier", "Deuxième"),
    "Rois": ("Premier livre des", "Deuxième livre des"),
    "Chroniques": ("Premier livre des", "Deuxième livre des"),
    "Corinthiens": ("Première épître aux", "Deuxième épître aux"),
    "Thessaloniciens": ("Première épître aux", "Deuxième épître aux"),
    "Timothée": ("Première épître à", "Deuxième épître à"),
    "Pierre": ("Première épître de", "Deuxième épître de"),
    "Jean": ("Première épître de", "Deuxième épître de", "Troisième épître de"),
}

_BOOK_WORD = r"[A-ZÉÈÊÀÂÎÔÛÇ][\wéèêàâîôûçëïü'’-]+"
# "Jean 3:16", "Doctrine et Alliances 88:118", "Joseph Smith, Histoire 1:17", "Alma 32:21-23"
_REF = re.compile(
    rf"(?P<book>(?:{_BOOK_WORD})(?:(?:\s+et|\s+des|\s+de|,)?\s+{_BOOK_WORD}){{0,2}})\s+"
    r"(?P<ch>\d{1,3})\s*[:.]\s*(?P<v1>\d{1,3})(?:\s*[-–à]\s*(?P<v2>\d{1,3}))?"
)
_CHAPTER_ONLY = re.compile(r"\b(Doctrine et Alliances|D&A)\s+(\d{1,3})\b(?!\s*[:.,]\s*\d)")


def _numbered_book(m: re.Match) -> str:
    n, book = int(m.group(1)), m.group(2)
    forms = _NUMBERED.get(book)
    if forms and 1 <= n <= len(forms):
        return f"{forms[n - 1]} {book}"
    return m.group(0)


def speechify(text: str) -> str:
    t = text.replace("D&A", "Doctrine et Alliances").replace("D. & A.", "Doctrine et Alliances")

    def ref(m: re.Match) -> str:
        book, ch, v1, v2 = m.group("book"), m.group("ch"), m.group("v1"), m.group("v2")
        if v2:
            return f"{book}, chapitre {ch}, versets {v1} à {v2}"
        return f"{book}, chapitre {ch}, verset {v1}"

    t = _REF.sub(ref, t)
    t = _CHAPTER_ONLY.sub(lambda m: f"{m.group(1)}, section {m.group(2)}", t)
    t = re.sub(r"\b([1-4])\s+(" + "|".join(_NUMBERED) + r")\b", _numbered_book, t)
    t = t.replace("«", "").replace("»", "").replace("“", "").replace("”", "")
    t = re.sub(r"\s+", " ", t).strip()
    return t
