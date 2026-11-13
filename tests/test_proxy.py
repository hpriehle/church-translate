"""Translation on credits through the hub (mocked Anthropic, never the network): who may translate when, what is
sent on, that nothing of the speech is logged, the engine's "hub" backend and how it is picked."""
import asyncio
import json
import logging
import time
from types import SimpleNamespace as NS

import anthropic
import httpx
import pytest
from anthropic.types.beta import BetaMessage

from app import main
from app.config import TranslateCfg
from app.hear import proxy
from app.hear.mail import RateLimiter
from app.translate.api_session import ApiSession
from app.translate.hub_session import HubClient, HubSession, hub_http_url
from app.translate.router import Translator

from test_hear import client, engine, hear, make_cong, recv, server, start  # noqa: F401  (fixtures)

SPEECH = "Brothers and sisters, the secret sentence 4711."
FRENCH = "Frères et sœurs, la phrase secrète 4711."


def reply(text=FRENCH, stop="end_turn") -> BetaMessage:
    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": text}],
        "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 20}})


class FakeAnthropic:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0) if self.replies else reply()
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def claude(monkeypatch):
    fake = FakeAnthropic()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-hub-key")
    monkeypatch.setattr(proxy, "_anthropic_client", lambda key: fake)
    monkeypatch.setattr(proxy, "limiter", RateLimiter(proxy.REQUESTS_PER_MIN, 60))
    proxy.in_flight.clear()
    return fake


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def turn(text=SPEECH, model="claude-opus-5-5", effort="low"):
    return {"model": model, "system": "SYSTEM", "effort": effort,
            "messages": [{"role": "user", "content": "reply simply: OK"}, {"role": "assistant", "content": "OK"},
                         {"role": "user", "content": text}]}


def go_live(hear, cong, token):
    """A meeting of this Mac live on the hub (what Live._start leaves behind)."""
    did = hear.db.device_by_token(token)["id"]
    hear.live.sessions[cong["id"]] = NS(device_id=did)
    return did


def spend(hear, cong, minutes):
    sid = hear.db.start_session(cong["id"], ["fr"], code="old", device_id=None)
    hear.db.end_session(sid)
    hear.db.record_speech(cong["id"], "old", [{"seq": i, "s": 60.0, "langs": ["fr"]} for i in range(minutes)])


# ------------------------------------------------------------------ the hub route

def test_a_live_meeting_translates_with_the_hubs_key_like_the_mac_would(client, hear, claude):
    cong, token = make_cong(hear)
    go_live(hear, cong, token)
    r = client.post("/api/hc/translate", json=turn(), headers=auth(token))
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["content"][1] == {"type": "text", "text": FRENCH} and got["stop_reason"] == "end_turn"
    assert got["usage"]["cache_read_input_tokens"] == 1000
    call, = claude.calls
    sent = turn()
    assert call["model"] == "claude-opus-5-5" and call["system"] == "SYSTEM" and call["messages"] == sent["messages"]
    assert call["max_tokens"] == 4000 and call["output_config"] == {"effort": "low"}
    assert call["cache_control"] == {"type": "ephemeral"}
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    r = client.post("/api/hc/translate", json=turn(effort=""), headers=auth(token))
    assert r.status_code == 200 and claude.calls[1]["output_config"] is anthropic.omit


def test_who_may_translate(client, hear, claude):
    cong, token = make_cong(hear)
    assert client.post("/api/hc/translate", json=turn()).status_code == 403
    assert client.post("/api/hc/translate", json=turn(), headers=auth("nope")).status_code == 403
    # credits, but no meeting under way on the hub (online or Wi-Fi-only): never metered, so no
    r = client.post("/api/hc/translate", json=turn(), headers=auth(token))
    assert r.status_code == 409 and r.json()["error"] == "not_live" and "internet" in r.json()["message"]
    # another Mac of the congregation while this one is live: no
    other = hear.db.add_device(cong["id"], "Second Mac")
    go_live(hear, cong, token)
    assert client.post("/api/hc/translate", json=turn(), headers=auth(other)).json()["error"] == "not_live"
    assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).status_code == 200
    # own key: straight from the Mac, never through us
    hear.db._x("UPDATE congregations SET billing = 'own_key' WHERE id = ?", (cong["id"],))
    r = client.post("/api/hc/translate", json=turn(), headers=auth(token))
    assert r.status_code == 409 and r.json()["error"] == "own_key"
    assert len(claude.calls) == 1


def test_the_last_sentences_after_a_meeting_ended_still_translate(client, hear, claude):
    cong, token = make_cong(hear)
    did = hear.db.device_by_token(token)["id"]
    sid = hear.db.start_session(cong["id"], ["fr"], code="abc", device_id=did)
    hear.db.end_session(sid)
    assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).status_code == 200
    hear.db._x("UPDATE sessions SET ended_at = ? WHERE id = ?", (time.time() - proxy.TRAILING_S - 1, sid))
    assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).json()["error"] == "not_live"


