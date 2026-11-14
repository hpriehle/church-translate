"""Glossaries by church: which entries only a Latter-day Saint congregation needs.

Every glossary entry is either Christian (any church) or LDS (The Church of Jesus Christ of Latter-day Saints only).
An entry says so itself with "scope": "lds" | "christian"; without one, its English key decides, from the lists
below (they also cover glossaries copied to a Mac before entries had a scope). The church chosen at onboarding
picks the glossary: Latter-day Saints → Christian + LDS entries; any other church → Christian entries only.
"""
from __future__ import annotations

from typing import Any

LDS_CHURCH = "The Church of Jesus Christ of Latter-day Saints"

LDS_TERMS = {
    "aaronic priesthood", "baptism for the dead", "bishopric", "book of mormon", "branch", "branch president",
    "calling", "celestial kingdom", "come, follow me", "counselor", "covenant path", "doctrine and covenants",
    "elder (missionary title)", "elder (priesthood office)", "elder (title of a general authority, e.g. elder bednar)",
    "elders quorum", "endowment", "exaltation", "family history", "family home evening", "fast and testimony meeting",
    "fast offering", "fast sunday", "first presidency", "first vision", "for the strength of youth",
    "full-time missionary", "gathering of israel", "general authority", "general conference", "gospel library",
    "heavenly parents", "high council", "latter-day saints", "law of chastity", "liahona (magazine)", "meetinghouse",
    "melchizedek priesthood", "ministering", "ministering brothers", "ministering brothers and sisters",
    "ministering sisters", "mission president", "patriarchal blessing", "pearl of great price", "plan of happiness",
    "plan of salvation", "premortal life", "president (e.g. president oaks)", "primary", "priesthood blessing",
    "quorum of the twelve apostles", "relief society", "restoration", "sacrament hymn", "sacrament meeting",
    "sacred grove", "sealing", "seventy", "stake", "stake center", "stake presidency", "stake president",
    "teacher (priesthood office)", "telestial kingdom", "temple recommend", "terrestrial kingdom",
    "the church of jesus christ of latter-day saints", "the family: a proclamation to the world", "the living christ",
    "the restoration", "the seventy", "to release (from a calling)", "to set apart", "to sustain", "ward",
    "word of wisdom", "young men", "young women",
}

LDS_NAMES = {
    "abinadi", "alma", "ammon", "andersen", "bednar", "bountiful", "brother of jared", "captain moroni", "causse",
    "christofferson", "ether", "eyring", "gong", "helaman", "hill cumorah", "holland", "jaredites", "joseph smith",
    "kearon", "king benjamin", "kirtland", "korihor", "laman", "lamanites", "lehi", "lemuel", "liahona",
    "liberty jail", "mormon", "moroni", "mosiah", "nauvoo", "nephi", "nephites", "oaks", "rasband", "renlund",
    "samuel the lamanite", "sariah", "soares", "uchtdorf", "zarahemla",
}

LDS_BOOKS = {
    "1 nephi", "2 nephi", "3 nephi", "4 nephi", "abraham", "alma", "articles of faith", "book of mormon",
    "doctrine and covenants", "enos", "ether", "helaman", "jacob", "jarom", "joseph smith—history",
    "joseph smith—matthew", "mormon", "moroni", "moses (pearl of great price)", "mosiah", "omni",
    "pearl of great price", "words of mormon",
}

_BUILTIN = {"terms": LDS_TERMS, "names": LDS_NAMES, "books": LDS_BOOKS}


def is_lds_church(church: str | None) -> bool:
    """Not chosen yet (every congregation before V3 is a Latter-day Saint one) or the Church itself."""
    c = (church or "").strip().lower()
    return not c or c in {LDS_CHURCH.lower(), "lds", "latter-day saints"}


def scope_of(section: str, item: dict[str, Any]) -> str:
    s = str(item.get("scope") or "").lower()
    if s in ("lds", "christian"):
        return s
    return "lds" if str(item.get("en") or "").strip().lower() in _BUILTIN.get(section, ()) else "christian"


def for_church(glossary: dict[str, Any], church: str | None) -> dict[str, Any]:
    """The glossary a congregation of `church` uses: everything for Latter-day Saints, Christian entries otherwise."""
    if is_lds_church(church):
        return glossary
    out = dict(glossary)
    for section in ("terms", "names", "books"):
        out[section] = [i for i in glossary.get(section) or [] if scope_of(section, i) == "christian"]
    return out
