"""FastAPI app: listener page + MP3 stream + caption WebSocket, operator page + control API."""
from __future__ import annotations

import asyncio
import logging
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
from .audio_in import list_input_devices
from .broadcast.hub import Hub
from .broadcast.mixer import Mixer
from .broadcast.stream import Mp3Broadcaster
from .config import (ROOT, SAMPLES_DIR, WEB_DIR, Settings, load_glossary, load_settings, save_glossary,
                     save_override, validate_glossary)
from .pipeline import Pipeline
from .stt import Transcriber
from .translate.router import Translator
from .tts.engines import TtsRunner, french_voices

log = logging.getLogger("app")

AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov"}
EDITABLE = {
    "audio": {"input_device", "input_channel", "input_gain"},
    "translate": {"mode", "claude_model", "claude_effort", "claude_timeout_s", "ollama_model", "claude_session_turns"},
    "tts": {"engine", "voice", "rate", "volume", "piper_model"},
    "backlog": {"speedup_start_s", "speedup_full_s", "max_speed", "condense_on_s", "condense_off_s", "drop_above_s"},
    "server": {"lan_ip"},
}


class State:
    settings: Settings
    glossary: dict[str, Any]
    pipeline: Pipeline
    broadcaster: Mp3Broadcaster
    mixer: Mixer
    listen: Hub
    operator: Hub
    translator: Translator
    started: float
    local_addrs: set[str]
    autostart: dict | None = None
    exit_when_done: bool = False
    port_override: int | None = None


S = State()


def listener_url() -> str:
    ip = netinfo.best_lan_ip(S.settings.server.lan_ip)
    port = S.settings.server.port
    return f"http://{ip}/" if port == 80 else f"http://{ip}:{port}/"


def full_status() -> dict[str, Any]:
    return {
        "type": "status",
        "time": time.time(),
        "pipeline": S.pipeline.status(),
        "translate": S.translator.status(),
        "listeners": {"stream": S.broadcaster.listeners, "captions": S.listen.count,
                      "kicked": S.broadcaster.kicked_total, "dropped_ws": S.listen.dropped_clients},
        "mixer_underruns": S.mixer.underruns,
        "listener_url": listener_url(),
    }


async def status_loop() -> None:
    while True:
        await asyncio.sleep(1.0)
        if S.operator.count:
            S.operator.publish(full_status())


def _caffeinate() -> subprocess.Popen | None:
    """Keep the Mac awake while the server runs (closing the lid still sleeps it)."""
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        import os

        return subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    S.settings = load_settings()
    if S.port_override:
        S.settings.server.port = S.port_override
    S.glossary = load_glossary()
    S.started = time.time()
    S.local_addrs = netinfo.local_addresses()
    st = S.settings
    S.listen = Hub("listen", history=300, queue_size=400)
    S.operator = Hub("operator", queue_size=2000)
    S.broadcaster = Mp3Broadcaster(st.stream.sample_rate, st.stream.bitrate_kbps, st.stream.burst_s)
    S.mixer = Mixer(st.stream.sample_rate, on_frame=S.broadcaster.feed)
    S.mixer.volume = st.tts.volume
    S.translator = Translator(st.translate, S.glossary, ROOT / "data" / "claude-workdir")
    tts = TtsRunner(st.tts, st.stream.sample_rate)
    stt = Transcriber(st.stt)
    S.pipeline = Pipeline(st, S.glossary, S.listen, S.operator, S.mixer, S.broadcaster, S.translator, stt, tts)
    S.pipeline.start_tasks()
    S.mixer.start()
    caff = _caffeinate()
    bg = [asyncio.create_task(status_loop()), asyncio.create_task(S.translator.start()),
          asyncio.create_task(S.pipeline.load_stt())]

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
                ts = S.translator.status()
                claude_ok = ts["claude"]["state"] in ("ready", "disabled", "missing")
                ollama_ok = S.translator.ollama.warm or not S.translator.ollama.available
                if S.pipeline.stt.ready and claude_ok and ollama_ok:
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
    await S.translator.close()
    if caff:
        caff.terminate()


def _shutdown() -> None:
    import os
    import signal

    os.kill(os.getpid(), signal.SIGINT)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# ------------------------------------------------------------------ access control

def _is_operator(host: str | None, pin: str | None, cookie: str | None) -> bool:
    if host and host.split("%")[0] in S.local_addrs:
        return True
    want = S.settings.server.operator_pin
    return bool(want) and (pin == want or cookie == want)


