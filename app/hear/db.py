"""SQLite storage for congregations, their devices (Macs) and their live sessions.

Plain stdlib sqlite3: one connection guarded by a lock (every query is tiny). Device tokens and
e-mail link tokens are stored only as SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import unicodedata
import re
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS congregations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    church TEXT NOT NULL,
    name TEXT NOT NULL,
    unit_type TEXT NOT NULL,
    stake TEXT,
    address TEXT NOT NULL DEFAULT '',
    lat REAL,
    lng REAL,
    meeting_times TEXT NOT NULL DEFAULT '[]',
    timezone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL,
    email_verified INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_seen REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    languages TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    trial INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS email_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    claim_hash TEXT NOT NULL UNIQUE,
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    purpose TEXT NOT NULL,
    device_name TEXT NOT NULL DEFAULT 'Mac',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    approved_at REAL,
    claimed_at REAL
);
CREATE INDEX IF NOT EXISTS sessions_open ON sessions(congregation_id, ended_at);
CREATE INDEX IF NOT EXISTS devices_cong ON devices(congregation_id);
"""

CONG_FIELDS = ("id", "slug", "church", "name", "unit_type", "stake", "address", "lat", "lng", "meeting_times",
               "timezone", "email", "email_verified", "created_at", "hidden")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def slugify(text: str) -> str:
    """"Dakar Ward" → "dakar-ward" (ASCII, lowercase, dashes)."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60].strip("-") or "congregation"


def _cong(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = {k: row[k] for k in CONG_FIELDS}
    d["meeting_times"] = json.loads(d["meeting_times"] or "[]")
    d["email_verified"], d["hidden"] = bool(d["email_verified"]), bool(d["hidden"])
    return d


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.executescript(SCHEMA)
        self.close_stale_sessions()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # ------------------------------------------------------------ congregations
    def unique_slug(self, name: str) -> str:
        base, n = slugify(name), 1
        while True:
            slug = base if n == 1 else f"{base}-{n}"
            if not self._q("SELECT 1 FROM congregations WHERE slug = ?", (slug,)):
                return slug
            n += 1

    def create_congregation(self, *, church: str, name: str, unit_type: str, email: str, stake: str | None = None,
                            address: str = "", lat: float | None = None, lng: float | None = None,
                            meeting_times: list | None = None, timezone: str = "") -> dict[str, Any]:
        for _ in range(5):  # two sign-ups with the same name at once: retry with the next free slug
            slug = self.unique_slug(name)
            try:
                cur = self._x(
                    "INSERT INTO congregations (slug, church, name, unit_type, stake, address, lat, lng, meeting_times,"
                    " timezone, email, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (slug, church, name, unit_type, stake or None, address, lat, lng,
                     json.dumps(meeting_times or []), timezone, email.lower(), time.time()))
                return self.congregation(cur.lastrowid)
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("could not pick a free slug")

    def congregation(self, cid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM congregations WHERE id = ?", (cid,))
        return _cong(rows[0]) if rows else None

    def by_slug(self, slug: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM congregations WHERE slug = ?", (slug,))
        return _cong(rows[0]) if rows else None

    def by_email(self, email: str, verified_only: bool = True) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM congregations WHERE email = ?" + (" AND email_verified = 1" if verified_only else "")
                       + " ORDER BY id", (email.lower(),))
        return [_cong(r) for r in rows]

    def list_congregations(self) -> list[dict[str, Any]]:
        return [_cong(r) for r in self._q("SELECT * FROM congregations ORDER BY id")]

    def set_verified(self, cid: int) -> None:
        self._x("UPDATE congregations SET email_verified = 1 WHERE id = ?", (cid,))

    def set_hidden(self, cid: int, hidden: bool) -> bool:
        return self._x("UPDATE congregations SET hidden = ? WHERE id = ?", (int(hidden), cid)).rowcount > 0

    # ------------------------------------------------------------ devices
    def add_device(self, cid: int, name: str) -> str:
        """Create a device and return its token (the only time it exists in clear)."""
        token = new_token("hcd")
        self._x("INSERT INTO devices (congregation_id, name, token_hash, created_at) VALUES (?,?,?,?)",
                (cid, name[:60] or "Mac", hash_token(token), time.time()))
        return token

    def device_by_token(self, token: str) -> dict[str, Any] | None:
        rows = self._q("SELECT id, congregation_id, name FROM devices WHERE token_hash = ?", (hash_token(token),))
        return dict(rows[0]) if rows else None

    def touch_device(self, did: int) -> None:
        self._x("UPDATE devices SET last_seen = ? WHERE id = ?", (time.time(), did))

    def devices(self, cid: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT id, name, created_at, last_seen FROM devices WHERE congregation_id = ?", (cid,))]

    # ------------------------------------------------------------ e-mail links
    def add_email_token(self, cid: int, purpose: str, device_name: str, ttl_s: float) -> tuple[str, str]:
        """A link token (goes in the e-mail) and a claim secret (stays with the app that asked)."""
        token, claim, now = new_token("hcl"), new_token("hcc"), time.time()
        self._x("INSERT INTO email_tokens (token_hash, claim_hash, congregation_id, purpose, device_name, created_at,"
                " expires_at) VALUES (?,?,?,?,?,?,?)",
                (hash_token(token), hash_token(claim), cid, purpose, device_name[:60] or "Mac", now, now + ttl_s))
        return token, claim

    def approve_email_token(self, token: str) -> dict[str, Any] | None:
        """The link was clicked: returns the token row if it is valid (not expired), else None."""
        rows = self._q("SELECT * FROM email_tokens WHERE token_hash = ?", (hash_token(token),))
        if not rows or rows[0]["expires_at"] < time.time():
            return None
        self._x("UPDATE email_tokens SET approved_at = COALESCE(approved_at, ?) WHERE id = ?", (time.time(), rows[0]["id"]))
        return dict(rows[0])

    def claim_email_token(self, claim: str) -> tuple[str, dict[str, Any] | None]:
        """("unknown"|"expired"|"pending"|"used"|"ok", row)"""
        rows = self._q("SELECT * FROM email_tokens WHERE claim_hash = ?", (hash_token(claim),))
        if not rows:
            return "unknown", None
        r = dict(rows[0])
        if r["claimed_at"]:
            return "used", r
        if r["expires_at"] < time.time():
            return "expired", r
        if not r["approved_at"]:
            return "pending", r
        # claimed_at is set atomically so two polls can't both get a device token
        if self._x("UPDATE email_tokens SET claimed_at = ? WHERE id = ? AND claimed_at IS NULL",
                   (time.time(), r["id"])).rowcount == 0:
            return "used", r
        return "ok", r

    # ------------------------------------------------------------ sessions
    def start_session(self, cid: int, languages: list[str], trial: bool = False) -> int:
        return self._x("INSERT INTO sessions (congregation_id, languages, started_at, trial) VALUES (?,?,?,?)",
                       (cid, json.dumps(languages), time.time(), int(trial))).lastrowid

    def end_session(self, sid: int) -> None:
        self._x("UPDATE sessions SET ended_at = COALESCE(ended_at, ?) WHERE id = ?", (time.time(), sid))

    def close_stale_sessions(self) -> None:
        """A server restart ends every session that was still open."""
        self._x("UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL", (time.time(),))

    def sessions(self, cid: int) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM sessions WHERE congregation_id = ? ORDER BY id", (cid,))
        return [{**dict(r), "languages": json.loads(r["languages"]), "trial": bool(r["trial"])} for r in rows]
