"""The Hear Church Mac app's engine: direct API translator, env overrides, files outside the code folder."""
import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from app.config import TranslateCfg
from app.translate.api_session import ApiSession, cost_usd, model_id
from app.translate.claude_session import Usage
from app.translate.router import Translator


def _reply(text: str, stop: str = "end_turn") -> BetaMessage:
    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": text}],
        "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 20},
    })


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append({**kw, "messages": [dict(m) for m in kw["messages"]]})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


async def test_api_session_keeps_an_append_only_cached_conversation():
    client = FakeClient([_reply("Bonjour."), _reply("Merci.")])
    s = ApiSession("SYSTEM", "opus", "low", client=client)
    await s.start()
    assert await s.ask("Hello.") == "Bonjour."
    assert await s.ask("Thank you.") == "Merci."
    first, second = client.calls
    assert first["model"] == "claude-opus-5-5" and first["system"] == "SYSTEM"
    assert first["output_config"] == {"effort": "low"} and first["cache_control"] == {"type": "ephemeral"}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    # The second request repeats the first exchange unchanged, thinking block included.
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "user"]
    assert second["messages"][1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    assert s.usage.turns == 2 and s.usage.cache_read == 2000 and not s.busy
    assert s.usage.cost_usd == pytest.approx(2 * (10 * 4 + 5 * 20 + 1000 * 0.2 + 20 * 5) / 1e6)


async def test_api_session_drops_an_unanswered_sentence_and_reports_refusals():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client = FakeClient([anthropic.APIConnectionError(request=req), _reply("", stop="refusal")])
    s = ApiSession("SYSTEM", "sonnet", "", client=client)
    await s.start()
    with pytest.raises(RuntimeError, match="cannot reach Anthropic"):
        await s.ask("Hello.")
    assert s.messages == [] and s.alive and not s.busy
    with pytest.raises(RuntimeError, match="declined"):
        await s.ask("Hello again.")
    assert client.calls[1]["model"] == "claude-sonnet-5-5" and client.calls[1]["output_config"] is anthropic.omit


async def test_api_session_needs_a_key(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("app.config.SECRETS_PATH", tmp_path / "none.env")
    monkeypatch.setattr("app.config.load_secrets", lambda path=None: {})
    with pytest.raises(RuntimeError, match="no Anthropic API key"):
        await ApiSession("S", "opus", "low").start()


def test_model_ids_and_prices():
    assert model_id("opus") == "claude-opus-5-5" and model_id("claude-x") == "claude-x"
    assert cost_usd("claude-haiku-5-5", Usage(input=1_000_000)) == pytest.approx(0.10)


def test_router_uses_the_api_without_the_claude_cli(monkeypatch, tmp_path):
    monkeypatch.setattr("app.translate.router.claude_binary", lambda: None)
    t = Translator(TranslateCfg(mode="claude", backend="api"), {}, tmp_path)
    assert t.has_claude and t.status()["claude"]["state"] != "missing"
    assert not Translator(TranslateCfg(mode="claude"), {}, tmp_path).has_claude


def test_cli_appearing_later_turns_claude_on(monkeypatch, tmp_path):
    """Subscription token in the Mac app: Claude Code may be installed after the translator was made."""
    found = {"bin": None}
    monkeypatch.setattr("app.translate.router.claude_binary", lambda: found["bin"])
    t = Translator(TranslateCfg(mode="claude", backend="cli"), {}, tmp_path)
    assert not t.has_claude
    found["bin"] = "/usr/local/bin/claude"
    assert t.has_claude


def test_subscription_token_from_the_app_counts_as_token(monkeypatch, tmp_path):
    from app.config import claude_access

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-" + "x" * 40)
    assert claude_access(tmp_path / "none.env") == "token"


def test_env_overrides_win(monkeypatch):
    from app import config

    monkeypatch.setenv("HEARCHURCH_HUB_URL", "wss://hearchurch.com/ws/engine")
    monkeypatch.setenv("HEARCHURCH_DEVICE_TOKEN", "dev-123")
    monkeypatch.setenv("HEARCHURCH_LISTENER_URL", "https://hearchurch.com/c/maple-ward")
    s = config.load_settings()
    assert s.engine.hub_url == "wss://hearchurch.com/ws/engine" and s.engine.device_token == "dev-123"
    assert s.server.public_url == "https://hearchurch.com/c/maple-ward"


def test_home_keeps_writable_files_outside_the_code(tmp_path):
    """With HEARCHURCH_HOME, settings/logs/voices go there and the glossaries are copied there once."""
    code = ("import json; from app import config, languages; "
            "print(json.dumps([str(config.OVERRIDES_PATH), str(config.LOG_DIR), str(config.PIPER_DIR), "
            "str(languages.LANGUAGES['wo'].glossary_file), str(config.MODELS_DIR)]))")
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True,
                         env={"HEARCHURCH_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}).stdout
    import json

    overrides, logs, piper, wo_glossary, models = json.loads(out)
    assert overrides.startswith(str(tmp_path)) and logs.startswith(str(tmp_path)) and piper.startswith(str(tmp_path))
    assert Path(wo_glossary) == tmp_path / "glossary.wo.json" and (tmp_path / "glossary.json").exists()
    assert models == str(root / "models")


def test_app_flag_is_parsed():
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-m", "app", "--help"], cwd=root, capture_output=True, text=True).stdout
    assert "--app" in out


async def test_packages_follow_the_languages(monkeypatch, tmp_path):
    from app import packages

    monkeypatch.setattr(packages, "PIPER_DIR", tmp_path)
    monkeypatch.setattr(packages, "_hf_cached", lambda repo: repo != "bilalfaye/speecht5_tts-wolof")
    fetched = []

    async def fake_fetch(url, dest):
        fetched.append(url)
        dest.write_text("x")

    monkeypatch.setattr(packages, "_fetch", fake_fetch)
    monkeypatch.setattr("huggingface_hub.snapshot_download", lambda repo, **kw: (_ for _ in ()).throw(OSError("offline")))
    p = packages.Packages()
    ready = []
    p.on_ready = lambda: ready.append(1)
    st = {x["id"]: x for x in await p.status(["fr", "wo", "xx"])}
    assert {k: v["state"] for k, v in st.items()} == {"speech": "ready", "fr": "missing", "wo": "missing"}
    assert st["speech"]["done_mb"] == st["speech"]["size_mb"] == 1615 and st["fr"]["done_mb"] == 0
    p.download_missing(["fr", "wo"])
    await asyncio.gather(*(x.task for x in p.items.values() if x.task))
    st = {x["id"]: (x["state"], x["error"]) for x in await p.status(["fr", "wo"])}
    assert st["fr"] == ("ready", "") and st["wo"] == ("failed", "offline") and len(fetched) == 4 and ready == [1]
    assert [x["id"] for x in await p.status(["fr"])] == ["speech", "fr"]


def test_download_progress_counts_real_files_once(tmp_path):
    from app import packages

    (tmp_path / "blobs").mkdir()
    (tmp_path / "blobs" / "abc.incomplete").write_bytes(b"x" * 1000)
    (tmp_path / "snapshots").mkdir()
    (tmp_path / "snapshots" / "w").symlink_to(tmp_path / "blobs" / "abc.incomplete")
    assert packages._bytes_in(tmp_path) == 1000
    pkg = packages.Package("x", "X", size_mb=100, state="downloading", done_bytes=500_000_000)
    assert pkg.public()["done_mb"] == 99.0                     # never shows 100% before it is ready


def test_local_sharing_lets_in_only_this_meetings_key(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.config import Settings

    s = Settings()
    s.server.sharing = "local"
    monkeypatch.setattr(main.S, "settings", s, raising=False)
    monkeypatch.setattr(main.S, "app_mode", True)
    monkeypatch.setattr(main.S, "local_addrs", {"127.0.0.1"}, raising=False)
    monkeypatch.setattr(main.S, "listen_key", "k3y")
    lan = "192.168.1.20"
    assert main._may_listen("127.0.0.1", {}, {})                      # this Mac
    assert not main._may_listen("127.0.0.1", {"cf-ray": "x"}, {})     # relayed: not this Mac
    assert not main._may_listen(lan, {}, {})
    assert not main._may_listen(lan, {}, {"hc_listen": "old"})
    assert main._may_listen(lan, {}, {"hc_listen": "k3y"})
    assert main.listener_url().endswith("/s/k3y")

    c = TestClient(main.app)   # its client address ("testclient") counts as another device on the Wi-Fi
    assert c.get("/s/nope", follow_redirects=False).status_code == 403
    r = c.get("/s/k3y", follow_redirects=False)
    assert r.status_code == 303 and r.cookies.get("hc_listen") == "k3y"
    assert c.get("/static/listener/listener.js").status_code == 200
    for path in ("/operator", "/api/state", "/api/hc/signup", "/admin", "/static/operator/operator.js"):
        assert c.get(path).status_code == 403, path

    s.server.sharing = "cloud"    # on hearchurch.com instead: nobody on the Wi-Fi gets in
    assert not main._may_listen(lan, {}, {"hc_listen": "k3y"})
    main.new_listen_key()
    assert main.S.listen_key != "k3y"


async def test_api_session_forgets_a_reply_with_markup():
    client = FakeClient([_reply("Bonjour."), _reply("antml:reasoning_effort>5"), _reply("Merci.")])
    s = ApiSession("SYSTEM", "sonnet", "", client=client)
    await s.start()
    assert await s.ask("Hello.") == "Bonjour."
    await s.ask("Last days.")               # the router's clean_output drops it; the context must not keep it
    assert [m["role"] for m in s.messages] == ["user", "assistant"]
    assert await s.ask("Thank you.") == "Merci."
    assert "antml" not in str(client.calls[2]["messages"])


def test_speaker_language_switch(monkeypatch):
    """French speaker: English turns on, French pauses (its phones are told), Wolof keeps translating."""
    from app import main
    from app.config import Settings
    from app.pipeline import Pipeline

    settings = Settings()
    pipe = Pipeline.__new__(Pipeline)
    pipe.settings, pipe.channels = settings, {}
    sent: dict[str, list] = {}

    def chan(code, on):
        sent[code] = []
        return SimpleNamespace(code=code, on=on, lang=SimpleNamespace(name=code), activate=lambda: None,
                               hub=SimpleNamespace(publish=lambda m, remember=False, c=code: sent[c].append(m)))
    pipe.channels = {"fr": chan("fr", True), "wo": chan("wo", True), "en": chan("en", False)}
    monkeypatch.setattr(main.S, "pipeline", pipe, raising=False)
    monkeypatch.setattr(main.S, "operator", SimpleNamespace(publish=lambda m: None), raising=False)
    monkeypatch.setattr(main, "full_status", lambda: {})
    assert list(pipe.translating()) == ["fr", "wo"]

    settings.stt.language = "fr"
    main.set_speaker("fr")
    assert pipe.channels["en"].on and list(pipe.translating()) == ["wo", "en"]
    assert {"type": "speaker", "speaker": "fr", "name": "Français"} in sent["fr"]
    assert [l["code"] for l in sent["fr"][-1]["languages"]] == ["fr", "wo", "en"]   # French is still offered

    main.set_languages(["wo"], persist=False)          # the operator's switches leave English alone
    assert pipe.channels["en"].on and not pipe.channels["fr"].on

    settings.stt.language = "en"
    main.set_speaker("en")
    assert not pipe.channels["en"].on and list(pipe.translating()) == ["wo"]