def test_out_of_credit_refuses_only_outside_a_meeting_under_way(client, hear, claude):
    cong, token = make_cong(hear)
    spend(hear, cong, 90)                                           # free hour gone, 30 min owed
    r = client.post("/api/hc/translate", json=turn(), headers=auth(token))
    assert r.status_code == 402 and r.json()["error"] == "out_of_credit" and "-$2.50" in r.json()["message"]
    go_live(hear, cong, token)                                      # a meeting that started with credit runs on
    assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).status_code == 200


def test_the_free_hour_counts_as_credit(client, hear, claude):
    cong, token = make_cong(hear)
    spend(hear, cong, 59)                                           # a minute of the free hour left: credit
    assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).json()["error"] == "not_live"
    hear.db.start_session(cong["id"], ["fr"], code="o2")
    hear.db.record_speech(cong["id"], "o2", [{"seq": 1, "s": 60.0, "langs": ["fr"]}])
    assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).status_code == 402


def test_not_configured_without_the_servers_key(client, hear, claude, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    cong, token = make_cong(hear)
    go_live(hear, cong, token)
    r = client.post("/api/hc/translate", json=turn(), headers=auth(token))
    assert r.status_code == 503 and r.json()["error"] == "not_configured"
    assert client.get("/api/hc/billing", headers=auth(token)).status_code == 200   # the rest works
    assert claude.calls == []


def test_bad_requests_are_refused_without_quoting_them(client, hear, claude, monkeypatch):
    cong, token = make_cong(hear)
    go_live(hear, cong, token)
    for body in (turn(model="claude-some-other"), turn(effort="huge"), {**turn(), "messages": []},
                 {**turn(), "messages": [{"role": "system", "content": SPEECH}]}, {"model": "claude-opus-5-5"}):
        r = client.post("/api/hc/translate", json=body, headers=auth(token))
        assert r.status_code == 400 and SPEECH not in r.text, body
    r = client.post("/api/hc/translate", content=b"not json", headers=auth(token))
    assert r.status_code == 400
    monkeypatch.setattr(proxy, "MAX_BODY", 100)
    assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).status_code == 413
    assert claude.calls == []


def test_rate_and_concurrency_limits_per_congregation(client, hear, claude, monkeypatch):
    cong, token = make_cong(hear)
    other, other_token = make_cong(hear, "Thiès Branch", "b@example.org")
    go_live(hear, cong, token)
    go_live(hear, other, other_token)
    monkeypatch.setattr(proxy, "limiter", RateLimiter(2, 60))
    assert [client.post("/api/hc/translate", json=turn(), headers=auth(token)).status_code
            for _ in range(3)] == [200, 200, 429]
    assert client.post("/api/hc/translate", json=turn(), headers=auth(other_token)).status_code == 200
    monkeypatch.setattr(proxy, "limiter", RateLimiter(100, 60))
    proxy.in_flight[cong["id"]] = proxy.IN_FLIGHT                   # that many turns already out
    r = client.post("/api/hc/translate", json=turn(), headers=auth(token))
    assert r.status_code == 429 and r.json()["error"] == "busy"
    proxy.in_flight.clear()
    assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).status_code == 200
    assert proxy.in_flight == {}


def test_anthropic_failures_map_to_plain_answers(client, hear, claude):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    def err(cls, status):
        return cls(SPEECH, response=httpx.Response(status, request=req), body=None)
    claude.replies = [err(anthropic.RateLimitError, 429), err(anthropic.AuthenticationError, 401),
                      anthropic.APIConnectionError(request=req), err(anthropic.BadRequestError, 400),
                      err(anthropic.InternalServerError, 529)]
    cong, token = make_cong(hear)
    go_live(hear, cong, token)
    got = [client.post("/api/hc/translate", json=turn(), headers=auth(token)) for _ in range(5)]
    assert [(r.status_code, r.json()["error"]) for r in got] == [
        (429, "anthropic_busy"), (503, "unavailable"), (502, "unreachable"), (400, "bad_request"), (502, "unreachable")]
    assert not any(SPEECH in r.text for r in got)                 # Anthropic's error text isn't passed on either
    assert proxy.in_flight == {}


def test_nothing_of_the_speech_or_translation_is_logged_or_stored(client, hear, claude, caplog, tmp_path):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    claude.replies = [reply(), anthropic.BadRequestError(SPEECH, response=httpx.Response(400, request=req), body=None)]
    cong, token = make_cong(hear)
    go_live(hear, cong, token)
    with caplog.at_level(logging.DEBUG):
        assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).status_code == 200
        assert client.post("/api/hc/translate", json=turn(), headers=auth(token)).status_code == 400
        assert client.post("/api/hc/translate", json=turn(model="x"), headers=auth(token)).status_code == 400
    assert caplog.records                                          # it does log (metadata)
    for rec in caplog.records:
        assert "4711" not in rec.getMessage(), rec.getMessage()
    hear.db._db.commit()
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert b"4711" not in f.read_bytes(), f


# ------------------------------------------------------------------ the engine's hub backend

