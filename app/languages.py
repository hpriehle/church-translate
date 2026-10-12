"""Output languages. Each enabled language gets its own translator, voice, audio stream and captions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import ROOT


@dataclass(frozen=True)
class Language:
    code: str
    name: str            # as listeners see it in the picker
    fallback: str        # "ollama" = local model if Claude fails; "none" = Claude only
    glossary_file: Path
    test_sentence: str   # "Play a test sentence" on the operator page


LANGUAGES: dict[str, Language] = {
    "fr": Language("fr", "Français", "ollama", ROOT / "glossary.json",
                   "Bonjour. Ceci est un essai de la traduction en direct. Si vous m'entendez, tout fonctionne."),
    # The local fallback model's Wolof is poor, so Wolof relies on Claude only.
    "wo": Language("wo", "Wolof", "none", ROOT / "glossary.wo.json",
                   "Salaam aleekum. Lii ab natt la ngir xool ndax tekki bi dafay dox. Su ngeen ma déggee, lépp baax na."),
}


def enabled(codes: list[str]) -> list[Language]:
    out = [LANGUAGES[c] for c in codes if c in LANGUAGES]
    return out or [LANGUAGES["fr"]]
