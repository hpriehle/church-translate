"""Per-language clean-up of caption text before it is read aloud."""
from __future__ import annotations

import re

from .speech_fr import speechify as speechify_fr

# Wolof number words (please have a Wolof speaker review). The Wolof translator is asked to write
# numbers in words already; this only catches digits that slip through, since the Wolof voice
# cannot read digits.
_WO_UNITS = ["", "benn", "ñaar", "ñett", "ñeent", "juróom", "juróom-benn", "juróom-ñaar", "juróom-ñett", "juróom-ñeent"]
_WO_TENS = {1: "fukk", 2: "ñaar-fukk", 3: "fanweer", 4: "ñeent-fukk", 5: "juróom-fukk", 6: "juróom-benn-fukk",
            7: "juróom-ñaar-fukk", 8: "juróom-ñett-fukk", 9: "juróom-ñeent-fukk"}
_WO_MULT = ["", "", "ñaari", "ñetti", "ñeenti", "juróomi", "juróom-benni", "juróom-ñaari", "juróom-ñetti", "juróom-ñeenti"]


def wolof_number(n: int) -> str:
    if n == 0:
        return "tus"
    parts = []
    thousands, rest = divmod(n, 1000)
    if thousands:
        parts.append("junni" if thousands == 1 else (f"{_WO_MULT[thousands]} junni" if thousands < 10 else f"{wolof_number(thousands)} junni"))
    hundreds, rest = divmod(rest, 100)
    if hundreds:
        parts.append("téeméer" if hundreds == 1 else f"{_WO_MULT[hundreds]} téeméer")
    tens, units = divmod(rest, 10)
    if tens:
        parts.append(_WO_TENS[tens])
    if units:
        parts.append(_WO_UNITS[units])
    return " ak ".join(parts)


def speechify_wo(text: str) -> str:
    t = text.replace("«", "").replace("»", "").replace("“", "").replace("”", "")
    t = re.sub(r"(\d+)\s*:\s*(\d+)", lambda m: f"{m.group(1)}, {m.group(2)}", t)  # 3:16 → 3, 16
    t = re.sub(r"\d{1,4}", lambda m: wolof_number(int(m.group(0))), t)
    return re.sub(r"\s+", " ", t).strip()


def speechify(code: str, text: str) -> str:
    if code == "fr":
        return speechify_fr(text)
    if code == "wo":
        return speechify_wo(text)
    return re.sub(r"\s+", " ", text).strip()
