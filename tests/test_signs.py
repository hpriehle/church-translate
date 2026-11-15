"""The printed signs (/print): one per downloaded translation language, the meeting's first."""
import re
from pathlib import Path

from app import signs
from app.languages import LANGUAGES

LISTENER_JS = Path(__file__).parent.parent / "web" / "listener" / "listener.js"


def _sheets(html: str) -> list[tuple[str, bool]]:
    return [(m.group(2), not m.group(1)) for m in re.finditer(r'<div class="sheet( off)?" data-lang="(\w+)"', html)]


def test_every_language_has_a_sign_and_the_listener_knows_its_link():
    assert set(signs.SIGNS) == set(LANGUAGES)
    js = LISTENER_JS.read_text(encoding="utf-8")
    for code, (_, _, word, *_rest) in signs.SIGNS.items():
        assert re.search(rf'\b{word}: "{code}"', js), word


def test_french_and_wolof_wording_kept():
    assert signs.SIGNS["fr"][4:] == ("Pour écouter la réunion en français", "Scannez le code QR",
                                     "Prenez des écouteurs si vous en avez besoin.")
    assert signs.SIGNS["wo"][4:] == ("Ngir déglu ndaje mi ci wolof", "Scannéel QR code bi",
                                     "Su nga soxlaa ay écouteurs, jëlal ci yi.")


def test_meeting_languages_first_and_ticked():
    offer, ticked = signs.sign_languages(["fr", "wo", "es", "zh"], ["zh", "fr", "lv"])
    assert offer == ["zh", "fr", "wo", "es"] and ticked == ["zh", "fr"]
    offer, ticked = signs.sign_languages(["es", "fr"], [])
    assert offer == ticked == ["fr", "es"]


def test_render_one_sheet_per_downloaded_language():
    tpl = (Path(__file__).parent.parent / "web" / "print.html").read_text(encoding="utf-8")
    html = signs.render(tpl, ["fa", "fr", "ne", "zh"], ["fa"])
    assert _sheets(html) == [("fa", True), ("fr", False), ("ne", False), ("zh", False)]
    assert html.count('type="checkbox"') == 4 and 'value="fa" checked' in html and 'value="fr">' in html
    assert 'data-lang="fa" lang="fa" dir="rtl"' in html and 'lang="zh-Hans"' in html
    assert "To listen to church in Persian" in html and "به زبان فارسی" in html
    assert '"fa": "persian"' in html and "<!--" not in html.split("<body>")[1].split("<script>")[0]
    assert "Claude" not in html and "Anthropic" not in html
    assert "No translation language" in signs.render(tpl, [], [])


def test_print_endpoint_lists_the_downloaded_languages(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main, packages as pk
    from app.config import Settings

    st = Settings.model_validate({})
    st.meeting.targets = ["es"]
    monkeypatch.setattr(main.S, "settings", st, raising=False)
    monkeypatch.setattr(main.S, "app_mode", True, raising=False)
    monkeypatch.setattr(main.S, "local_addrs", {"testclient", "127.0.0.1"}, raising=False)
    monkeypatch.setattr(main, "_request_role", lambda request: "operator")

    async def status(langs):
        ready = {"speech", "fr", "es", "uk"}
        return [{"id": c, "state": "ready" if c in ready else "missing"} for c in ["speech", *langs]]
    monkeypatch.setattr(pk.PACKAGES, "status", status)
    r = TestClient(main.app).get("/print")
    assert r.status_code == 200
    assert _sheets(r.text) == [("es", True), ("fr", False), ("uk", False)]
