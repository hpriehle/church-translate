"""FastAPI app: listener page + MP3 stream + caption WebSocket, operator page + control API."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import netinfo
from .audio_in import BrowserSource, list_input_devices
from .broadcast.hub import Hub
from .broadcast.mixer import Mixer
from .broadcast.stream import Mp3Broadcaster
from .channel import Channel
from .config import (ROOT, SAMPLES_DIR, WEB_DIR, Settings, VoiceCfg, claude_access, load_glossary, load_settings,
                     save_glossary, save_override, save_secrets, tts_for, validate_glossary)
from .languages import LANGUAGES
from .pipeline import Pipeline
from .stt import Transcriber
from .translate.router import Translator
from .tts.engines import TtsRunner, list_voices_for

log = logging.getLogger("app")

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov"}
EDITABLE = {
    "audio": {"input_device", "input_channel", "input_gain"},
    "translate": {"mode", "claude_model", "claude_effort", "claude_timeout_s", "ollama_model", "claude_session_turns"},
    "tts": {"engine", "voice", "rate", "volume"},
    "backlog": {"speedup_start_s", "speedup_full_s", "max_speed", "condense_on_s", "condense_off_s", "drop_above_s"},
    "server": {"lan_ip", "use_public_url"},
}


class State:
    settings: Settings
    glossaries: dict[str, dict[str, Any]]
    pipeline: Pipeline
    operator: Hub
    started: float
    local_addrs: set[str]
    autostart: dict | None = None
    exit_when_done: bool = False
    port_override: int | None = None
    mode_override: str | None = None
    tunnel: dict = {"state": "unknown"}
    argv: list[str] = []
    version: str = ""


S = State()


def channel(code: str | None = None) -> Channel:
    """The channel for a language code (even if switched off, so its phones resume when it's back on);
    the first language being translated if the code is unknown or omitted."""
    chans = S.pipeline.channels
    return chans.get(code or "") or next(iter(S.pipeline.on_channels.values()), None) or next(iter(chans.values()))


def languages_public() -> list[dict[str, str]]:
    """The languages listeners can pick: the ones being translated right now."""
    return [{"code": c, "name": ch.lang.name} for c, ch in S.pipeline.on_channels.items()]


def set_languages(codes: list[str]) -> list[str]:
    """Switch languages on/off live, remember the choice, and tell every phone and the operator."""
    on = [c for c in S.pipeline.channels if c in codes]
    for c, ch in S.pipeline.channels.items():
        was, ch.on = ch.on, c in on
        if ch.on:
            ch.activate()
        if ch.on != was:
            ch.hub.publish({"type": "lang_state", "on": ch.on})
            log.info("%s translation switched %s", ch.lang.name, "on" if ch.on else "off")
    S.settings.languages = on
    save_override("languages", on)
    for ch in S.pipeline.channels.values():
        ch.hub.publish({"type": "languages", "languages": languages_public()})
    S.operator.publish(full_status())
    return on


def listener_url() -> str:
    if S.settings.server.public_url and S.settings.server.use_public_url:
        return S.settings.server.public_url
    ip = netinfo.best_lan_ip(S.settings.server.lan_ip)
    port = S.settings.server.port
    return f"http://{ip}/" if port == 80 else f"http://{ip}:{port}/"


def _code_version() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%h · %cd", "--date=format:%b %d %H:%M"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _managed_by() -> str:
    import os

    if os.environ.get("XPC_SERVICE_NAME", "").startswith(("org.church-translate", "com.clntacq.")):
        return "launchd (starts at login)"
    if os.environ.get("INVOCATION_ID"):
        return "systemd"
    return "terminal"


def server_info() -> dict[str, Any]:
    return {"started": S.started, "version": S.version, "managed_by": _managed_by()}


def full_status() -> dict[str, Any]:
    return {
        "type": "status",
        "time": time.time(),
        "server": server_info(),
        "pipeline": S.pipeline.status(),
        "translate": {c: ch.translator.status() for c, ch in S.pipeline.on_channels.items()},
        "languages": languages_public(),
        "language_switches": [{"code": c, "name": ch.lang.name, "on": ch.on} for c, ch in S.pipeline.channels.items()],
        "listeners": {"stream": sum(ch.broadcaster.listeners for ch in S.pipeline.channels.values()),
                      "captions": sum(ch.hub.count for ch in S.pipeline.channels.values())},
        "listener_url": listener_url(),
        "tunnel": S.tunnel,
        "claude_access": claude_access(),
    }


async def tunnel_loop() -> None:
    """Is the public link up? Asks the Cloudflare tunnel running on this machine (if any)."""
    import httpx

    async with httpx.AsyncClient(timeout=2) as c:
        while True:
            try:
                r = await c.get(f"http://{S.settings.server.tunnel_metrics}/ready")
                n = int((r.json() or {}).get("readyConnections") or 0)
                S.tunnel = {"state": "connected" if r.status_code == 200 and n else "offline", "connections": n}
            except Exception:
                # No tunnel on this machine (local-network only, or the tunnel runs elsewhere).
                S.tunnel = {"state": "not running", "connections": 0}
            await asyncio.sleep(5)


async def status_loop() -> None:
    while True:
        await asyncio.sleep(1.0)
        if S.operator.count:
            S.operator.publish(full_status())


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.settings = load_settings()
    if S.port_override:
        S.settings.server.port = S.port_override
    if S.mode_override:
        S.settings.translate.mode = S.mode_override
    S.started = time.time()
    S.version = _code_version()
    S.local_addrs = netinfo.local_addresses()
    st = S.settings
    # A channel for every language; the ones in settings.languages are translated (switchable live).
    langs = list(LANGUAGES.values())
    S.glossaries = {lang.code: load_glossary(lang.glossary_file) for lang in langs}
    S.operator = Hub("operator", queue_size=2000)
    S.pipeline = Pipeline(st, S.glossaries, S.operator, Transcriber(st.stt))
    sr = st.stream.sample_rate
    for lang in langs:
        # One translator, voice, mixer, MP3 stream and caption feed per language.
        b = Mp3Broadcaster(sr, st.stream.bitrate_kbps, st.stream.burst_s)
        m = Mixer(sr, on_frame=b.feed)
        m.volume = st.tts.volume
        tr = Translator(st.translate, S.glossaries[lang.code], ROOT / "data" / "claude-workdir", lang)
        tts = TtsRunner(tts_for(st, lang.code), sr, lazy=True)
        ch = Channel(S.pipeline, lang, tr, tts, m, b, Hub(f"listen-{lang.code}", history=300, queue_size=400))
        ch.on = lang.code in st.languages
        S.pipeline.add_channel(ch)
    S.pipeline.start_tasks()
    bg = [asyncio.create_task(status_loop()), asyncio.create_task(S.pipeline.load_stt()),
          asyncio.create_task(tunnel_loop())]
    for ch in S.pipeline.channels.values():
        ch.mixer.start()
        if ch.on:  # a language switched off stays unloaded until it is switched on
            ch.activate()

    if S.exit_when_done:
        def _drained():
            log.info("test run complete — shutting down")
            asyncio.get_running_loop().call_later(0.5, _shutdown)
        S.pipeline.on_drained = _drained
    if S.autostart:
        async def _auto():
            # Give Claude/Ollama/STT a moment so the first chunks aren't measured against cold models.
            t0 = time.monotonic()
            while time.monotonic() - t0 < 120:
                chans = S.pipeline.on_channels.values()
                claude_ok = all(ch.translator.status()["claude"]["state"] in ("ready", "disabled", "missing") for ch in chans)
                ollama_ok = all(not ch.translator.uses_ollama or S.settings.translate.mode != "ollama"
                                or ch.translator.ollama.warm or not ch.translator.ollama.available for ch in chans)
                tts_ok = all(ch.tts.ready.is_set() for ch in chans)
                if S.pipeline.stt.ready and claude_ok and ollama_ok and tts_ok:
                    break
                await asyncio.sleep(0.5)
            log.info("autostart after %.1fs warm-up", time.monotonic() - t0)
            S.pipeline.start(**S.autostart)
        bg.append(asyncio.create_task(_auto()))

    url = listener_url()
    log.info("Listener page: %s", url)
    log.info("Operator page: http://localhost:%d/operator", st.server.port)
    yield
    for t in bg:
        t.cancel()
    S.pipeline.stop()
    S.pipeline.release_awake()
    for ch in S.pipeline.channels.values():
        await ch.translator.close()


def _shutdown() -> None:
    import os
    import signal

    os.kill(os.getpid(), signal.SIGINT)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    # Without this, a CDN/tunnel in front (Cloudflare adds a 4 h browser cache) keeps phones on old JS.
    response = await call_next(request)
    if request.url.path.startswith("/static/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-cache"
    return response


# ------------------------------------------------------------------ access control

_ASSET = re.compile(r'((?:src|href)=")(/static/([^"?]+\.(?:js|css)))(")')


def page(path: Path) -> HTMLResponse:
    """Serve an HTML page with its JS/CSS links stamped with the file's version.

    Cloudflare rewrites static files to a 4-hour browser cache; versioned links mean a phone can
    never mix a new page with an old cached script.
    """
    def stamp(m: re.Match) -> str:
        f = WEB_DIR / m.group(3)
        v = int(f.stat().st_mtime) if f.exists() else 0
        return f"{m.group(1)}{m.group(2)}?v={v}{m.group(4)}"

    return HTMLResponse(_ASSET.sub(stamp, path.read_text(encoding="utf-8")), headers={"Cache-Control": "no-cache"})


_PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-real-ip", "forwarded")


OPERATOR, SPEAKER = "operator", "speaker"


def _cookie_token(pin: str, role: str = OPERATOR) -> str:
    """What the browser keeps after signing in: a fingerprint of the password, not the password."""
    import hashlib

    tag = "church-translate" if role == OPERATOR else "church-translate:speaker"
    return hashlib.sha256(f"{tag}:{pin}".encode()).hexdigest()


def _passwords() -> list[tuple[str, str]]:
    srv = S.settings.server
    return [(r, p) for r, p in ((OPERATOR, srv.operator_pin), (SPEAKER, srv.speaker_pin)) if p]


def _role(host: str | None, headers, query, cookies) -> str | None:
    """Who is asking: "operator" (a browser on this machine, or the operator password), "speaker" (the
    speaker password: the speak page only), or None.

    Requests relayed by a tunnel/reverse proxy also come from localhost, so they never count as local.
    """
    import hmac

    proxied = any(h in headers for h in _PROXY_HEADERS)
    if not proxied and host and host.split("%")[0] in S.local_addrs:
        return OPERATOR
    given = query.get("key") or query.get("pin") or ""
    cookie = cookies.get("op_pin") or ""
    for role, want in _passwords():
        if given and hmac.compare_digest(given, want):
            return role
        if cookie and hmac.compare_digest(cookie, _cookie_token(want, role)):
            return role
    return None


def _request_role(request: Request) -> str | None:
    return _role(request.client.host if request.client else None, request.headers, request.query_params, request.cookies)


def _is_operator(host: str | None, headers, query, cookies) -> bool:
    return _role(host, headers, query, cookies) == OPERATOR


def require_operator(request: Request) -> None:
    role = _request_role(request)
    if role != OPERATOR:
        raise HTTPException(403, "Only the operator password can do this." if role
                            else "Not signed in: open /login (or use the computer running the server).")


def require_speaker(request: Request) -> str:
    """The speak page's actions: allowed with either password. Returns the role."""
    role = _request_role(request)
    if role is None:
        raise HTTPException(403, "Not signed in: open /login (or use the computer running the server).")
    return role


def _set_login_cookie(request: Request, resp: Response, role: str = OPERATOR) -> Response:
    pin = S.settings.server.operator_pin if role == OPERATOR else S.settings.server.speaker_pin
    resp.set_cookie("op_pin", _cookie_token(pin, role), httponly=True, samesite="lax",
                    max_age=30 * 86400, secure=request.headers.get("x-forwarded-proto") == "https")
    return resp


def _remember_key(request: Request, resp: Response) -> Response:
    import hmac

    key = request.query_params.get("key") or request.query_params.get("pin")
    for role, want in _passwords() if key else []:
        if hmac.compare_digest(key, want):
            return _set_login_cookie(request, resp, role)
    return resp


def _operator_page(request: Request, path: Path, speaker_ok: bool = False) -> Response:
    """An operator page, or the sign-in page if this browser isn't signed in yet. With the speaker
    password, pages other than the speak/print pages lead to the speak page."""
    from fastapi.responses import RedirectResponse
    from urllib.parse import quote

    role = _request_role(request)
    if role is None:
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
    if role == SPEAKER and not speaker_ok:
        return RedirectResponse("/speak", status_code=303)
    return _remember_key(request, page(path))


def _public_settings() -> dict[str, Any]:
    """Settings for the pages: the passwords are never sent, only whether they are set."""
    d = S.settings.model_dump()
    for k in ("operator_pin", "speaker_pin"):
        d["server"][k] = "set" if d["server"].get(k) else ""
    return d


_login_fails: dict[str, list[float]] = {}


@app.get("/guide", response_class=HTMLResponse)
async def guide_page():
    """How to run a meeting from a phone (public: no password in it)."""
    return page(WEB_DIR / "guide.html")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return page(WEB_DIR / "login.html")


@app.post("/login")
async def login(request: Request):
    import hmac
    from fastapi.responses import RedirectResponse
    from urllib.parse import parse_qs, quote

    form = parse_qs((await request.body()).decode(errors="replace"))
    password = (form.get("password") or [""])[0]
    nxt = (form.get("next") or ["/operator"])[0]
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/operator"
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < 900]
    if len(fails) >= 10:
        return RedirectResponse(f"/login?error=busy&next={quote(nxt)}", status_code=303)
    for role, want in _passwords():
        if hmac.compare_digest(password, want):
            _login_fails.pop(ip, None)
            if role == SPEAKER and nxt not in ("/speak", "/print"):
                nxt = "/speak"
            return _set_login_cookie(request, RedirectResponse(nxt, status_code=303), role)
    fails.append(now)
    _login_fails[ip] = fails
    await asyncio.sleep(min(5, len(fails)))  # slow down guessing
    return RedirectResponse(f"/login?error=1&next={quote(nxt)}", status_code=303)


