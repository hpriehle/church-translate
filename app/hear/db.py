"""SQLite storage for congregations, their devices (Macs) and their live sessions.

Plain stdlib sqlite3: one connection guarded by a lock (every query is tiny). Device tokens, e-mail link
tokens and one-time own-key codes are stored only as SHA-256 hashes.

Billing: a congregation pays with prepaid credits ("credits"), or with its own Anthropic key ("own_key"). Own key
is only reached with a one-time code Harrison makes for that church (`python -m app.hear.codes mint "Dakar Ward"`).
Congregations that existed before billing are grandfathered as "own_key": they were set up with their own keys.

Metering and the ledger: the Mac reports seconds of speech per heard language (`record_speech`, idempotent on the
session and the Mac's chunk number). A congregation's balance is its top-ups (`add_credit`, idempotent on the Stripe
payment id; a Stripe refund takes its amount back off that top-up, `refund_credit`) minus the language-minutes
of its credit-billed sessions, after the free first hour. Each session keeps
the billing mode it started under, so own-key meetings are metered but never debited, also after a switch to
credits. The balance may go negative (a meeting is never cut off); `can_start` then refuses the next one.
"""
from __future__ import annotations

import hashlib
import hmac
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
    hidden INTEGER NOT NULL DEFAULT 0,
    city TEXT NOT NULL DEFAULT '',
    connect_code_hash TEXT,
    connect_code_at REAL,
    billing TEXT NOT NULL DEFAULT 'credits'
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_seen REAL,
    app_version TEXT,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    languages TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    trial INTEGER NOT NULL DEFAULT 0,
    code TEXT,
    billing TEXT NOT NULL DEFAULT 'credits',
    device_id INTEGER
);
CREATE TABLE IF NOT EXISTS speech (
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,
    lang TEXT NOT NULL,
    seconds REAL NOT NULL,
    PRIMARY KEY (session_id, seq, lang)
);
CREATE TABLE IF NOT EXISTS credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    payment_id TEXT NOT NULL UNIQUE,
    cents INTEGER NOT NULL,
    seconds REAL NOT NULL,
    created_at REAL NOT NULL,
    refunded_cents INTEGER NOT NULL DEFAULT 0,
    refunded_at REAL
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
CREATE TABLE IF NOT EXISTS own_key_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    created_at REAL NOT NULL,
    used_at REAL,
    congregation_id INTEGER REFERENCES congregations(id),
    voided_at REAL
);
CREATE TABLE IF NOT EXISTS refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credit_id INTEGER NOT NULL REFERENCES credits(id),
    cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    created_at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'stripe',
    stripe_refund_id TEXT
);
CREATE TABLE IF NOT EXISTS congregation_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    email TEXT NOT NULL,
    is_main INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE (congregation_id, email)
);
CREATE TABLE IF NOT EXISTS device_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL UNIQUE,
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    device_id INTEGER
);
CREATE INDEX IF NOT EXISTS sessions_open ON sessions(congregation_id, ended_at);
CREATE INDEX IF NOT EXISTS emails_address ON congregation_emails(email);
CREATE INDEX IF NOT EXISTS device_codes_cong ON device_codes(congregation_id, kind);
CREATE INDEX IF NOT EXISTS devices_cong ON devices(congregation_id);
CREATE INDEX IF NOT EXISTS credits_cong ON credits(congregation_id);
CREATE INDEX IF NOT EXISTS refunds_credit ON refunds(credit_id);
"""

# Prices (decided Nov 8): $5 per language-hour of speech, the first language-hour free per congregation.
CENTS_PER_LANGUAGE_HOUR = 500
FREE_SECONDS = 3600
MAX_CHUNK_S = 60.0     # one reported chunk of speech is never longer than this (the chunker cuts far sooner)
MAX_LANGS = 20

CONG_FIELDS = ("id", "slug", "church", "name", "unit_type", "stake", "address", "city", "lat", "lng", "meeting_times",
               "timezone", "email", "email_verified", "created_at", "hidden", "billing")

# hearchurch.com paths served by the download Worker and the marketing site, not by this app
# (docs/seo-site-structure.md). Add a new top-level page here before it goes live, after checking that no
# congregation already has that slug.
SITE_PAGES = frozenset({
    "app", "appcast", "blog", "compare", "credits", "demo", "download", "faq", "features", "how-it-works", "languages", "lds",
    "multilingual", "pricing", "rr", "rrweb", "setup", "sitemap", "small-churches"})

# Listener pages live at hearchurch.com/<slug>, so a slug may never be one of the site's own top-level paths
# (tests/test_hear.py checks this list against the routes).
RESERVED_SLUGS = frozenset({
    "about", "admin", "api", "c", "clips", "favicon-ico", "find", "guide", "health", "help", "listen", "listener",
    "login", "logout", "operator", "print", "privacy", "qr", "robots", "s", "signs", "signup", "speak", "static",
    "stream", "support", "terms", "voices", "ws", "www"}) | SITE_PAGES


def city_of(address: str) -> str:
    """The town for search results, from the address the congregation gave:
    "123 W 125th St, New York, NY 10027" → "New York, NY"; "Rue 10, Dakar, Senegal" → "Dakar, Senegal"."""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if parts and parts[-1].lower() in ("usa", "us", "u.s.a.", "united states", "united states of america"):
        parts.pop()
    if len(parts) < 2:
        return ""
    region = re.sub(r"\s*\b\d[\d -]*$", "", parts[-1]).strip()       # drop a ZIP / postcode
    region = re.sub(r"^\d[\d -]*\s+", "", region).strip()             # "75001 Paris" → "Paris"
    if len(parts) == 2 and parts[0][:1].isdigit():   # "12 Main St, Springfield": the second part is the town
        return region
    city = re.sub(r"^\d[\d -]*\s+", "", parts[-2]).strip()   # "New York, NY" or "…, New York, NY 10027"
    return f"{city}, {region}" if region and region != city else city


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


# One-time own-key codes: "HC-7KQM-X2PA-9TDW". No 0/O, 1/I/L or U, so a code read aloud or retyped survives.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CODE_LEN = 12   # 30^12, about 59 bits: unguessable behind the hub's failure limits


MAX_EMAILS = 10              # addresses per congregation
DEVICE_CODE_LEN = 8          # add-a-Mac codes "7KQM-X2PA": 30^8, ~39 bits, short-lived and behind failure limits
DEVICE_CODE_TTL_S = 15 * 60


def new_own_key_code() -> str:
    c = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    return f"HC-{c[:4]}-{c[4:8]}-{c[8:]}"


def normalize_own_key_code(code: str) -> str | None:
    """As typed ("hc-7kqm x2pa 9tdw", "7KQMX2PA9TDW") → "7KQMX2PA9TDW", or None if it can't be a code."""
    c = re.sub(r"[\s-]", "", code or "").upper()
    if c.startswith("HC") and len(c) == CODE_LEN + 2:
        c = c[2:]
    return c if len(c) == CODE_LEN and all(ch in CODE_ALPHABET for ch in c) else None


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
    def __init__(self, path: Path | str, close_stale: bool = True):
        """close_stale: end sessions left open (a server restart). Tools that open the live database beside the
        running server (app.hear.codes) pass False, so a meeting in progress isn't marked ended."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            had_emails = bool(self._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                                               " AND name = 'congregation_emails'").fetchone())
            self._db.executescript(SCHEMA)
            if not had_emails:   # databases made before several e-mails: the sign-up address becomes the main one
                self._db.execute("INSERT OR IGNORE INTO congregation_emails (congregation_id, email, is_main,"
                                 " created_at) SELECT id, lower(trim(email)), 1, created_at FROM congregations"
                                 " WHERE trim(email) != ''")
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(congregations)")}
            if "city" not in cols:   # databases made before search showed the town
                self._db.execute("ALTER TABLE congregations ADD COLUMN city TEXT NOT NULL DEFAULT ''")
                for cid, address in self._db.execute("SELECT id, address FROM congregations").fetchall():
                    self._db.execute("UPDATE congregations SET city = ? WHERE id = ?", (city_of(address or ""), cid))
            if "connect_code_hash" not in cols:   # databases made before connect codes: no code yet
                self._db.execute("ALTER TABLE congregations ADD COLUMN connect_code_hash TEXT")
            if "connect_code_at" not in cols:
                self._db.execute("ALTER TABLE congregations ADD COLUMN connect_code_at REAL")
            if "billing" not in cols:   # databases made before credits: every congregation there brought its own key
                self._db.execute("ALTER TABLE congregations ADD COLUMN billing TEXT NOT NULL DEFAULT 'credits'")
                self._db.execute("UPDATE congregations SET billing = 'own_key'")
            scols = {r[1] for r in self._db.execute("PRAGMA table_info(sessions)")}
            if "code" not in scols:
                self._db.execute("ALTER TABLE sessions ADD COLUMN code TEXT")
            if "billing" not in scols:   # sessions from before credits were all paid with the church's own key
                self._db.execute("ALTER TABLE sessions ADD COLUMN billing TEXT NOT NULL DEFAULT 'credits'")
                self._db.execute("UPDATE sessions SET billing = 'own_key'")
            if "device_id" not in scols:   # the Mac that started it: only that one may carry it on (recent_session)
                self._db.execute("ALTER TABLE sessions ADD COLUMN device_id INTEGER")
            ccols = {r[1] for r in self._db.execute("PRAGMA table_info(credits)")}
            if "refunded_cents" not in ccols:   # databases made before refunds were taken off the ledger
                self._db.execute("ALTER TABLE credits ADD COLUMN refunded_cents INTEGER NOT NULL DEFAULT 0")
                self._db.execute("ALTER TABLE credits ADD COLUMN refunded_at REAL")
            dcols = {r[1] for r in self._db.execute("PRAGMA table_info(devices)")}
            if "app_version" not in dcols:   # databases made before the app said its version
                self._db.execute("ALTER TABLE devices ADD COLUMN app_version TEXT")
            if "revoked_at" not in dcols:    # databases made before a device could be revoked
                self._db.execute("ALTER TABLE devices ADD COLUMN revoked_at REAL")
            kcols = {r[1] for r in self._db.execute("PRAGMA table_info(own_key_codes)")}
            if "voided_at" not in kcols:   # databases made before codes could be voided
                self._db.execute("ALTER TABLE own_key_codes ADD COLUMN voided_at REAL")
            self._db.execute("CREATE INDEX IF NOT EXISTS sessions_code ON sessions(congregation_id, code)")
        if close_stale:
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
            if slug not in RESERVED_SLUGS and not self._q("SELECT 1 FROM congregations WHERE slug = ?", (slug,)):
                return slug
            n += 1

    def create_congregation(self, *, church: str, name: str, unit_type: str, email: str, stake: str | None = None,
                            address: str = "", lat: float | None = None, lng: float | None = None,
                            meeting_times: list | None = None, timezone: str = "", city: str = "") -> dict[str, Any]:
        for _ in range(5):  # two sign-ups with the same name at once: retry with the next free slug
            slug = self.unique_slug(name)
            try:
                cur = self._x(
                    "INSERT INTO congregations (slug, church, name, unit_type, stake, address, city, lat, lng,"
                    " meeting_times, timezone, email, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (slug, church, name, unit_type, stake or None, address, city or city_of(address), lat, lng,
                     json.dumps(meeting_times or []), timezone, email.lower(), time.time()))
                if email.strip():
                    self.add_email(cur.lastrowid, email, main=True)
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

    def set_location(self, cid: int, lat: float, lng: float, address: str | None = None, city: str = "") -> None:
        """city: the town at the pin (reverse geocoded); otherwise taken from the address, if it says."""
        town = city or (city_of(address) if address is not None else "") or None
        self._x("UPDATE congregations SET lat = ?, lng = ?, address = COALESCE(?, address),"
                " city = COALESCE(?, city) WHERE id = ?", (lat, lng, address, town, cid))

    def missing_towns(self) -> list[dict[str, Any]]:
        return [_cong(r) for r in self._q("SELECT * FROM congregations WHERE city = '' AND lat IS NOT NULL")]

    def set_town(self, cid: int, town: str) -> None:
        self._x("UPDATE congregations SET city = ? WHERE id = ?", (town, cid))

    def in_box(self, south: float, north: float, west: float, east: float) -> list[dict[str, Any]]:
        """Visible (verified, not hidden) congregations with a location inside a box. west > east = across ±180°."""
        lng_sql = "lng BETWEEN ? AND ?" if west <= east else "(lng >= ? OR lng <= ?)"
        rows = self._q("SELECT * FROM congregations WHERE email_verified = 1 AND hidden = 0 AND lat BETWEEN ? AND ?"
                       f" AND {lng_sql}", (south, north, west, east))
        return [_cong(r) for r in rows]

    def search(self, q: str, limit: int = 10) -> list[dict[str, Any]]:
        """Visible congregations whose name, stake, church or town contains q (case-insensitive)."""
        like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self._q("SELECT * FROM congregations WHERE email_verified = 1 AND hidden = 0 AND (lower(name) LIKE :like"
                       " ESCAPE '\\' OR lower(stake) LIKE :like ESCAPE '\\' OR lower(church) LIKE :like ESCAPE '\\'"
                       " OR lower(city) LIKE :like ESCAPE '\\')"
                       " ORDER BY (lower(name) LIKE :pfx ESCAPE '\\') DESC, name LIMIT :lim",
                       {"like": like, "pfx": like[1:], "lim": limit})
        return [_cong(r) for r in rows]

    def set_hidden(self, cid: int, hidden: bool) -> bool:
        return self._x("UPDATE congregations SET hidden = ? WHERE id = ?", (int(hidden), cid)).rowcount > 0

    # ------------------------------------------------------------ connect codes
    def new_connect_code(self, cid: int) -> str:
        """Make a fresh 6-digit code (the old one stops working); only its hash is stored."""
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        self._x("UPDATE congregations SET connect_code_hash = ?, connect_code_at = ? WHERE id = ?",
                (hash_token(code), time.time(), cid))
        return code

    def connect_code_ok(self, cid: int, code: str) -> bool:
        """code: as typed ("123 456", "123-456"). False when wrong or when no code was ever made."""
        rows = self._q("SELECT connect_code_hash FROM congregations WHERE id = ?", (cid,))
        stored = rows[0]["connect_code_hash"] if rows else None
        digits = re.sub(r"[\s-]", "", code or "")
        if not stored or not re.fullmatch(r"\d{6}", digits):
            return False
        return hmac.compare_digest(stored, hash_token(digits))

    # ------------------------------------------------------------ e-mails (several per congregation, one main)
    def emails(self, cid: int) -> list[dict[str, Any]]:
        """The congregation's addresses, the main (billing) one first."""
        return [{"email": r["email"], "main": bool(r["is_main"]), "added_at": r["created_at"]}
                for r in self._q("SELECT email, is_main, created_at FROM congregation_emails WHERE congregation_id = ?"
                                 " ORDER BY is_main DESC, id", (cid,))]

    def main_email(self, cid: int) -> str | None:
        rows = self._q("SELECT email FROM congregation_emails WHERE congregation_id = ? AND is_main = 1", (cid,))
        return rows[0]["email"] if rows else None

    def add_email(self, cid: int, email: str, main: bool = False) -> bool:
        """Add an address (lowercased). The first address, or main=True, becomes the main one; the main address is
        also kept in congregations.email (what the older routes read). Returns True if it was new. ValueError past
        MAX_EMAILS."""
        email = email.strip().lower()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                rows = self._db.execute("SELECT email FROM congregation_emails WHERE congregation_id = ?",
                                        (cid,)).fetchall()
                known = {r["email"] for r in rows}
                new = email not in known
                if new and len(known) >= MAX_EMAILS:
                    raise ValueError(f"at most {MAX_EMAILS} e-mail addresses per congregation")
                if new:
                    self._db.execute("INSERT INTO congregation_emails (congregation_id, email, is_main, created_at)"
                                     " VALUES (?,?,0,?)", (cid, email, time.time()))
                if main or not known:
                    self._set_main(cid, email)
                self._db.execute("COMMIT")
                return new
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _set_main(self, cid: int, email: str) -> None:   # inside a transaction
        self._db.execute("UPDATE congregation_emails SET is_main = (email = ?) WHERE congregation_id = ?", (email, cid))
        self._db.execute("UPDATE congregations SET email = ? WHERE id = ?", (email, cid))

    def set_main_email(self, cid: int, email: str) -> bool:
        """Make one of the congregation's addresses the main one. False when it isn't one of them."""
        email = email.strip().lower()
        with self._lock:
            if not self._db.execute("SELECT 1 FROM congregation_emails WHERE congregation_id = ? AND email = ?",
                                    (cid, email)).fetchone():
                return False
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._set_main(cid, email)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            return True

    def remove_email(self, cid: int, email: str) -> str:
        """"removed", "unknown", or "main" (the main address can't be removed: make another one main first)."""
        email = email.strip().lower()
        rows = self._q("SELECT is_main FROM congregation_emails WHERE congregation_id = ? AND email = ?", (cid, email))
        if not rows:
            return "unknown"
        if rows[0]["is_main"]:
            return "main"
        self._x("DELETE FROM congregation_emails WHERE congregation_id = ? AND email = ? AND is_main = 0", (cid, email))
        return "removed"

    def congregations_with_email(self, email: str) -> list[dict[str, Any]]:
        """Visible (not hidden, verified) congregations that list this address, oldest first."""
        rows = self._q("SELECT c.* FROM congregations c JOIN congregation_emails e ON e.congregation_id = c.id"
                       " WHERE e.email = ? AND c.hidden = 0 AND c.email_verified = 1 ORDER BY c.id",
                       (email.strip().lower(),))
        return [_cong(r) for r in rows]

    # ------------------------------------------------------------ device codes (add a Mac: pairing or e-mailed)
    def new_device_code(self, cid: int, kind: str, ttl_s: float = DEVICE_CODE_TTL_S) -> tuple[str, float]:
        """A one-time code that connects a new Mac to congregation `cid`: kind "pair" (shown by a Mac that is
        already in, "Add device") or "email" (sent to the congregation's addresses). Earlier unused codes of the
        same kind stop working. Returns (code as shown "ABCD-EFGH", expires_at); only its hash is kept."""
        if kind not in ("pair", "email"):
            raise ValueError(kind)
        c = "".join(secrets.choice(CODE_ALPHABET) for _ in range(DEVICE_CODE_LEN))
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("UPDATE device_codes SET expires_at = ? WHERE congregation_id = ? AND kind = ?"
                                 " AND used_at IS NULL AND expires_at > ?", (now, cid, kind, now))
                self._db.execute("INSERT INTO device_codes (code_hash, congregation_id, kind, created_at, expires_at)"
                                 " VALUES (?,?,?,?,?)", (hash_token(c), cid, kind, now, now + ttl_s))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return f"{c[:4]}-{c[4:]}", now + ttl_s

    def redeem_device_code(self, code: str, device_name: str) -> tuple[dict[str, Any], str] | None:
        """Use a pairing or e-mailed code: a new device for its congregation. Returns (congregation, device token),
        or None for a code that is unknown, used, expired, or whose congregation is hidden. Single use, atomically."""
        c = re.sub(r"[\s-]", "", code or "").upper()
        if len(c) != DEVICE_CODE_LEN or any(ch not in CODE_ALPHABET for ch in c):
            return None
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT d.id, d.congregation_id FROM device_codes d JOIN congregations c"
                                   " ON c.id = d.congregation_id WHERE d.code_hash = ? AND d.used_at IS NULL"
                                   " AND d.expires_at > ? AND c.hidden = 0", (hash_token(c), now)).fetchone()
            if row is None or self._db.execute("UPDATE device_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
                                               (now, row["id"])).rowcount == 0:
                return None
        token, did = self._add_device(row["congregation_id"], device_name)
        self._x("UPDATE device_codes SET device_id = ? WHERE id = ?", (did, row["id"]))
        return self.congregation(row["congregation_id"]), token

    # ------------------------------------------------------------ own-key codes
    def mint_own_key_code(self, label: str) -> str:
        """A new one-time code for a church (label: who it's for, e.g. "Dakar Ward"). Only its hash is kept, so
        this is the one time the code can be seen."""
        label = " ".join((label or "").split())[:120]
        if not label:
            raise ValueError("say who the code is for")
        code = new_own_key_code()
        self._x("INSERT INTO own_key_codes (code_hash, label, created_at) VALUES (?,?,?)",
                (hash_token(normalize_own_key_code(code)), label, time.time()))
        return code

    def own_key_code_ok(self, code: str) -> bool:
        """True for a code that exists and hasn't been used. Doesn't use it up."""
        c = normalize_own_key_code(code)
        return bool(c and self._q("SELECT 1 FROM own_key_codes WHERE code_hash = ? AND used_at IS NULL"
                                    " AND voided_at IS NULL", (hash_token(c),)))

    def redeem_own_key_code(self, code: str, cid: int) -> bool:
        """Use the code up and switch the congregation to its own key, in one transaction. Asking again for the
        congregation that already used it is True (a retried request); any other used or unknown code is False."""
        c = normalize_own_key_code(code)
        if not c:
            return False
        h = hash_token(c)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT used_at, congregation_id, voided_at FROM own_key_codes"
                                       " WHERE code_hash = ?", (h,)).fetchone()
                if (row is None or (row["used_at"] is None and row["voided_at"] is not None)
                        or (row["used_at"] is not None and row["congregation_id"] != cid)):
                    self._db.execute("ROLLBACK")
                    return False
                if row["used_at"] is None:
                    self._db.execute("UPDATE own_key_codes SET used_at = ?, congregation_id = ? WHERE code_hash = ?",
                                     (time.time(), cid, h))
                self._db.execute("UPDATE congregations SET billing = 'own_key' WHERE id = ?", (cid,))
                self._db.execute("COMMIT")
                return True
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def own_key_codes(self, cid: int | None = None) -> list[dict[str, Any]]:
        """Every code made (or those congregation `cid` used), newest first, with its status ("unused", "used",
        "void") and who used it. Never the code itself: only its hash is kept."""
        rows = self._q("SELECT k.id, k.label, k.created_at, k.used_at, k.voided_at, k.congregation_id, c.slug, c.name"
                       " FROM own_key_codes k LEFT JOIN congregations c ON c.id = k.congregation_id"
                       + (" WHERE k.congregation_id = ?" if cid is not None else "") + " ORDER BY k.id DESC",
                       (cid,) if cid is not None else ())
        return [{"id": r["id"], "label": r["label"], "created_at": r["created_at"], "used_at": r["used_at"],
                 "voided_at": r["voided_at"],
                 "status": "used" if r["used_at"] else "void" if r["voided_at"] else "unused",
                 "used_by": r["slug"], "used_by_id": r["congregation_id"], "used_by_name": r["name"]} for r in rows]

    def void_own_key_code(self, kid: int) -> str | None:
        """Void an unused code so it stops working. Returns its status after ("void"; voiding again is fine), "used"
        when it was already used (nothing changes), None for an unknown id."""
        with self._lock:
            self._db.execute("UPDATE own_key_codes SET voided_at = ? WHERE id = ? AND used_at IS NULL"
                             " AND voided_at IS NULL", (time.time(), kid))
            row = self._db.execute("SELECT used_at FROM own_key_codes WHERE id = ?", (kid,)).fetchone()
        return None if row is None else "used" if row["used_at"] else "void"

    # ------------------------------------------------------------ devices
    def add_device(self, cid: int, name: str) -> str:
        """Create a device and return its token (the only time it exists in clear)."""
        return self._add_device(cid, name)[0]

    def _add_device(self, cid: int, name: str) -> tuple[str, int]:
        token = new_token("hcd")
        cur = self._x("INSERT INTO devices (congregation_id, name, token_hash, created_at) VALUES (?,?,?,?)",
                      (cid, (name or "")[:60] or "Mac", hash_token(token), time.time()))
        return token, cur.lastrowid

    def device_by_token(self, token: str) -> dict[str, Any] | None:
        """The device with this token; None for an unknown or revoked one."""
        rows = self._q("SELECT id, congregation_id, name FROM devices WHERE token_hash = ? AND revoked_at IS NULL",
                       (hash_token(token),))
        return dict(rows[0]) if rows else None

    def device(self, did: int) -> dict[str, Any] | None:
        rows = self._q("SELECT id, congregation_id, name, created_at, last_seen, app_version, revoked_at FROM devices"
                       " WHERE id = ?", (did,))
        return dict(rows[0]) if rows else None

    def revoke_device(self, did: int) -> bool:
        """The device's token stops working for good (the row stays, for the history). False for an unknown id;
        revoking again is fine."""
        self._x("UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (time.time(), did))
        return self.device(did) is not None

    def touch_device(self, did: int, app_version: str | None = None) -> None:
        """The Mac connected (or said hello): last seen now, and its app version when it told us one."""
        v = (app_version or "").strip()[:40] or None
        self._x("UPDATE devices SET last_seen = ?, app_version = COALESCE(?, app_version) WHERE id = ?",
                (time.time(), v, did))

    def devices(self, cid: int, revoked: bool = False) -> list[dict[str, Any]]:
        """The congregation's devices; revoked ones (with revoked_at) only when asked."""
        return [dict(r) for r in self._q("SELECT id, name, created_at, last_seen, app_version, revoked_at FROM devices"
                                         " WHERE congregation_id = ?" + ("" if revoked else " AND revoked_at IS NULL"),
                                         (cid,))]

    def all_devices(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT id, congregation_id, name, created_at, last_seen, app_version"
                                         " FROM devices ORDER BY id")]

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
    def start_session(self, cid: int, languages: list[str], trial: bool = False, code: str | None = None,
                      billing: str = "credits", device_id: int | None = None) -> int:
        """code: the hub's session code (the Mac's meter reports name it); billing: the congregation's mode now;
        device_id: the Mac that started it."""
        return self._x("INSERT INTO sessions (congregation_id, languages, started_at, trial, code, billing, device_id)"
                       " VALUES (?,?,?,?,?,?,?)", (cid, json.dumps(languages), time.time(), int(trial), code,
                                                   "own_key" if billing == "own_key" else "credits",
                                                   device_id)).lastrowid

    def end_session(self, sid: int) -> None:
        self._x("UPDATE sessions SET ended_at = COALESCE(ended_at, ?) WHERE id = ?", (time.time(), sid))

    def close_stale_sessions(self) -> None:
        """A server restart ends every session that was still open."""
        self._x("UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL", (time.time(),))

    def recent_session(self, cid: int, code: str, device_id: int, within_s: float = 6 * 3600) -> bool:
        """Did Mac `device_id` of congregation `cid` start session `code` within the last `within_s`? (a meeting
        being resumed). Another Mac of the congregation can't use the code to start a meeting without credit; a
        session from before device ids were kept matches no Mac."""
        return bool(code and self._q("SELECT 1 FROM sessions WHERE congregation_id = ? AND code = ? AND device_id = ?"
                                     " AND started_at > ?", (cid, code, device_id, time.time() - within_s)))

    def ended_lately(self, cid: int, device_id: int, within_s: float) -> bool:
        """Did Mac `device_id` end a meeting of congregation `cid` in the last `within_s`? (its last sentences may
        still be on their way through the translator)"""
        return bool(self._q("SELECT 1 FROM sessions WHERE congregation_id = ? AND device_id = ? AND ended_at > ?",
                            (cid, device_id, time.time() - within_s)))

    def sessions(self, cid: int) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM sessions WHERE congregation_id = ? ORDER BY id", (cid,))
        return [{**dict(r), "languages": json.loads(r["languages"]), "trial": bool(r["trial"])} for r in rows]

    # ------------------------------------------------------------ metering
    def record_speech(self, cid: int, code: str, items: list) -> list[int] | None:
        """Store the Mac's speech report for session `code` of congregation `cid`: items [{seq, s, langs}] = seconds
        of speech in chunk `seq`, translated into `langs` that someone was listening to. Returns the seqs taken
        (stored now or before: a resent report counts once), or None when the session is unknown. Malformed items
        are skipped and not acknowledged; a chunk's length is capped at MAX_CHUNK_S."""
        rows = self._q("SELECT id FROM sessions WHERE congregation_id = ? AND code = ? ORDER BY id DESC LIMIT 1",
                       (cid, str(code or "")))
        if not rows or not code:
            return None
        sid, taken = rows[0]["id"], []
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for it in items if isinstance(items, list) else []:
                    if not isinstance(it, dict):
                        continue
                    seq, s, langs = it.get("seq"), it.get("s"), it.get("langs")
                    if (not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(s, (int, float))
                            or isinstance(s, bool) or not s > 0 or not isinstance(langs, list)):
                        continue
                    langs = sorted({l for l in langs if isinstance(l, str) and 0 < len(l) <= 12})[:MAX_LANGS]
                    if not langs:
                        continue
                    if not self._db.execute("SELECT 1 FROM speech WHERE session_id = ? AND seq = ?",
                                            (sid, seq)).fetchone():
                        self._db.executemany("INSERT INTO speech (session_id, seq, lang, seconds) VALUES (?,?,?,?)",
                                             [(sid, seq, l, min(float(s), MAX_CHUNK_S)) for l in langs])
                    taken.append(seq)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return taken

    def speech_by_language(self, cid: int) -> dict[str, float]:
        """Seconds of heard speech per language, over all of a congregation's sessions."""
        rows = self._q("SELECT p.lang, SUM(p.seconds) AS s FROM speech p JOIN sessions s ON s.id = p.session_id"
                       " WHERE s.congregation_id = ? GROUP BY p.lang ORDER BY p.lang", (cid,))
        return {r["lang"]: round(r["s"], 2) for r in rows}

    # ------------------------------------------------------------ credits (the ledger)
    def add_credit(self, cid: int, payment_id: str, cents: int) -> tuple[dict[str, Any], bool]:
        """Credit a top-up paid with Stripe payment `payment_id` ($5 buys a language-hour). Idempotent: the same
        payment again returns the stored credit and False. ValueError for a blank id, a non-positive amount, or a
        payment id already used with another amount or congregation."""
        pid = (payment_id or "").strip()
        if not pid or len(pid) > 255:
            raise ValueError("a payment id is needed")
        if not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0:
            raise ValueError("the amount must be a positive number of cents")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM credits WHERE payment_id = ?", (pid,)).fetchone()
                if row is not None:
                    self._db.execute("ROLLBACK")
                    if row["congregation_id"] != cid or row["cents"] != cents:
                        raise ValueError("that payment was already credited with another amount or congregation")
                    return dict(row), False
                cur = self._db.execute("INSERT INTO credits (congregation_id, payment_id, cents, seconds, created_at)"
                                       " VALUES (?,?,?,?,?)",
                                       (cid, pid, cents, cents * 3600 / CENTS_PER_LANGUAGE_HOUR, time.time()))
                row = self._db.execute("SELECT * FROM credits WHERE id = ?", (cur.lastrowid,)).fetchone()
                self._db.execute("COMMIT")
                return dict(row), True
            except ValueError:
                raise
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def refund_credit(self, payment_id: str, refunded_cents: int, source: str = "stripe",
                      stripe_refund_id: str | None = None) -> tuple[dict[str, Any], bool]:
        """Stripe refunded `refunded_cents` in all (cumulative, as on the charge) of the top-up paid with
        `payment_id`: that much comes back off the balance, at the rate the top-up was bought at. Idempotent and
        order-proof: the refunded total only ever grows (a resent or late event changes nothing) and never exceeds
        the top-up. Returns (credit, changed). KeyError for a payment that was never credited, ValueError for a bad
        amount. The balance may go negative when credit already used is refunded. Each change is also written to
        `refunds` (source "stripe" = the charge.refunded webhook, "admin" = POST /api/admin/refunds) for the history."""
        pid = (payment_id or "").strip()
        if not isinstance(refunded_cents, int) or isinstance(refunded_cents, bool) or refunded_cents < 0:
            raise ValueError("the refunded amount must be a whole number of cents")
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT * FROM credits WHERE payment_id = ?", (pid,)).fetchone() if pid else None
                if row is None:
                    raise KeyError(pid)
                total = min(row["cents"], refunded_cents)
                changed = total > row["refunded_cents"]
                if changed:
                    now = time.time()
                    self._db.execute("UPDATE credits SET refunded_cents = ?, refunded_at = ? WHERE id = ?",
                                     (total, now, row["id"]))
                    self._db.execute("INSERT INTO refunds (credit_id, cents, total_cents, created_at, source,"
                                     " stripe_refund_id) VALUES (?,?,?,?,?,?)",
                                     (row["id"], total - row["refunded_cents"], total, now, source, stripe_refund_id))
                    row = self._db.execute("SELECT * FROM credits WHERE id = ?", (row["id"],)).fetchone()
                self._db.execute("COMMIT")
                return dict(row), changed
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def credits(self, cid: int | None = None) -> list[dict[str, Any]]:
        """A congregation's top-ups (every congregation's with cid None), oldest first."""
        if cid is None:
            return [dict(r) for r in self._q("SELECT * FROM credits ORDER BY id")]
        return [dict(r) for r in self._q("SELECT * FROM credits WHERE congregation_id = ? ORDER BY id", (cid,))]

    def credit_by_payment(self, payment_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM credits WHERE payment_id = ?", ((payment_id or "").strip(),))
        return dict(rows[0]) if rows else None

    def refunds(self) -> list[dict[str, Any]]:
        """Every refund taken off the ledger, oldest first, with its top-up's payment id and congregation."""
        return [dict(r) for r in self._q(
            "SELECT r.*, c.payment_id, c.congregation_id FROM refunds r JOIN credits c ON c.id = r.credit_id"
            " ORDER BY r.id")]

    def all_sessions(self) -> list[dict[str, Any]]:
        rows = self._q("SELECT * FROM sessions ORDER BY id")
        return [{**dict(r), "languages": json.loads(r["languages"]), "trial": bool(r["trial"])} for r in rows]

    def speech_by_session(self) -> dict[int, dict[str, float]]:
        """Seconds of heard speech per language, per session id (sessions with any speech only)."""
        out: dict[int, dict[str, float]] = {}
        for r in self._q("SELECT session_id, lang, SUM(seconds) AS s FROM speech GROUP BY session_id, lang"):
            out.setdefault(r["session_id"], {})[r["lang"]] = r["s"]
        return out

    def balance(self, cid: int) -> dict[str, Any]:
        """Where a congregation stands, in language-minutes (and dollars for what was bought):
        metered_min  all heard speech, own-key meetings included (stats)
        used_min     what its credit-billed meetings used; the free hour goes first
        free_left_min, balance_min / balance_usd (bought minus used beyond the free hour; may be negative),
        bought_usd, left_min (free + balance), can_start (False only for credits with nothing left)."""
        (metered, used), = self._q(
            "SELECT COALESCE(SUM(p.seconds), 0), COALESCE(SUM(CASE WHEN s.billing = 'credits' THEN p.seconds END), 0)"
            " FROM speech p JOIN sessions s ON s.id = p.session_id WHERE s.congregation_id = ?", (cid,))
        (cents, bought), = self._q("SELECT COALESCE(SUM(cents - refunded_cents), 0),"
                                   " COALESCE(SUM(seconds * (cents - refunded_cents) / cents), 0) FROM credits"
                                   " WHERE congregation_id = ?", (cid,))
        free_left = max(0.0, FREE_SECONDS - used)
        bal = bought - max(0.0, used - FREE_SECONDS)
        cong = self.congregation(cid)
        return {"metered_min": round(metered / 60, 2), "used_min": round(used / 60, 2),
                "free_left_min": round(free_left / 60, 2), "balance_min": round(bal / 60, 2),
                "balance_usd": round(bal / 3600 * CENTS_PER_LANGUAGE_HOUR / 100, 2), "bought_usd": round(cents / 100, 2),
                "left_min": round((free_left + bal) / 60, 2),
                "can_start": (cong or {}).get("billing") == "own_key" or free_left + bal > 0}

    def can_start(self, cong: dict[str, Any]) -> bool:
        """May this congregation start a new meeting? Own key: always. Credits: while the free hour or the balance
        has something left. A meeting already running is never stopped; only the next one waits for a top-up."""
        return cong["billing"] == "own_key" or self.balance(cong["id"])["can_start"]
