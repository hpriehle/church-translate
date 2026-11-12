"""One-time own-key codes, from the command line on the hub's computer.

  uv run python -m app.hear.codes mint "Dakar Ward"   prints a new code (the only time it can be seen)
  uv run python -m app.hear.codes list                every code made: who it's for, when, who used it

A church enters the code during setup in the Mac app; its congregation then uses its own Anthropic key instead of
credits. A code works once. --db picks the database (default: [hearchurch] db_path in config.toml).
The same thing over HTTP, signed in as operator: POST /api/admin/own-key-codes {label}.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .db import Database


def _when(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "-"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.hear.codes", description="One-time own-key codes for churches")
    p.add_argument("--db", type=Path, help="the hub's SQLite database (default: from config.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint", help="make a code for one church")
    m.add_argument("label", help='who it is for, e.g. "Dakar Ward"')
    sub.add_parser("list", help="every code made, and who used it")
    a = p.parse_args(argv)

    path = a.db
    if path is None:
        from ..config import HOME, load_settings
        path = HOME / load_settings().hearchurch.db_path
    db = Database(path, close_stale=False)   # the server may be running a meeting on this database
    if a.cmd == "mint":
        try:
            print(db.mint_own_key_code(a.label))
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        return 0
    for c in db.own_key_codes():
        used = f"used {_when(c['used_at'])} by {c['used_by']}" if c["used_at"] else "unused"
        print(f"{_when(c['created_at'])}  {c['label']}  ({used})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
