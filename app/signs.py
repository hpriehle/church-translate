"""The printed signs (/print): one sheet per translation language, with a QR code to that language's listening page.

Each sheet: a headline in the language, "scan the QR code", "take headphones", and a small English line. The French and
Wolof wording is what the church has printed since the start; keep it as it is.

Wording a native speaker hasn't checked yet is marked UNCHECKED below.
"""
from __future__ import annotations

import json
from html import escape

# code: (html lang, text direction, the word after "#" in the listening link, English name,
#        headline, scan line, headphones line)
SIGNS: dict[str, tuple[str, str, str, str, str, str, str]] = {
    "fr": ("fr", "ltr", "french", "French",
           "Pour écouter la réunion en français", "Scannez le code QR",
           "Prenez des écouteurs si vous en avez besoin."),
    # Wolof: as printed so far; have a Wolof speaker look at it before a new print run.
    "wo": ("wo", "ltr", "wolof", "Wolof",
           "Ngir déglu ndaje mi ci wolof", "Scannéel QR code bi",
           "Su nga soxlaa ay écouteurs, jëlal ci yi."),
    # "audífonos" is the Latin American word; Spain says "auriculares".
    "es": ("es", "ltr", "spanish", "Spanish",
           "Para escuchar la reunión en español", "Escanee el código QR",
           "Tome unos audífonos si los necesita."),
    "ru": ("ru", "ltr", "russian", "Russian",
           "Чтобы слушать собрание на русском языке", "Отсканируйте QR-код",
           "Возьмите наушники, если они вам нужны."),
    # Brazilian Portuguese ("fones de ouvido"; Portugal says "auscultadores").
    "pt": ("pt", "ltr", "portuguese", "Portuguese",
           "Para ouvir a reunião em português", "Escaneie o código QR",
           "Pegue fones de ouvido se precisar."),
    "uk": ("uk", "ltr", "ukrainian", "Ukrainian",
           "Щоб слухати збори українською мовою", "Відскануйте QR-код",
           "Візьміть навушники, якщо вони вам потрібні."),
    # UNCHECKED: Persian, especially the headphones line.
    "fa": ("fa", "rtl", "persian", "Persian",
           "برای گوش دادن به جلسه به زبان فارسی", "کد QR را اسکن کنید",
           "اگر به هدفون نیاز دارید، یکی بردارید."),
    # UNCHECKED: Nepali, all three lines.
    "ne": ("ne", "ltr", "nepali", "Nepali",
           "सभा नेपाली भाषामा सुन्नका लागि", "QR कोड स्क्यान गर्नुहोस्",
           "आवश्यक भएमा हेडफोन लिनुहोस्।"),
    "vi": ("vi", "ltr", "vietnamese", "Vietnamese",
           "Để nghe buổi họp bằng tiếng Việt", "Hãy quét mã QR",
           "Xin lấy tai nghe nếu quý vị cần."),
    # Simplified Chinese (Mandarin), as the Chinese voice speaks. UNCHECKED: "聚会" for the meeting.
    "zh": ("zh-Hans", "ltr", "chinese", "Chinese",
           "用中文收听聚会", "请扫描二维码",
           "如有需要，请取用耳机。"),
    "en": ("en", "ltr", "english", "English",
           "To listen to the meeting in English", "Scan the QR code",
           "Take some headphones if you need some."),
    # UNCHECKED: Latvian, all three lines.
    "lv": ("lv", "ltr", "latvian", "Latvian",
           "Lai klausītos sanāksmi latviešu valodā", "Noskenējiet QR kodu",
           "Ja nepieciešams, paņemiet austiņas."),
}

_PHONES = ('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 15v-3a8 8 0 0 1 16 0v3"/>'
           '<rect x="2.5" y="14" width="4" height="6.5" rx="1.5"/><rect x="17.5" y="14" width="4" height="6.5" rx="1.5"/></svg>')


def sign_languages(downloaded: list[str], meeting: list[str]) -> tuple[list[str], list[str]]:
    """The languages to offer (the meeting's translation languages first, then the other downloaded ones, in the
    usual order) and the ones ticked at first (the meeting's, or all of them when the meeting has none)."""
    have = [c for c in SIGNS if c in downloaded]
    first = [c for c in dict.fromkeys(meeting) if c in have]
    offer = first + [c for c in have if c not in first]
    return offer, (first or offer)


def sheet(code: str, on: bool) -> str:
    lang, direction, _, name, headline, scan, phones = SIGNS[code]
    english = "" if code == "en" else (
        f'\n  <div class="en" lang="en" dir="ltr">To listen to church in {escape(name)}, scan the QR code. '
        f'Take some headphones if you need some.</div>')
    return (f'<div class="sheet{"" if on else " off"}" data-lang="{code}" lang="{lang}" dir="{direction}">\n'
            f'  <h1>{escape(headline)}</h1>\n'
            f'  <div class="scan">{escape(scan)}</div>\n'
            f'  <img class="qr" alt="QR">\n'
            f'  <div class="url" dir="ltr"></div>\n'
            f'  <div class="phones">{_PHONES} <span>{escape(phones)}</span></div>{english}\n'
            f'</div>')


def choice(code: str, on: bool) -> str:
    name = SIGNS[code][3]
    return (f'<label><input type="checkbox" value="{code}"{" checked" if on else ""}> {escape(name)}</label>')


def render(template: str, downloaded: list[str], meeting: list[str]) -> str:
    """print.html with its sheets and checkboxes filled in."""
    offer, ticked = sign_languages(downloaded, meeting)
    hashes = {c: SIGNS[c][2] for c in offer}
    if not offer:
        sheets = '<p class="none">No translation language is downloaded yet. Download one first, then print its sign.</p>'
    else:
        sheets = "\n\n".join(sheet(c, c in ticked) for c in offer)
    return (template.replace("<!--CHOICES-->", " ".join(choice(c, c in ticked) for c in offer))
                    .replace("<!--SHEETS-->", sheets)
                    .replace("/*HASHES*/{}", json.dumps(hashes)))
