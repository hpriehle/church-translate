"""Churches never see the AI provider's name (Harrison, 2026-11-14): no user-facing "Claude" or "Anthropic" in the
Mac app's Swift sources or the local web pages. Exceptions: the Mac's AI tab (AISettings.swift, shown only to
congregations on their own key, who paste an Anthropic API key) and the licence notices.

What counts as user-facing: Swift string literals (outside comments), HTML text and the attributes people read
(title, placeholder, alt, aria-label, value of buttons), and JS string/template literals. Technical literals (ids,
config keys, env var names, the key-check URL) are allowed by pattern below."""
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SWIFT = ROOT / "mac" / "HearChurch" / "Sources"
WEB = ROOT / "web"
BRAND = re.compile(r"claude|anthropic", re.I)
EXEMPT_FILES = {"AISettings.swift"}

# Literals that are code, not words: model ids, setting keys, element ids, env vars, the key-check request.
TECHNICAL = [re.compile(p) for p in (
    r"^claude[A-Za-z0-9_-]*$",               # "claude", "claude_model", "claude-opus-5-5", "claudeState"
    r"^/api/claude-access$",
    r"^(ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN)$",
    r"^anthropic-(api-key|version)$",
    r"^https://api\.anthropic\.com/v1/models\?limit=1$",
)]


def technical(s: str) -> bool:
    return any(p.search(s) for p in TECHNICAL)


def exempt(path: Path) -> bool:
    return path.name in EXEMPT_FILES or "licen" in path.name.lower() or "notice" in path.name.lower()


def strip_comments(code: str) -> str:
    """Drop // line comments and /* */ blocks, leaving string literals alone (good enough for these sources)."""
    out, i, n = [], 0, len(code)
    quote = None
    while i < n:
        c = code[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(code[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
            out.append(c)
        elif code.startswith("//", i) and not code[max(0, i - 1)] == ":":   # not "https://"
            j = code.find("\n", i)
            i = n if j < 0 else j
            continue
        elif code.startswith("/*", i):
            j = code.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def literals(code: str, quotes: str) -> list[str]:
    code = strip_comments(code)
    pats = {'"': r'"(?:[^"\\\n]|\\.)*"', "'": r"'(?:[^'\\\n]|\\.)*'", "`": r"`(?:[^`\\]|\\.)*`"}
    found = []
    for q in quotes:
        found += [m.group(0)[1:-1] for m in re.finditer(pats[q], code)]
    return found


def hits_in(strings: list[str]) -> list[str]:
    return [s for s in strings if BRAND.search(s) and not technical(s)]


class _Text(HTMLParser):
    SEEN_ATTRS = {"title", "placeholder", "alt", "aria-label", "content"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        for k, v in attrs:
            if v and (k in self.SEEN_ATTRS or (k == "value" and tag in ("button", "input"))):
                self.out.append(v)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.out.append(data.strip())


def test_swift_sources_never_name_the_provider_outside_the_ai_tab():
    bad = []
    for f in sorted(SWIFT.glob("*.swift")):
        if exempt(f):
            continue
        bad += [f"{f.name}: {s!r}" for s in hits_in(literals(f.read_text(), '"'))]
    assert not bad, "User-facing provider names in the Mac app:\n" + "\n".join(bad)


def test_web_pages_never_name_the_provider():
    bad = []
    for f in sorted(WEB.rglob("*")):
        if not f.is_file() or exempt(f):
            continue
        rel = f.relative_to(ROOT)
        if f.suffix == ".html":
            p = _Text()
            p.feed(f.read_text())
            text = p.out
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", f.read_text(), re.S)
            text += [s for js in scripts for s in literals(js, "\"'`")]
        elif f.suffix == ".js":
            text = literals(f.read_text(), "\"'`")
        elif f.suffix == ".css":
            text = re.findall(r'content:\s*"([^"]*)"', f.read_text())
        else:
            continue
        bad += [f"{rel}: {s!r}" for s in hits_in(text)]
    assert not bad, "User-facing provider names in web/:\n" + "\n".join(bad)


def test_the_check_catches_a_brand_name():
    assert hits_in(literals('Text("Claude keeps them as they are.") // Claude in a comment is fine', '"'))
    assert not hits_in(literals('model.engine.set(["translate": ["claude_model": m]])  // Claude', '"'))
    p = _Text()
    p.feed('<p title="Ask Anthropic">Hi</p><script>const a = "Claude";</script>')
    assert hits_in(p.out) == ["Ask Anthropic"]