def mock_hub(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_hub_session_keeps_the_seed_for_the_first_sentence():
    sent = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer dev-token"
        assert request.url == "https://hearchurch.test/api/hc/translate"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=reply().model_dump(mode="json", exclude_none=True))

    s = HubSession("SYSTEM", "opus", "low", client=HubClient("https://hearchurch.test/", "dev-token", mock_hub(handler)))
    await s.start()
    assert await s.ask("The meeting is about to start. For this first message only, reply simply: OK") == "OK"
    assert sent == []                                              # an open app with no meeting asks nothing
    s.ready = True
    assert await s.ask(SPEECH) == FRENCH
    first, = sent
    assert first["model"] == "claude-opus-5-5" and first["effort"] == "low" and first["system"] == "SYSTEM"
    assert [m["role"] for m in first["messages"]] == ["user", "assistant", "user"]
    assert first["messages"][2]["content"] == SPEECH
    assert await s.ask("Thank you.") == FRENCH
    # the reply goes back unchanged, thinking block included, like the api backend
    assert sent[1]["messages"][3]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    assert s.usage.turns == 2 and s.usage.cache_read == 2000 and s.auth.startswith("Hear Church credits")
    await s.close()


async def test_hub_session_errors_drop_the_sentence_and_read_plainly():
    answers = [httpx.Response(409, json={"error": "not_live", "message": "Translation on credit works only while…"}),
               httpx.Response(403, json={"detail": "Unknown device: sign in again."})]
    s = HubSession("S", "sonnet", "", client=HubClient("https://h.test", "t", mock_hub(lambda r: answers.pop(0))))
    await s.start()
    s.ready = True
    with pytest.raises(RuntimeError, match="works only while"):
        await s.ask(SPEECH)
    assert s.messages == [] and s.alive and not s.busy
    with pytest.raises(RuntimeError, match="sign in again"):
        await s.ask(SPEECH)
    assert not s.alive                                             # signed out: this session is done for

    def down(request):
        raise httpx.ConnectError("no route")
    s = HubSession("S", "opus", "low", client=HubClient("https://h.test", "t", mock_hub(down)))
    await s.start()
    s.ready = True
    with pytest.raises(RuntimeError, match="cannot reach Hear Church"):
        await s.ask(SPEECH)
    assert s.alive


async def test_hub_session_needs_a_signed_in_mac(hear):
    main.S.settings.engine.hub_url, main.S.settings.engine.device_token = "", ""
    with pytest.raises(RuntimeError, match="isn't signed in"):
        await HubSession("S", "opus", "low").start()
    assert hub_http_url("wss://hearchurch.com/ws/engine") == "https://hearchurch.com"
    assert hub_http_url("ws://127.0.0.1:8000/ws/engine") == "http://127.0.0.1:8000"


def test_router_uses_the_hub_backend(monkeypatch, tmp_path):
    monkeypatch.setattr("app.translate.router.claude_binary", lambda: None)
    t = Translator(TranslateCfg(mode="claude", backend="hub"), {}, tmp_path)
    assert t.has_claude
    from app.translate import router
    assert router.SESSION_TYPES["hub"] is HubSession and issubclass(HubSession, ApiSession)


async def test_end_to_end_a_mac_without_a_key_translates_a_live_meeting(server, hear, claude):
    cong, token = make_cong(hear)
    ws = await engine(server, token)
    assert (await start(ws, ["fr"]))["type"] == "session_started"
    s = HubSession("SYSTEM", "opus", "low", client=HubClient(f"http://127.0.0.1:{server}", token))
    await s.start()
    await s.ask("reply simply: OK")
    s.ready = True
    assert await s.ask(SPEECH) == FRENCH
    assert claude.calls[0]["messages"][-1] == {"role": "user", "content": SPEECH}
    await ws.send(json.dumps({"type": "session_stop"}))
    await asyncio.sleep(0.1)
    assert await s.ask("Amen.") == FRENCH                         # the last sentences, just after it ended
    await s.close()
    await ws.close()


def test_the_app_picks_the_backend_from_billing(hear, monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.SECRETS_PATH", tmp_path / "none.env")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(main.S, "app_mode", True)
    main.S.settings.engine.hub_url, main.S.settings.engine.device_token = "wss://hearchurch.com/ws/engine", "dev"
    assert main.app_backend() == "hub"                             # no key: credits, before the hub has said
    assert main.app_backend("own_key") == "api"                    # own key but none passed: "no API key" as before
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-church")
    assert main.app_backend() == "api" and main.app_backend("credits") == "hub"

    restarted = []
    tr = NS(cfg=TranslateCfg(backend="api"), restart_claude=lambda: restarted.append(1))
    monkeypatch.setattr(main.S, "pipeline", NS(channels={"fr": NS(translator=tr)}), raising=False)
    main.S.settings.translate.backend = "api"
    main.use_billing("credits")
    assert main.S.settings.translate.backend == "hub" and tr.cfg.backend == "hub" and restarted == [1]
    main.use_billing("credits")
    assert restarted == [1]                                        # unchanged: no restart
    main.use_billing("own_key")
    assert tr.cfg.backend == "api" and restarted == [1, 1]
    main.use_billing("something else")
    assert tr.cfg.backend == "api"
