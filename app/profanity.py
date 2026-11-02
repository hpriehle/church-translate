"""Swear words, vulgar words and slurs → "****", in everything listeners see or hear.

Used on the English transcript (before it is translated, so a translation can't carry the word) and on
every translation (before captions; the voice skips the "****"). The translation prompt also tells Claude
to write **** itself, which covers languages whose list here is thin (Wolof).

Matching is per whole word, ignores case and accents (except ñ), and catches the usual forms through the
patterns. Words that appear in scripture or ordinary church talk are deliberately NOT here, so a talk is
never censored: hell / enfer / infierno / ад, damn / damnation, ass (the donkey), cock (the rooster that
crowed), whore and harlot (Revelation, 1 Nephi 14), bastard (Hebrews 12:8 KJV), prick (Acts 9:5),
bloody, "gueule" alone (la gueule du lion), "fous" (fools), zorras (foxes), hostia (the Host).

To add a word: put its accent-free, lowercase pattern in the right list. A pattern is a regular expression
matched as a whole word; tests/test_units.py checks words that must still pass.
"""
from __future__ import annotations

import re
import unicodedata

MASK = "****"

EN = [
    r"\w*f+u+c+k\w*", r"motherf\w*", r"f\*+ck\w*",
    r"fck(?:ing|ed|er)?", r"stfu", r"wtf",
    r"(?:bull|horse|dip|chicken|bat|ape)?shit\w*",
    r"bitch(?:es|ing|y|ed)?", r"sonofabitch", r"son of a bitch",
    r"a(?:ss|rse)[ -]?holes?", r"dumb[ -]?ass(?:es)?", r"smart[ -]?ass(?:es)?", r"bad[ -]?ass(?:es)?", r"kiss my ass",
    r"cunts?", r"twats?", r"wank(?:s|er|ers|ing|ed)?", r"jerk(?:ing)? off", r"bollocks", r"bugger(?:s|ed)? off",
    r"god ?damn(?:ed|it)?", r"god ?dam", r"dick ?heads?", r"cock ?suck(?:er|ers|ing)?",
    r"piss(?:ed|ing|es)", r"piss off", r"crap(?:py|s|ped)?", r"sluts?", r"slutty", r"skanks?",
    r"tits", r"titties", r"boobs", r"jizz", r"cumshots?", r"dildos?", r"blow ?jobs?", r"hand ?jobs?",
    r"porn(?:o|ography)?",
    # slurs (not "retard": the English list runs on every language, and "en retard" is everyday French)
    r"nigg(?:er|ers|a|as|az)", r"faggots?", r"fags?", r"dykes?", r"spics?", r"chinks?", r"kikes?",
    r"wetbacks?", r"trann(?:y|ies)",
]

FR = [
    r"putains?", r"putes?", r"fils de putes?", r"pute[ -]?borgne",
    r"merde(?:s|ux|use|uses)?", r"merdiques?", r"(?:em|dé)merd\w*",
    r"cons?", r"connes?", r"connard(?:e|s|es)?", r"connasses?", r"conneries?", r"ducon",
    r"salope(?:s|rie|ries)?", r"salauds?", r"saloperies?",
    r"encul(?:e|é|ée|és|ées|er|es|ent|eur|eurs|ade)s?",
    r"niqu(?:e|er|es|ent|é|ée|és|ez|ons)", r"nique ta mere", r"ntm",
    r"foutre", r"fout(?:u|ue|us|ues|ait|aient|ent)?",
    r"chi(?:er|e|es|ent|é|ant|ante|ants|antes|eur|euse|eurs|asse)", r"chiottes?",
    r"bites?", r"couill(?:e|es|on|ons|onne|onnes|onnade)", r"cul(?![- ]de[- ]sac)", r"trou du cul", r"trou de balle",
    r"branl(?:er|e|es|ent|eur|eurs|ette|ettes|é)", r"petasses?", r"pouffiasses?", r"garces?",
    r"enfoir(?:e|é|ée|és|ées)s?", r"bordel(?:s)?", r"(?:ta|ferme ta|vos|fermez vos) gueules?", r"nichons?", r"nibards?",
    r"bâtard(?:e|s|es)?", r"teubs?",
    # slurs
    r"pedes?", r"pd", r"tarlouzes?", r"gouines?", r"negros?", r"negresses?", r"bougnoules?", r"youpins?",
    r"bamboulas?", r"bicots?",
]

