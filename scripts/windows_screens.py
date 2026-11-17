"""Screenshots of the Windows app (W2) on the CI runner: the real app, sidecar and a local hub.

1. starts this repo's hub with a fake Claude (as scripts/windows_smoke.py does: no Anthropic key, no network call) and
   signs up a test congregation; asks it for a pairing code ("Add device"), which the app then signs in with;
2. downloads speech recognition and the English and French voices into a fresh data folder through the sidecar's own
   /api/packages/download, and puts the sermon recording in its samples\\ folder;
3. runs HearChurch.exe with HEARCHURCH_APP_SCREENSHOTS: the app signs in, finishes setup, waits for the engine,
   plays the sermon as a meeting, opens every Settings tab and quits, saving a PNG of each screen (UI/Screenshots.cs);
4. checks the pictures are there and prints what the app logged (screens.json).

    python scripts/windows_screens.py --app out/HearChurch/HearChurch.exe --audio samples/sermon.mp3 \
        --out screens --keep screens-logs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import windows_smoke as smoke  # noqa: E402

SCREENS = ["1-sign-in.png", "3-main-idle.png", "4-main-meeting.png", "5-settings-1-general.png", "5-settings-2-billing.png",
           "5-settings-3-languages.png", "5-settings-3b-languages-french.png", "5-settings-4-speaking.png",
           "5-settings-5-glossary.png", "5-settings-6-congregation.png", "5-settings-7-about.png"]


def serve_hub(port: int, home: Path) -> None:
    """The smoke test's hub, plus a plain /health: run with lifespan off it has no pipeline, so the real one fails,
    and the app's "Can reach hearchurch.com" check would show red in the screenshots."""
    from fastapi.routing import APIRoute

    from app import main as hub_main

    async def health():
        return {"ok": True}

    hub_main.app.router.routes.insert(0, APIRoute("/health", health, methods=["GET"]))
    smoke.serve_hub(port, home)


def pairing_code(base: str, token: str) -> str:
    r = httpx.post(f"{base}/api/hc/pairing-code", headers={"Authorization": f"Bearer {token}"}, json={}, timeout=30)
    r.raise_for_status()
    return r.json()["code"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--app", required=True, type=Path, help="HearChurch.exe (with engine\\hearchurch-engine.exe beside it)")
    ap.add_argument("--audio", required=True, type=Path, help="the sermon recording")
    ap.add_argument("--out", required=True, type=Path, help="where the screenshots go")
    ap.add_argument("--keep", type=Path, help="copy the logs here")
    ap.add_argument("--timeout", type=float, default=1500)
    ap.add_argument("--serve-hub", nargs=2, metavar=("PORT", "HOME"), help=argparse.SUPPRESS)
    if "--serve-hub" in sys.argv:
        i = sys.argv.index("--serve-hub")
        serve_hub(int(sys.argv[i + 1]), Path(sys.argv[i + 2]))
        return 0
    a = ap.parse_args()

    app = a.app.resolve()
    engine = app.parent / "engine" / "hearchurch-engine.exe"
    out = a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    logs = (a.keep or out).resolve()
    logs.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="hc-screens-"))
    home = tmp / "app-home"
    r = smoke.Report()
    hub_port = smoke.free_port()
    hub_env = {k: v for k, v in os.environ.items() if k not in smoke.CLAUDE_VARS}
    hub_env.update({"HEARCHURCH_HOME": str(tmp / "hub"), "ANTHROPIC_API_KEY": "sk-ant-ci-fake-never-sent",
                    "E2E_OPERATOR_PIN": secrets.token_hex(12)})
    (tmp / "hub").mkdir()
    hub_log = open(logs / "hub.log", "w", encoding="utf-8")
    hub = subprocess.Popen([sys.executable, __file__, "--serve-hub", str(hub_port), str(tmp / "hub")],
                           cwd=smoke.ROOT, env=hub_env, stdout=hub_log, stderr=subprocess.STDOUT)
    try:
        base = f"http://127.0.0.1:{hub_port}"
        if not r.step("hub up with a fake Claude", smoke.wait_http(f"{base}/api/hc/billing", hub, 60, ok=(401, 403)), base):
            return 1
        token = asyncio.run(smoke.signup(base))
        r.step("test congregation signed up", bool(token))

        eng = smoke.Engine([str(engine)], home, hub_port, token, logs)
        eng.meeting(["en"], ["fr"])
        if not eng.download(r, ["en", "fr"], 1200):
            return 1
        (home / "samples").mkdir(parents=True, exist_ok=True)
        shutil.copy2(a.audio, home / "samples" / "sermon.mp3")
        for f in (home / "app-settings.json", home / "profile.json"):
            f.unlink(missing_ok=True)
        code = pairing_code(base, token)
        r.step("pairing code for the app (Add device)", len(code) >= 8, code[:2] + "…")

        env = {k: v for k, v in os.environ.items() if k not in smoke.CLAUDE_VARS and not k.startswith("HEARCHURCH_")}
        env.update({"HEARCHURCH_HOME": str(home), "HEARCHURCH_APP_HUB": base, "HEARCHURCH_APP_SCREENSHOTS": str(out),
                    "HEARCHURCH_APP_DEMO_CODE": code, "HEARCHURCH_APP_DEMO_FILE": "sermon.mp3"})
        t0 = time.monotonic()
        p = subprocess.Popen([str(app)], env=env, cwd=app.parent)
        try:
            rc = p.wait(a.timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = None
        r.step("the app walks through its screens and quits by itself", rc == 0, f"exit {rc} after {time.monotonic() - t0:.0f}s")
        steps = out / "screens.json"
        if steps.exists():
            for s in json.loads(steps.read_text(encoding="utf-8")):
                print(f"  {s['t']:>6}s  {'ok ' if s['ok'] else 'NO '} {s['step']}  {s.get('detail', '')}"[:400], flush=True)
        for name in SCREENS:
            f = out / name
            r.step(f"screenshot {name}", f.exists() and f.stat().st_size > 10_000, f"{f.stat().st_size} bytes" if f.exists() else "missing")
    except Exception as e:   # noqa: BLE001
        r.step("unexpected error", False, f"{type(e).__name__}: {e}")
    finally:
        smoke.stop(hub)
        hub_log.close()
        for f in [*(home / "logs").glob("*")] if (home / "logs").exists() else []:
            if f.is_file():
                shutil.copy2(f, logs / f"app-home-{f.name}")
        # The app's own engine may still be shutting down: never fail on cleanup.
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{'OK' if not r.fails else 'FAILED'}: {r.fails} failure(s)")
    return 1 if r.fails else 0


if __name__ == "__main__":
    sys.exit(main())