def require_operator(request: Request) -> None:
    if not _is_operator(request.client.host if request.client else None,
                        request.query_params.get("pin"), request.cookies.get("op_pin")):
        raise HTTPException(403, "Operator page is only available on the Mac running the server (or with the PIN).")


# ------------------------------------------------------------------ listener side

@app.get("/", response_class=HTMLResponse)
async def listener_page():
    return FileResponse(WEB_DIR / "listener" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/stream.mp3")
async def stream(request: Request):
    sub, burst = S.broadcaster.subscribe()

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
            S.broadcaster.unsubscribe(sub)

    headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache",
               "X-Content-Type-Options": "nosniff", "Access-Control-Allow-Origin": "*",
               "icy-name": "Traduction en direct"}
    return StreamingResponse(body(), media_type="audio/mpeg", headers=headers)


@app.get("/clips/{seq}.mp3")
async def clip(seq: int):
    data = S.pipeline.clips.get(seq)
    if data is None:
        raise HTTPException(404)
    return Response(data, media_type="audio/mpeg", headers={"Cache-Control": "max-age=600"})


@app.websocket("/ws/listen")
async def ws_listen(ws: WebSocket):
    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        hello = {}
    # Caption numbers restart when the server restarts; a phone from a previous boot starts over.
    last = int(hello.get("last_seq") or 0) if hello.get("boot") == S.started else 0
    init = {"type": "init", "boot": S.started, "live": S.pipeline.running, "server_time": time.time(),
            "history": S.listen.replay_after(last)}
    await S.listen.serve(ws, [init])


@app.get("/qr.svg")
async def qr(dark: str = "#000", light: str = "#fff"):
    return Response(netinfo.qr_svg(listener_url(), dark=dark, light=light), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/qr")
async def api_qr(request: Request, text: str):
    require_operator(request)
    return Response(netinfo.qr_svg(text), media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    return {"ok": True, "listeners": S.broadcaster.listeners, "running": S.pipeline.running}


# ------------------------------------------------------------------ operator side

@app.get("/operator", response_class=HTMLResponse)
async def operator_page(request: Request):
    require_operator(request)
    resp = FileResponse(WEB_DIR / "operator" / "index.html", headers={"Cache-Control": "no-cache"})
    pin = request.query_params.get("pin")
    if pin and pin == S.settings.server.operator_pin:
        resp.set_cookie("op_pin", pin, httponly=True, samesite="strict")
    return resp


@app.get("/print", response_class=HTMLResponse)
async def print_page(request: Request):
    require_operator(request)
    return FileResponse(WEB_DIR / "print.html", headers={"Cache-Control": "no-cache"})


@app.websocket("/ws/operator")
async def ws_operator(ws: WebSocket):
    if not _is_operator(ws.client.host if ws.client else None, ws.query_params.get("pin"), ws.cookies.get("op_pin")):
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
    require_operator(request)
    st = S.settings
    return {
        "settings": st.model_dump(),
        "devices": list_input_devices(),
        "voices": french_voices(),
        "samples": _samples(),
        "glossary": S.glossary,
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
                             start_s=float(body.get("start_s", 0)), record=bool(body.get("record", False)))
        else:
            S.pipeline.start("mic", device=body.get("device"), record=bool(body.get("record", False)))
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/stop")
async def api_stop(request: Request):
    require_operator(request)
    S.pipeline.stop()
    return {"ok": True}


@app.post("/api/settings")
async def api_settings(request: Request):
    require_operator(request)
    body: dict[str, dict] = await request.json()
    data = S.settings.model_dump()
    for section, values in body.items():
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
    S.pipeline.apply_settings(new, S.glossary)
    return {"ok": True, "settings": new.model_dump()}


@app.get("/api/glossary")
async def api_glossary_get(request: Request):
    require_operator(request)
    return S.glossary


@app.post("/api/glossary")
async def api_glossary_set(request: Request):
    require_operator(request)
    try:
        g = validate_glossary(await request.json())
        save_glossary(g)
    except Exception as e:
        raise HTTPException(400, f"Invalid glossary: {e}")
    S.glossary = g
    S.pipeline.apply_settings(S.settings, g)
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
    require_operator(request)
    body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
    text = body.get("text") or "Bonjour. Ceci est un essai de la traduction en direct. Si vous m'entendez, tout fonctionne."
    try:
        await S.pipeline.say_test(text)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/report")
async def api_report(request: Request):
    require_operator(request)
    from .metrics import summarize

    recs = S.pipeline.metrics.records if S.pipeline.metrics else []
    return JSONResponse(summarize(recs))
