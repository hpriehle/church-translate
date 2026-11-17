"""End-to-end smoke test of the built engine (the Windows sidecar exe, or any engine command) on a recorded sermon.

    python scripts/windows_smoke.py --engine dist/hearchurch-engine/hearchurch-engine.exe \
        --audio samples/sermon.mp3 --duration 300 [--wolof 45] [--wolof-voice 60 [--wolof-voice-dir DIR]]
        [--no-sermon] [--out smoke.json]

Like the Mac app runs it, with nothing live:
1. starts this repo's hub (app/hear, hub routes only) on a free port with a throwaway database, Claude replaced by a
   fake that answers every translation at once ("En français, <the English>"), so no Anthropic key and no
   network call; the hub's own proxy, metering and live-meeting checks are the real ones (like tests/test_proxy.py)
2. signs up a test congregation on credits (the free hour) and takes its device token, as device sign-in would
3. starts the engine in app mode (--app) with HEARCHURCH_HOME, HEARCHURCH_HUB_URL and HEARCHURCH_DEVICE_TOKEN in
   its environment (what the Mac app passes, and what the Windows app will), downloads the speech model and the
   French Piper voice through the engine's own /api/packages/download, then stops it
4. runs the engine on the recording (--file, real time, --exit-when-done): faster-whisper on the CPU (int8) →
   translation through the hub proxy (the fake Claude) → the French Piper voice
5. reads the run's per-chunk records (logs/*.jsonl) and prints the lag numbers: speech-to-caption delay,
   speech-to-voice delay, speech recognition real-time factor (seconds of work per second of speech)
6. with --wolof N: downloads Wolof speech recognition (the Omnilingual ONNX model from hearchurch.com, as every
   non-Apple-Silicon computer does) and runs N seconds with Wolof as the speaker language, to prove onnxruntime runs
   it here (the sermon is English, so the Wolof text itself means nothing)
7. with --wolof-voice N: N seconds with English spoken and Wolof as the translation language, voiced by the Wolof
   SpeechT5 voice on onnxruntime (the fake Claude's text, so the words aren't Wolof; only that it speaks, and how
   fast, is checked). The voice is downloaded through the engine, or, with --wolof-voice-dir (a folder from
   scripts/models/export_speecht5_onnx.py, before the archive is on hearchurch.com), put where the download goes.
--no-sermon skips 3-5 (the French part); the speech model is still downloaded.
Exit 1 when any step fails. The temp folder is kept with --keep (logs for the CI artifact).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

CLAUDE_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
FAKE_PREFIX = "En français, "


# ------------------------------------------------------------------ the hub (child process)

def serve_hub(port: int, home: Path) -> None:
    """The real hub (as scripts/e2e_credits.py runs it) with Claude replaced by an instant fake."""
    from types import SimpleNamespace as NS

    from anthropic.types.beta import BetaMessage

    from app.hear import proxy

    class FakeClaude:
        def __init__(self):
            self.beta = NS(messages=NS(create=self.create))

        async def create(self, **kw):
            last = kw["messages"][-1]["content"]
            text = last if isinstance(last, str) else " ".join(b.get("text", "") for b in last if isinstance(b, dict))
            reply = "OK" if "reply simply" in text else FAKE_PREFIX + text.strip()
            return BetaMessage.model_validate({
                "id": "msg_ci", "type": "message", "role": "assistant", "model": kw.get("model", "claude"),
                "content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 10}})

    fake = FakeClaude()
    proxy._anthropic_client = lambda key: fake
    from e2e_credits import serve
    serve(port, home)


# ------------------------------------------------------------------ helpers

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, proc: subprocess.Popen, timeout: float, ok=(200,)) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            return False
        try:
            if httpx.get(url, timeout=2).status_code in ok:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


class Report:
    def __init__(self):
        self.fails = 0

    def step(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
        self.fails += not ok
        return ok


def pct(values: list[float], q: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    return round(vals[min(len(vals) - 1, int(q * len(vals)))], 2) if vals else None


# ------------------------------------------------------------------ the engine

class Engine:
    def __init__(self, cmd: list[str], home: Path, hub_port: int, token: str, log_dir: Path):
        self.cmd, self.home, self.log_dir = cmd, home, log_dir
        self.env = {k: v for k, v in os.environ.items() if k not in CLAUDE_VARS}   # never a Claude key
        self.env.update({"HEARCHURCH_HOME": str(home), "HEARCHURCH_HUB_URL": f"ws://127.0.0.1:{hub_port}/ws/engine",
                         "HEARCHURCH_DEVICE_TOKEN": token, "HEARCHURCH_DEVICE_NAME": "CI runner",
                         "HEARCHURCH_SHARE_LOGS": "0", "PYTHONUNBUFFERED": "1"})
        self.env.pop("HF_HOME", None)

    def meeting(self, speakers: list[str], targets: list[str]) -> None:
        data = self.home / "data"
        data.mkdir(parents=True, exist_ok=True)
        (data / f"overrides.{sys.platform}.json").write_text(json.dumps(
            {"meeting": {"speakers": speakers, "targets": targets, "auto_en": False}}), encoding="utf-8")

    def start(self, name: str, *args: str) -> tuple[subprocess.Popen, int]:
        port = free_port()
        out = open(self.log_dir / f"engine-{name}.log", "w", encoding="utf-8")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        p = subprocess.Popen([*self.cmd, "--app", "--no-browser", "--port", str(port), *args], env=self.env,
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, creationflags=flags, cwd=self.home)
        return p, port

    def download(self, r: Report, languages: list[str], timeout: float) -> bool:
        p, port = self.start(f"download-{'-'.join(languages)}")
        base = f"http://127.0.0.1:{port}"
        try:
            if not r.step(f"engine starts in app mode ({'/'.join(languages)})", wait_http(f"{base}/api/status", p, 180),
                          f"exit {p.poll()}" if p.poll() is not None else base):
                return False
            t0 = time.monotonic()
            httpx.post(f"{base}/api/packages/download", json={"languages": languages}, timeout=30).raise_for_status()
            pk: list = []
            while time.monotonic() - t0 < timeout:
                pk = httpx.get(f"{base}/api/packages", params={"languages": ",".join(languages)}, timeout=30).json()["packages"]
                if all(x["state"] == "ready" for x in pk) or any(x["state"] == "failed" for x in pk):
                    break
                time.sleep(3)
            ok = all(x["state"] == "ready" for x in pk)
            return r.step(f"engine downloads {', '.join(x['id'] for x in pk)}", ok,
                          f"{time.monotonic() - t0:.0f}s, " + "; ".join(f"{x['id']} {x['state']} {x['size_mb']} MB "
                                                                       f"{x['error']}".strip() for x in pk))
        finally:
            stop(p)

    def run_file(self, name: str, audio: Path, duration: float, timeout: float) -> tuple[int | None, float]:
        p, _ = self.start(name, "--file", str(audio), "--duration", str(duration), "--exit-when-done")
        t0 = time.monotonic()
        try:
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            stop(p)
            return None, time.monotonic() - t0
        return p.returncode, time.monotonic() - t0

    def records(self, since: float) -> list[dict]:
        out = []
        for f in sorted((self.home / "logs").glob("*.jsonl")):
            if f.stat().st_mtime >= since:
                out += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
        return out


def lag_numbers(records: list[dict], lang: str) -> dict:
    heard = [x for x in records if x["status"] == "done"]
    rs = [x["langs"][lang] for x in records if lang in x.get("langs", {})]
    cap = [x["caption_lag"] for x in rs if x.get("t_caption")]
    air = [x["air_lag"] for x in rs if x["status"] == "aired"]
    stt_work = sum(x["stt_ms"] for x in heard) / 1000
    speech = sum(x["audio_dur_s"] for x in heard)
    return {
        "chunks": len(records), "recognised": len(heard),
        "skipped_behind": sum(1 for x in records if x["status"] == "skipped"),
        "captioned": len(cap), "aired": len(air),
        "through_hub": sum(1 for x in rs if x.get("t_caption") and x.get("engine") == "claude"),
        "speech_s": round(speech, 1),
        "stt_rtf": round(stt_work / speech, 3) if speech else None,
        "stt_ms_p50": pct([x["stt_ms"] for x in heard], .5), "stt_ms_p95": pct([x["stt_ms"] for x in heard], .95),
        "caption_lag_p50": pct(cap, .5), "caption_lag_p95": pct(cap, .95), "caption_lag_max": pct(cap, 1.0),
        "voice_lag_p50": pct(air, .5), "voice_lag_p95": pct(air, .95),
        "tts_ms_p50": pct([x["tts_ms"] for x in rs if x["status"] == "aired"], .5),
        "sample": next((x["text"] for x in rs if x.get("text")), ""),
    }


def stt_model_used(log: Path) -> str:
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    lines = [ln for ln in text.splitlines() if "STT ready" in ln or "switching to the smaller" in ln
             or "NVIDIA" in ln or "too slow" in ln]
    return " | ".join(ln.split("  ", 1)[-1].strip() for ln in lines[-4:])


# ------------------------------------------------------------------ main

async def signup(base: str) -> str:
    async with httpx.AsyncClient(base_url=base, timeout=30) as c:
        tag = secrets.token_hex(3)
        res = await c.post("/api/hc/signup", json={
            "church": "CI Test Church", "name": f"CI Windows {tag}", "unit_type": "ward",
            "email": f"ci-{tag}@example.invalid", "meeting_times": [{"day": "sun", "time": "10:00"}],
            "device_name": "CI runner"})
        res.raise_for_status()
        return res.json()["device_token"]


def wolof_voice(r: Report, eng: Engine, a: argparse.Namespace, result: dict) -> int:
    """English spoken, translated into Wolof and voiced by the ONNX Wolof voice (step 7)."""
    eng.meeting(["en"], ["wo"])
    if a.wolof_voice_dir:
        from app import wolof_voice as wv

        entry = wv.manifest()
        if not r.step("app/wolof_voice.json names the ONNX Wolof voice", bool(entry)):
            return 1
        dest = eng.home / "models" / entry["id"]
        shutil.copytree(a.wolof_voice_dir, dest, dirs_exist_ok=True)
        r.step("Wolof voice from the exported folder (not downloaded)", (dest / "config.json").exists(), str(dest))
    if not eng.download(r, ["wo"], a.download_timeout):
        return 1
    since = time.time()
    code, took = eng.run_file("wolof-voice", a.audio.resolve(), a.wolof_voice, a.wolof_voice * 4 + 600)
    w = lag_numbers(eng.records(since), "wo")
    log = eng.log_dir / "engine-wolof-voice.log"
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    w["tts_engine"] = next((ln.split("  ", 1)[-1].strip() for ln in text.splitlines() if "SpeechT5 on onnxruntime" in ln), "")
    result["wolof_voice"] = w
    r.step("Wolof voice (SpeechT5 ONNX) speaks on this machine", code == 0 and w["aired"] >= 3 and bool(w["tts_engine"]),
           f"exit {code}, {w['aired']} of {w['captioned']} lines voiced, {w['tts_ms_p50']} ms per line (p50), "
           f"voice lag p50 {w['voice_lag_p50']}s; {w['tts_engine']}")
    print("WOLOF VOICE  speech-to-voice p50 {voice_lag_p50}s p95 {voice_lag_p95}s · {aired} lines voiced, "
          "{tts_ms_p50} ms per line (p50)".format(**w), flush=True)
    return 1 if r.fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--engine", help="the engine command, e.g. dist/hearchurch-engine/hearchurch-engine.exe "
                                     "(default: this Python with -m app)")
    ap.add_argument("--audio", type=Path, help="the sermon recording")
    ap.add_argument("--duration", type=float, default=300.0, help="seconds of the recording to play (real time)")
    ap.add_argument("--wolof", type=float, default=0.0, help="also N seconds with Wolof as the speaker language")
    ap.add_argument("--wolof-voice", type=float, default=0.0, help="also N seconds translated into Wolof, voiced")
    ap.add_argument("--wolof-voice-dir", type=Path, help="the exported ONNX Wolof voice, instead of downloading it")
    ap.add_argument("--no-sermon", action="store_true", help="skip the French sermon run")
    ap.add_argument("--download-timeout", type=float, default=1200.0)
    ap.add_argument("--out", type=Path, help="write the numbers here (JSON)")
    ap.add_argument("--keep", type=Path, help="keep the engine's folder and logs here")
    ap.add_argument("--serve-hub", nargs=2, metavar=("PORT", "HOME"), help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.serve_hub:
        serve_hub(int(a.serve_hub[0]), Path(a.serve_hub[1]))
        return 0
    if not a.audio or not a.audio.exists():
        print(f"FAIL  no recording at {a.audio}")
        return 1

    cmd = [str(Path(a.engine).resolve())] if a.engine else [sys.executable, "-m", "app"]
    tmp = Path(tempfile.mkdtemp(prefix="hc-smoke-"))
    logs = a.keep or tmp
    logs.mkdir(parents=True, exist_ok=True)
    r = Report()
    hub_port = free_port()
    hub_env = {k: v for k, v in os.environ.items() if k not in CLAUDE_VARS}
    hub_env.update({"HEARCHURCH_HOME": str(tmp / "hub"), "ANTHROPIC_API_KEY": "sk-ant-ci-fake-never-sent",
                    "E2E_OPERATOR_PIN": secrets.token_hex(12)})
    (tmp / "hub").mkdir()
    hub_log = open(logs / "hub.log", "w", encoding="utf-8")
    hub = subprocess.Popen([sys.executable, __file__, "--serve-hub", str(hub_port), str(tmp / "hub")], cwd=ROOT,
                           env=hub_env, stdout=hub_log, stderr=subprocess.STDOUT)
    result: dict = {"engine": " ".join(cmd), "audio": a.audio.name, "duration_s": a.duration,
                    "machine": {"os": sys.platform, "cpus": os.cpu_count()}}
    try:
        base = f"http://127.0.0.1:{hub_port}"
        if not r.step("hub up with a fake Claude", wait_http(f"{base}/api/hc/billing", hub, 60, ok=(401, 403)), base):
            return 1
        token = asyncio.run(signup(base))
        r.step("test congregation signed up (device token)", bool(token))
        eng = Engine(cmd, tmp / "engine", hub_port, token, logs)
        if a.no_sermon:
            return wolof_voice(r, eng, a, result)
        eng.meeting(["en"], ["fr"])
        if not eng.download(r, ["fr"], a.download_timeout):
            return 1

        since = time.time()
        code, took = eng.run_file("sermon", a.audio.resolve(), a.duration, a.duration * 3 + 600)
        r.step("engine plays the sermon and exits by itself", code == 0, f"exit {code} after {took:.0f}s")
        recs = eng.records(since)
        n = lag_numbers(recs, "fr")
        n["stt"] = stt_model_used(logs / "engine-sermon.log")
        result["sermon"] = n
        r.step("speech recognised", n["recognised"] >= 5, f"{n['recognised']} of {n['chunks']} chunks")
        r.step("translated through the hub proxy", n["through_hub"] >= 5 and n["sample"].startswith(FAKE_PREFIX),
               f"{n['through_hub']} captions, e.g. {n['sample'][:70]!r}")
        r.step("voiced by Piper", n["aired"] >= 5, f"{n['aired']} lines aired")
        print("LAG  speech-to-caption p50 {caption_lag_p50}s p95 {caption_lag_p95}s max {caption_lag_max}s · "
              "speech-to-voice p50 {voice_lag_p50}s p95 {voice_lag_p95}s · STT real-time factor {stt_rtf} "
              "(p50 {stt_ms_p50} ms per chunk) · {speech_s}s of speech, {skipped_behind} chunks skipped (behind)"
              .format(**n), flush=True)
        print(f"STT  {n['stt']}", flush=True)

        if a.wolof:
            eng.meeting(["wo"], ["fr"])
            if eng.download(r, ["speech-wo"], a.download_timeout):
                since = time.time()
                code, took = eng.run_file("wolof", a.audio.resolve(), a.wolof, a.wolof * 4 + 600)
                recs = eng.records(since)
                wo = [x for x in recs if x.get("src") == "wo" and x["status"] == "done"]
                w = lag_numbers(recs, "fr")
                w["wolof_chunks"] = len(wo)
                result["wolof"] = w
                r.step("Wolof speaker: Omnilingual ONNX recognises on this machine", code == 0 and len(wo) >= 1,
                       f"exit {code}, {len(wo)} Wolof chunks, RTF {w['stt_rtf']}, e.g. {(wo[0]['en'] if wo else '')[:50]!r}")
        if a.wolof_voice:
            wolof_voice(r, eng, a, result)
    except Exception as e:   # noqa: BLE001
        r.step("unexpected error", False, f"{type(e).__name__}: {e}")
    finally:
        stop(hub)
        hub_log.close()
        result["failures"] = r.fails
        if a.out:
            a.out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
        if a.keep:
            for f in (tmp / "engine" / "logs").glob("*"):
                if f.is_file():
                    shutil.copy2(f, a.keep / f"engine-home-{f.name}")
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{'OK' if not r.fails else 'FAILED'}: {r.fails} failure(s)")
    return 1 if r.fails else 0


if __name__ == "__main__":
    sys.exit(main())