# Starter list only: needs a native Wolof speaker to complete. Claude's own **** (see the prompt) covers the rest.
WO: list[str] = []

ES = [
    r"mierdas?", r"put(?:a|as|o|os|ada|adas|ear)", r"hij(?:o|a)s? de puta", r"jod(?:er|ido|ida|idos|idas|e|as|an)",
    r"coñ(?:o|os|azo)", r"cabr(?:o|ó)n(?:es|a|as)?", r"pendej(?:o|a|os|as|ada)", r"gilipollas", r"carajos?",
    r"ching(?:ar|ada|ado|adera|ue|a|as|o)", r"vergas?", r"culer(?:o|a|os|as)", r"cojones", r"capullos?",
    r"maric(?:o|ó)n(?:es)?", r"mamad(?:a|as)", r"cabrones", r"malparid(?:o|a|os|as)",
]

RU = [
    r"(?:на|по|ни|от|за)?ху[йяеёию]\w*", r"(?:рас|за|от|про|вы)?пизд\w*",
    r"(?:за|вы|у|на|по|от|до|пере|раз|при|съ|из|в|об)?[её]б(?:ать|ал|ала|али|ан|аный|аная|аное|аные|ну|нуть|ут|ёт|ет|ешь|у|ло|ля|лан|ок|ки)\w*",
    r"долбо[её]б\w*", r"бля(?:дь|ди|дям|дский|дская|дство|ть)?", r"сук(?:а|и|е|у|ой|ин|ины|ин сын)", r"сучк\w*",
    r"муда(?:к|ки|ка|ку|ков|чьё)\w*", r"мудил\w*", r"говн\w*", r"дерьм\w*", r"жоп\w*", r"залуп\w*",
    r"пид(?:о|а)р\w*", r"шлюх\w*", r"гандон\w*",
]

LISTS = {"en": EN, "fr": FR, "wo": WO, "es": ES, "ru": RU}


def _fold(text: str) -> str:
    """Lowercase without accents, same length as the input (so match positions fit the original). ñ stays ñ."""
    out = []
    for ch in text:
        if ch in "ñÑ":
            out.append("ñ")
        else:
            base = unicodedata.normalize("NFKD", ch)[:1] or ch
            out.append(base.lower()[:1] or ch)
    return "".join(out)


def _compile(patterns: list[str]) -> re.Pattern | None:
    if not patterns:
        return None
    folded = [_fold(p) for p in patterns]
    return re.compile(r"(?<![\w*])(?:" + "|".join(folded) + r")(?![\w*])", re.UNICODE)


_RE = {code: _compile(p) for code, p in LISTS.items()}


def censor(text: str, lang: str = "en") -> str:
    """Every swear word in `text` → ****. English is always checked too (an untranslated word slips through)."""
    if not text:
        return text
    spans = []
    folded = _fold(text)
    for code in dict.fromkeys((lang, "en")):
        rx = _RE.get(code)
        if rx:
            spans += [m.span() for m in rx.finditer(folded)]
    if not spans:
        return text
    spans.sort()
    out, last = [], 0
    for a, b in spans:
        if a < last:            # overlaps a span already masked
            continue
        out.append(text[last:a])
        out.append(MASK)
        last = b
    out.append(text[last:])
    return "".join(out)


def unspoken(text: str) -> str:
    """For the voice: masked words are skipped, not read out as "asterisk". "" if nothing else is left."""
    t = re.sub(r"\*{2,}", " ", text)
    t = re.sub(r"\s+([,.;:!?…])", r"\1", t)          # "de , ce" → "de, ce"
    t = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", t)     # ", ," → ","
    t = re.sub(r"\s{2,}", " ", t).strip(" ,;:")
    return t if any(ch.isalpha() for ch in t) else ""
