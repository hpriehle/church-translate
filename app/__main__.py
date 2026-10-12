"""Entry point: `python -m app` (start.sh does this for you)."""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser

import uvicorn

from .config import LOG_DIR, load_settings


def main() -> None:
    p = argparse.ArgumentParser(description="Live English → French translation server")
    p.add_argument("--port", type=int, help="override [server].port")
    p.add_argument("--file", help="test mode: play this recording into the pipeline and start right away")
    p.add_argument("--speed", type=float, default=1.0, help="test mode playback speed (1.0 = real time)")
    p.add_argument("--start", type=float, default=0.0, help="test mode: start N seconds into the file")
    p.add_argument("--duration", type=float, help="test mode: only play N seconds")
    p.add_argument("--mic", action="store_true", help="start capturing from the input device right away")
    p.add_argument("--record", action="store_true", help="save the French stream to logs/ as MP3")
    p.add_argument("--exit-when-done", action="store_true", help="test mode: quit after the file has been fully processed")
    p.add_argument("--translator", choices=["auto", "ollama"], help="this run only: override [translate].mode")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_DIR / "server.log")],
    )
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from . import main as appmod

    settings = load_settings()
    port = args.port or settings.server.port
    if args.file:
        appmod.S.autostart = {"kind": "file", "file": args.file, "speed": args.speed, "start_s": args.start,
                              "max_s": args.duration, "record": args.record}
    elif args.mic:
        appmod.S.autostart = {"kind": "mic", "record": args.record}
    appmod.S.exit_when_done = args.exit_when_done
    appmod.S.port_override = args.port
    appmod.S.mode_override = args.translator
    appmod.S.argv = sys.argv[1:]

    if settings.server.open_browser and not args.no_browser and not args.exit_when_done:
        def _open():
            time.sleep(2.5)
            webbrowser.open(f"http://localhost:{port}/operator")
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(appmod.app, host=settings.server.host, port=port, log_level="warning",
                ws_ping_interval=20, ws_ping_timeout=30, timeout_keep_alive=30)


if __name__ == "__main__":
    main()