# ------------------------------------------------------------------ listener side

@app.get("/", response_class=HTMLResponse)
async def listener_page():
    return page(WEB_DIR / "listener" / "index.html")


@app.get("/stream.mp3")
async def stream_default(request: Request):
    return _stream(channel())


@app.get("/stream/{code}.mp3")
async def stream_lang(code: str):
    if code not in S.pipeline.channels:
        raise HTTPException(404)
    return _stream(channel(code))


def _stream(ch: Channel) -> StreamingResponse:
    broadcaster = ch.broadcaster
    sub, burst = broadcaster.subscribe()

    async def body():
        try:
            if burst:
                yield burst
            while True:
                data = await sub.queue.get()
                if data is None:
                    return
                yield data
        finally:
            broadcaster.unsubscribe(sub)

    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache",
               "X-Content-Type-Options": "nosniff", "Access-Control-Allow-Origin": "*",
               "icy-name": f"Traduction en direct ({ch.lang.name})"}
    return StreamingResponse(body(), media_type="audio/mpeg", headers=headers)


@app.get("/clips/{code}/{seq}.mp3")
async def clip(code: str, seq: int):
    data = channel(code).clips.get(seq) if code in S.pipeline.channels else None
    if data is None:
        raise HTTPException(404)
    # no-store: clip numbers restart with the server, so a cached clip could be the wrong one.
    return Response(data, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.websocket("/ws/listen")
async def ws_listen(ws: WebSocket):
    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        hello = {}
    # Only the current session is ever shown. A phone reconnecting within the same session, server
    # boot and language gets what it missed; anyone else gets the current session from its start.
    ch = channel(hello.get("lang") or ws.query_params.get("lang"))
    session = S.pipeline.session_id
    same = hello.get("boot") == S.started and hello.get("session") == session and hello.get("lang") == ch.code
    last = int(hello.get("last_seq") or 0) if same else 0
    init = {"type": "init", "boot": S.started, "session": session, "live": S.pipeline.running,
            "lang": ch.code, "on": ch.on, "languages": languages_public(),
            "server_time": time.time(), "history": ch.hub.replay_after(last) if session else []}
    await ch.hub.serve(ws, [init])


@app.get("/qr.svg")
async def qr(dark: str = "#000", light: str = "#fff"):
    return Response(netinfo.qr_svg(listener_url(), dark=dark, light=light), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/qr")
async def api_qr(request: Request, text: str):
    require_speaker(request)
    return Response(netinfo.qr_svg(text), media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    return {"ok": True, "listeners": sum(ch.broadcaster.listeners for ch in S.pipeline.channels.values()),
            "running": S.pipeline.running, "started": S.started, "languages": list(S.pipeline.on_channels)}


# ------------------------------------------------------------------ operator side

@app.get("/operator", response_class=HTMLResponse)
async def operator_page(request: Request):
    return _operator_page(request, WEB_DIR / "operator" / "index.html")


@app.get("/speak", response_class=HTMLResponse)
async def speak_page(request: Request):
    return _operator_page(request, WEB_DIR / "speak" / "index.html", speaker_ok=True)


@app.websocket("/ws/speak")
async def ws_speak(ws: WebSocket):
    """Browser microphone → pipeline. First message: {"type":"hello","sample_rate":48000}; then int16 PCM frames."""
    if _role(ws.client.host if ws.client else None, ws.headers, ws.query_params, ws.cookies) is None:
        await ws.close(code=4403)
        return
    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
        rate = int(hello.get("sample_rate") or 48000)
        if not 8000 <= rate <= 192000:
            raise ValueError("bad sample rate")
    except Exception:
        await ws.close(code=1003)
        return
    source = BrowserSource(rate, label=f"browser microphone ({hello.get('device') or 'web page'})")
    try:
        S.pipeline.start("browser", source=source, record=bool(hello.get("record")))
    except Exception as e:
        await ws.send_json({"type": "error", "message": str(e)})
        await ws.close()
        return
    await ws.send_json({"type": "started"})
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                source.push(msg["bytes"])
            elif msg.get("text") and '"stop"' in msg["text"]:
                break
    except Exception:
        pass
    finally:
        source.stop()


@app.get("/print", response_class=HTMLResponse)
async def print_page(request: Request):
    return _operator_page(request, WEB_DIR / "print.html", speaker_ok=True)


@app.websocket("/ws/operator")
async def ws_operator(ws: WebSocket):
    if _role(ws.client.host if ws.client else None, ws.headers, ws.query_params, ws.cookies) is None:
        await ws.close(code=4403)
        return
    await ws.accept()
    await S.operator.serve(ws, [full_status()])


def _samples() -> list[dict]:
    SAMPLES_DIR.mkdir(exist_ok=True)
    return [{"name": p.name, "mb": round(p.stat().st_size / 1e6, 1)}
            for p in sorted(SAMPLES_DIR.iterdir()) if p.suffix.lower() in AUDIO_EXT]


@app.get("/api/state")
async def api_state(request: Request):
    role = require_speaker(request)
    return {
        "role": role,
        "settings": _public_settings(),
        "devices": list_input_devices(),
        "voices": {c: list_voices_for(c) for c in S.pipeline.channels},
        "samples": _samples(),
        "languages": languages_public(),
        "all_languages": [{"code": l.code, "name": l.name} for l in LANGUAGES.values()],
        "glossaries": S.glossaries,
        "listener_url": listener_url(),
        "lan_candidates": netinfo.candidate_ips(),
        "status": full_status(),
        "platform": sys.platform,
    }


@app.post("/api/start")
async def api_start(request: Request):
    require_operator(request)
    body = await request.json()
    kind = body.get("source", "mic")
    try:
        if kind == "file":
            name = Path(body.get("file", "")).name
            path = SAMPLES_DIR / name
            S.pipeline.start("file", file=path, speed=float(body.get("speed", 1.0)),
                             start_s=float(body.get("start_s", 0)), record=bool(body.get("record", False)),
                             max_s=float(body["duration"]) if body.get("duration") else None)
        else:
            S.pipeline.start("mic", device=body.get("device"), record=bool(body.get("record", False)))
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/stop")
async def api_stop(request: Request):
    require_speaker(request)
    S.pipeline.stop()
    return {"ok": True}


# What the speaker password may change (from the speak page): languages and voices, nothing else.
SPEAKER_EDITABLE = {"languages": None, "voices": None, "tts": {"engine", "voice"}}


@app.post("/api/settings")
async def api_settings(request: Request):
    role = require_speaker(request)
    body: dict[str, Any] = await request.json()
    if role == SPEAKER:
        for section, values in body.items():
            allowed = SPEAKER_EDITABLE.get(section, set())
            if section not in SPEAKER_EDITABLE or (allowed is not None and set(values) - allowed):
                raise HTTPException(403, "Only the operator password can change that setting.")
    data = S.settings.model_dump()
    note = ""
    for section, values in body.items():
        if section == "voices":  # {"wo": {"engine": "speecht5", "voice": "female"}}
            for code, v in values.items():
                if code in LANGUAGES and code != "fr":
                    data["voices"][code] = VoiceCfg(**v).model_dump()
            save_override("voices", data["voices"])
            continue
        if section == "languages":  # live: each language can be switched on/off at any time
            data["languages"] = set_languages([c for c in values if c in LANGUAGES])
            continue
        allowed = EDITABLE.get(section, set())
        clean = {k: v for k, v in values.items() if k in allowed}
        if not clean:
            continue
        data[section].update(clean)
        save_override(section, clean)
    try:
        new = Settings.model_validate(data)
    except Exception as e:
        raise HTTPException(400, str(e))
    S.settings = new
    S.pipeline.apply_settings(new, S.glossaries)
    return {"ok": True, "settings": _public_settings(), "message": note}


@app.post("/api/speaker-password")
async def api_speaker_password(request: Request):
    """Set, change or remove the speaker password (write-only). Changing it signs out every speaker."""
    require_operator(request)
    pw = str((await request.json()).get("password") or "").strip()
    if pw and (len(pw) < 6 or len(pw) > 100):
        raise HTTPException(400, "Use 6 to 100 characters.")
    if pw and pw == S.settings.server.operator_pin:
        raise HTTPException(400, "Use a different password from the operator password.")
    S.settings.server.speaker_pin = pw
    save_override("server", {"speaker_pin": pw})
    log.info("speaker password %s", "set" if pw else "removed")
    return {"ok": True, "set": bool(pw),
            "message": "Speaker password saved. Earlier speaker sign-ins no longer work." if pw else "Speaker password removed."}


CLAUDE_CREDENTIALS = {  # kind → (variable in data/secrets.env, how it starts, name for messages)
    "token": ("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat", "subscription token"),
    "api_key": ("ANTHROPIC_API_KEY", "sk-ant-api", "API key"),
}


@app.post("/api/claude-access")
async def api_claude_access(request: Request):
    """Save a Claude subscription token or API key for the translator. Write-only: it is never sent back."""
    require_operator(request)
    body: dict[str, Any] = await request.json()
    kind, value = body.get("kind"), str(body.get("value") or "").strip()
    if kind == "login":  # forget saved credentials; use this computer's own Claude login
        save_secrets({})
        message = "Removed. Claude uses this computer's own login again."
    elif kind in CLAUDE_CREDENTIALS:
        var, prefix, name = CLAUDE_CREDENTIALS[kind]
        if not value.startswith(prefix) or not re.fullmatch(r"sk-ant-[A-Za-z0-9_-]{20,400}", value):
            hint = (" If you copied a code from the browser, paste that code into Terminal where claude "
                    "setup-token is waiting; Terminal then prints the token.") if kind == "token" else ""
            raise HTTPException(400, f"That isn't the {name}: it should start with {prefix}….{hint}")
        save_secrets({var: value})  # one credential at a time
        message = f"Saved the {name}. Claude restarts with it in a few seconds."
    else:
        raise HTTPException(400, "unknown kind")
    for ch in S.pipeline.channels.values():
        ch.translator.restart_claude()
    log.info("Claude credentials changed: %s", claude_access())
    return {"ok": True, "access": claude_access(), "message": message}


@app.get("/api/glossary")
async def api_glossary_get(request: Request, lang: str = "fr"):
    require_operator(request)
    return S.glossaries.get(lang) or load_glossary(LANGUAGES[lang].glossary_file) if lang in LANGUAGES else {}


@app.post("/api/glossary")
async def api_glossary_set(request: Request, lang: str = "fr"):
    require_operator(request)
    if lang not in LANGUAGES:
        raise HTTPException(400, "unknown language")
    try:
        g = validate_glossary(await request.json())
        save_glossary(g, LANGUAGES[lang].glossary_file)
    except Exception as e:
        raise HTTPException(400, f"Invalid glossary: {e}")
    S.glossaries[lang] = g
    S.pipeline.apply_settings(S.settings, S.glossaries)
    return {"ok": True, "message": "Glossary saved. Claude restarts with it in the background (Ollama and speech recognition use it immediately)."}


@app.post("/api/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    require_operator(request)
    name = Path(file.filename or "upload").name
    if Path(name).suffix.lower() not in AUDIO_EXT:
        raise HTTPException(400, "unsupported file type")
    SAMPLES_DIR.mkdir(exist_ok=True)
    dest = SAMPLES_DIR / name
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    return {"ok": True, "samples": _samples()}


@app.post("/api/test-voice")
async def api_test_voice(request: Request):
    require_speaker(request)
    try:
        await S.pipeline.say_test()
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


_restarting = False


async def _restart_in_place() -> None:
    """Replace this process with a fresh copy of the server (same PID, same login session).

    Staying in the same process keeps whatever started it (Terminal, launchd, systemd) and
    therefore keeps access to the macOS keychain that Claude Code needs.
    """
    import os

    S.operator.publish({"type": "restarting"})
    for ch in S.pipeline.channels.values():
        ch.hub.publish({"type": "status", "live": False})
    await asyncio.sleep(0.5)  # let the HTTP reply and the notices go out
    try:
        S.pipeline.stop()
        S.pipeline.release_awake()
        await asyncio.wait_for(asyncio.gather(*(ch.translator.close() for ch in S.pipeline.channels.values())), 8)
    except Exception:
        log.exception("cleanup before restart failed (continuing)")
    git = shutil.which("git")
    if S.settings.server.update_from_git and git and (ROOT / ".git").exists():
        proc = await asyncio.create_subprocess_exec(git, "pull", "--ff-only", "--quiet", cwd=str(ROOT))
        try:
            await asyncio.wait_for(proc.wait(), 60)
        except asyncio.TimeoutError:
            proc.kill()
        log.info("git pull before restart: exit %s", proc.returncode)
    uv = shutil.which("uv")
    if uv and (ROOT / "uv.lock").exists():
        # Pick up new dependencies; --inexact never removes packages (e.g. dev extras).
        proc = await asyncio.create_subprocess_exec(uv, "sync", "--quiet", "--inexact", cwd=str(ROOT))
        try:
            await asyncio.wait_for(proc.wait(), 300)
        except asyncio.TimeoutError:
            proc.kill()
    args = [a for a in S.argv if a != "--no-browser"] + ["--no-browser"]
    log.info("restarting: %s -m app %s", sys.executable, " ".join(args))
    logging.shutdown()
    os.chdir(ROOT)
    os.execv(sys.executable, [sys.executable, "-m", "app", *args])


@app.post("/api/restart")
async def api_restart(request: Request):
    global _restarting
    require_operator(request)
    if _restarting:
        return {"ok": True, "message": "already restarting"}
    _restarting = True
    asyncio.create_task(_restart_in_place())
    update = " Getting the latest code first." if S.settings.server.update_from_git else ""
    return {"ok": True, "message": f"Restarting — back in about 15 seconds.{update}"}


@app.get("/api/report")
async def api_report(request: Request):
    require_operator(request)
    from .metrics import summarize

    recs = S.pipeline.metrics.records if S.pipeline.metrics else []
    return JSONResponse({c: summarize(recs, c) for c in S.pipeline.channels})
