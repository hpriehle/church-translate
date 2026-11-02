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
    # Shares the French glossary, read the other way round (official Church French → English).
    "en": Language("en", "English", "ollama", user_file("glossary.json"),
                   "Hello. This is a test of the live translation. If you can hear me, everything is working."),
}

# What the speaker can talk in: speech recognition (Whisper) knows these. Not Wolof: Whisper doesn't.
SPEAKER_LANGUAGES: dict[str, str] = {"en": "English", "fr": "Français", "es": "Español", "ru": "Русский"}
SPEAKER_NAMES_EN = {"en": "English", "fr": "French", "es": "Spanish", "ru": "Russian"}

# English follows the speaker: it is translated whenever the speaker isn't speaking English.
AUTO = {"en"}


def enabled(codes: list[str]) -> list[Language]:
    out = [LANGUAGES[c] for c in codes if c in LANGUAGES]
    return out or [LANGUAGES["fr"]]
