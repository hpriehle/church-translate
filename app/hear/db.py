"""SQLite storage for congregations, their devices (Macs) and their live sessions.

Plain stdlib sqlite3: one connection guarded by a lock (every query is tiny). Device tokens, e-mail link
tokens and one-time own-key codes are stored only as SHA-256 hashes.

Billing: a congregation pays with prepaid credits ("credits"), or with its own Anthropic key ("own_key"). Own key
is only reached with a one-time code Harrison makes for that church (`python -m app.hear.codes mint "Dakar Ward"`).
Congregations that existed before billing are grandfathered as "own_key": they were set up with their own keys.
"internal" (rare, set only by the operator, never advertised): the hub translates for it at no charge with Harrison's
own Claude Code login (app/hear/internal.py); its meetings are metered like the others but never debited or refused.

Metering and the ledger: the Mac reports seconds of speech per heard language (`record_speech`, idempotent on the
session and the Mac's chunk number). Credit is stored as the language-minutes bought (`credits.seconds`): a bundle
gives its hours (BUNDLES: $20 = 4 h, $50 = 11 h, $100 = 25 h, $250 = 70 h); top-ups from before the bundles keep the
minutes they got at $5 per language-hour. `add_credit` is idempotent on the Stripe payment id; a Stripe refund takes
its amount back off that top-up at the top-up's own rate (`refund_credit`: refunding $25 of the $50 bundle takes off
5.5 h). Every credit-billed congregation gets FREE_SECONDS (one language-hour) free per calendar month (UTC; a meeting
counts in the month it started; unused free time does not carry over). What a month's meetings use beyond that is
taken from the credit, oldest top-up first (`_walk`). Each session keeps
the billing mode it started under, so own-key meetings are metered but never debited, also after a switch to
credits. The balance may go negative (a meeting is never cut off); `can_start` then refuses the next one.
"""
from __future__ import annotations

import calendar
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
    revoked_at REAL,
    platform TEXT
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
CREATE TABLE IF NOT EXISTS slug_aliases (
    slug TEXT PRIMARY KEY,
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT,
    code_hash TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    value REAL NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT '',
    congregation_id INTEGER REFERENCES congregations(id),
    max_uses INTEGER NOT NULL DEFAULT 1,
    uses INTEGER NOT NULL DEFAULT 0,
    expires_at REAL,
    voided_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS code_redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_id INTEGER NOT NULL REFERENCES codes(id),
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    at REAL NOT NULL,
    status TEXT NOT NULL,
    payment_id TEXT,
    consumed_at REAL,
    UNIQUE (code_id, congregation_id)
);
CREATE TABLE IF NOT EXISTS payment_profiles (
    congregation_id INTEGER PRIMARY KEY REFERENCES congregations(id),
    customer_id TEXT,
    payment_method_id TEXT,
    card_brand TEXT,
    card_last4 TEXT,
    card_exp_month INTEGER,
    card_exp_year INTEGER,
    card_saved_at REAL,
    auto_topup INTEGER NOT NULL DEFAULT 0,
    auto_topup_cents INTEGER,
    auto_last_at REAL,
    auto_last_pi TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS web_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    purpose TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS web_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    csrf TEXT NOT NULL,
    email TEXT NOT NULL,
    congregation_id INTEGER REFERENCES congregations(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    device_id INTEGER,
    session_id INTEGER,
    code TEXT NOT NULL DEFAULT '',
    meeting TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    at REAL NOT NULL,
    received_at REAL NOT NULL,
    report TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS health_cong ON health(congregation_id, received_at);
CREATE INDEX IF NOT EXISTS health_received ON health(received_at);
CREATE TABLE IF NOT EXISTS device_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    congregation_id INTEGER NOT NULL REFERENCES congregations(id),
    device_id INTEGER,
    kind TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    received_at REAL NOT NULL,
    n_lines INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    lines TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS device_logs_cong ON device_logs(congregation_id, received_at);
CREATE INDEX IF NOT EXISTS device_logs_received ON device_logs(received_at);
CREATE INDEX IF NOT EXISTS web_codes_email ON web_codes(email, purpose);
CREATE INDEX IF NOT EXISTS web_codes_hash ON web_codes(code_hash);
CREATE INDEX IF NOT EXISTS web_sessions_email ON web_sessions(email);
CREATE INDEX IF NOT EXISTS sessions_open ON sessions(congregation_id, ended_at);
CREATE INDEX IF NOT EXISTS code_redemptions_cong ON code_redemptions(congregation_id, status);
CREATE INDEX IF NOT EXISTS slug_aliases_cong ON slug_aliases(congregation_id);
CREATE INDEX IF NOT EXISTS emails_address ON congregation_emails(email);
CREATE INDEX IF NOT EXISTS device_codes_cong ON device_codes(congregation_id, kind);
CREATE INDEX IF NOT EXISTS devices_cong ON devices(congregation_id);
CREATE INDEX IF NOT EXISTS credits_cong ON credits(congregation_id);
CREATE INDEX IF NOT EXISTS refunds_credit ON refunds(credit_id);
"""

# Prices (decided Nov 14): one language-hour free every calendar month (UTC) per congregation, then credit bundles,
# cheaper per hour as they grow: (price in cents, language-hours). Unlimited listeners.
BUNDLES = ((2000, 4), (5000, 11), (10000, 25), (25000, 70))
FREE_SECONDS = 3600            # per calendar month
# Before the bundles (Nov 8): $5 per language-hour. Top-ups bought then keep their minutes at this rate; a top-up of
# an amount that is no bundle (by hand) is credited at it; and an overdrawn balance is valued at it in dollars.
LEGACY_CENTS_PER_LANGUAGE_HOUR = 500


def bundle(cents: int) -> tuple[int, int] | None:
    """The bundle sold at this price, as (cents, language-hours), or None."""
    return next((b for b in BUNDLES if b[0] == cents), None)


def credit_seconds(cents: int) -> float:
    """Language-seconds a top-up of `cents` buys: the bundle's hours, or $5 per hour for any other amount."""
    b = bundle(cents)
    return float(b[1] * 3600) if b else cents * 3600 / LEGACY_CENTS_PER_LANGUAGE_HOUR


def month_of(t: float) -> str:
    """The calendar month (UTC) a time falls in, "2026-11": the free hour's period."""
    return time.strftime("%Y-%m", time.gmtime(t))


def next_month_start(t: float) -> float:
    """When the month after `t` begins (UTC midnight on the 1st): the free hour resets then."""
    y, m = time.gmtime(t)[:2]
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return float(calendar.timegm((y, m, 1, 0, 0, 0)))

PLATFORMS = ("mac-arm", "mac-intel", "windows")   # what a device runs on (devices.platform)


def device_platform(value: Any) -> str | None:
    """One of PLATFORMS, or None for anything else (older apps send nothing)."""
    v = value.strip().lower() if isinstance(value, str) else ""
    return v if v in PLATFORMS else None


BILLING_TYPES = ("credits", "own_key", "internal")
NO_CREDIT_NEEDED = ("own_key", "internal")   # metered, but never refused for balance or debited
MAX_CHUNK_S = 60.0     # one reported chunk of speech is never longer than this (the chunker cuts far sooner)
MAX_LANGS = 20
HEALTH_KEEP_S = 30 * 86400   # meeting health reports are kept 30 days
HEALTH_KINDS = ("start", "periodic", "end", "error")
LOGS_KEEP_S = 30 * 86400     # diagnostic logs (app/sharelog.py) are kept 30 days
LOGS_KEEP_BYTES = 20 * 2 ** 20   # and at most this much per congregation (the oldest stream batches go first)
LOG_KINDS = ("stream", "manual")

CONG_FIELDS = ("id", "slug", "church", "name", "unit_type", "stake", "address", "city", "lat", "lng", "meeting_times",
               "timezone", "email", "email_verified", "created_at", "hidden", "billing")

# hearchurch.com paths served by the download Worker and the marketing site, not by this app
# (docs/seo-site-structure.md). Add a new top-level page here before it goes live, after checking that no
# congregation already has that slug.
SITE_PAGES = frozenset({
    "app", "appcast", "blog", "compare", "credits", "demo", "download", "faq", "features", "how-it-works", "languages", "lds",
    "multilingual", "pricing", "rr", "rrweb", "setup", "sitemap", "small-churches", "source"})

# Listener pages live at hearchurch.com/<slug>, so a slug may never be one of the site's own top-level paths
# (tests/test_hear.py checks this list against the routes).
RESERVED_SLUGS = frozenset({
    "about", "account", "admin", "api", "c", "clips", "favicon-ico", "find", "guide", "health", "help", "listen", "listener",
    "login", "logout", "operator", "print", "privacy", "qr", "robots", "s", "signs", "signup", "speak", "start", "static",
    "stream", "support", "talk", "terms", "voices", "ws", "www"}) | SITE_PAGES


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


NEW_CODE_LEN = 8   # codes from the codes table (own key, free hours, % off): "7KQM-X2PA"
CODE_TYPES = ("own_key", "free_hours", "percent_off")
CODE_CREDIT_PREFIX = "code:"   # credits.payment_id of free hours from a code (cents 0)
AUTO_TOPUP_EVERY_S = 24 * 3600   # at most one automatic top-up per this long
AUTO_TOPUP_BELOW_S = 3600        # an auto top-up runs after a meeting when the paid hours left are below this


WEB_CODE_TTL_S = 10 * 60     # the 6-digit codes e-mailed by hearchurch.com/start and /account
WEB_CODE_TRIES = 5           # wrong guesses before such a code stops working
WEB_SESSION_TTL_S = 30 * 24 * 3600   # a hearchurch.com/account sign-in lasts this long (unless signed out)
WEB_LINK_TTL_S = 2 * 60      # the Mac's one-time "Manage billing" sign-in link
MAX_EMAILS = 10              # addresses per congregation
DEVICE_CODE_LEN = 8          # add-a-Mac codes "7KQM-X2PA": 30^8, ~39 bits, short-lived and behind failure limits
DEVICE_CODE_TTL_S = 15 * 60


def new_own_key_code() -> str:
    c = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    return f"HC-{c[:4]}-{c[4:8]}-{c[8:]}"


def new_code() -> str:
    """A code Harrison hands out (own key, free hours, % off): "7KQM-X2PA", 30^8 (~39 bits) behind the hub's
    failure limits."""
    c = "".join(secrets.choice(CODE_ALPHABET) for _ in range(NEW_CODE_LEN))
    return format_code(c)


def format_code(c: str) -> str:
    return f"{c[:4]}-{c[4:]}" if len(c) == NEW_CODE_LEN else c


def normalize_code(code: str) -> str | None:
    """As typed, any case, spaces or dashes ("7kqm x2pa", "HC-7KQM-X2PA-9TDW") → "7KQMX2PA" / "7KQMX2PA9TDW", or
    None if it can't be a code (the new 8-character ones and the old 12-character own-key ones)."""
    c = re.sub(r"[\s-]", "", code or "").upper()
    if c.startswith("HC") and len(c) == CODE_LEN + 2:
        c = c[2:]
    return c if len(c) in (CODE_LEN, NEW_CODE_LEN) and all(ch in CODE_ALPHABET for ch in c) else None


class CodeError(Exception):
    """A code can't be redeemed: str(e) is "invalid" (unknown, used, expired, voided, someone else's: the user only
    ever hears "That code isn't valid.") or "pending_discount"."""


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


def _not_refunded(c: dict[str, Any]) -> float:
    """A credit's seconds less what refunds took back (free hours from a code cost nothing and are never refunded)."""
    return c["seconds"] * (c["cents"] - c["refunded_cents"]) / c["cents"] if c["cents"] else c["seconds"]


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
            had_codes = bool(self._db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                                              " AND name = 'codes'").fetchone())
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
            if "platform" not in dcols:      # databases made before the Windows app (mac-arm, mac-intel, windows)
                self._db.execute("ALTER TABLE devices ADD COLUMN platform TEXT")
            kcols = {r[1] for r in self._db.execute("PRAGMA table_info(own_key_codes)")}
            if "voided_at" not in kcols:   # databases made before codes could be voided
                self._db.execute("ALTER TABLE own_key_codes ADD COLUMN voided_at REAL")
            if not had_codes:   # databases made before the codes table: their own-key codes move over, same ids
                self._db.execute("INSERT INTO codes (id, code, code_hash, type, value, label, max_uses, uses,"
                                 " voided_at, created_at) SELECT id, NULL, code_hash, 'own_key', 0, label, 1,"
                                 " used_at IS NOT NULL, voided_at, created_at FROM own_key_codes")
                self._db.execute("INSERT INTO code_redemptions (code_id, congregation_id, at, status)"
                                 " SELECT id, congregation_id, used_at, 'applied' FROM own_key_codes"
                                 " WHERE used_at IS NOT NULL AND congregation_id IS NOT NULL")
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
            if slug not in RESERVED_SLUGS and not self._slug_used(slug):
                return slug
            n += 1

    def _slug_used(self, slug: str) -> bool:
        """Some congregation's address now, or an old one that still redirects (printed QR codes)."""
        return bool(self._q("SELECT 1 FROM congregations WHERE slug = ? UNION SELECT 1 FROM slug_aliases"
                            " WHERE slug = ?", (slug, slug)))

    def change_slug(self, cid: int, new: str) -> str:
        """Give congregation `cid` the address hearchurch.com/<new>. The old slug becomes an alias that redirects to
        the new one for good. Taking back one of its own old slugs is fine (that alias goes; the current one becomes
        an alias). Returns "ok", "same", "reserved", "taken" or "not_found". The caller checks the format."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT slug FROM congregations WHERE id = ?", (cid,)).fetchone()
                if row is None:
                    result = "not_found"
                elif row["slug"] == new:
                    result = "same"
                elif new in RESERVED_SLUGS:
                    result = "reserved"
                elif (self._db.execute("SELECT 1 FROM congregations WHERE slug = ?", (new,)).fetchone()
                      or self._db.execute("SELECT 1 FROM slug_aliases WHERE slug = ? AND congregation_id != ?",
                                          (new, cid)).fetchone()):
                    result = "taken"
                else:
                    now = time.time()
                    self._db.execute("DELETE FROM slug_aliases WHERE slug = ?", (new,))
                    self._db.execute("INSERT INTO slug_aliases (slug, congregation_id, created_at) VALUES (?,?,?)",
                                     (row["slug"], cid, now))
                    self._db.execute("UPDATE congregations SET slug = ? WHERE id = ?", (new, cid))
                    result = "ok"
                self._db.execute("COMMIT" if result == "ok" else "ROLLBACK")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def slug_alias(self, slug: str) -> dict[str, Any] | None:
        """The congregation an old slug belongs to (it redirects to the current one), or None."""
        rows = self._q("SELECT c.* FROM slug_aliases a JOIN congregations c ON c.id = a.congregation_id"
                       " WHERE a.slug = ?", (slug,))
        return _cong(rows[0]) if rows else None

    def by_slug_or_alias(self, slug: str) -> dict[str, Any] | None:
        return self.by_slug(slug) or self.slug_alias(slug)

    def aliases(self, cid: int | None = None) -> list[dict[str, Any]]:
        """Old slugs that redirect (of one congregation, or all), oldest first: {slug, congregation_id, created_at}."""
        if cid is None:
            return [dict(r) for r in self._q("SELECT * FROM slug_aliases ORDER BY created_at, slug")]
        return [dict(r) for r in self._q("SELECT * FROM slug_aliases WHERE congregation_id = ? ORDER BY created_at,"
                                         " slug", (cid,))]

    def set_billing(self, cid: int, mode: str) -> bool:
        """Switch how a congregation pays: "credits", "own_key" (the operator unlocks or locks the own key) or
        "internal" (no charge, through the hub). Meetings keep the mode they started under. False for an unknown
        congregation."""
        if mode not in BILLING_TYPES:
            raise ValueError(mode)
        return self._x("UPDATE congregations SET billing = ? WHERE id = ?", (mode, cid)).rowcount > 0

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

    def redeem_device_code(self, code: str, device_name: str,
                           platform: str | None = None) -> tuple[dict[str, Any], str] | None:
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
        token, did = self._add_device(row["congregation_id"], device_name, platform)
        self._x("UPDATE device_codes SET device_id = ? WHERE id = ?", (did, row["id"]))
        return self.congregation(row["congregation_id"]), token

    # ------------------------------------------------------------ codes (own key, free hours, % off)
    def create_code(self, type: str, value: float = 0, label: str = "", congregation_id: int | None = None,
                    max_uses: int = 1, expires_at: float | None = None, legacy: bool = False) -> dict[str, Any]:
        """A new code (CODE_TYPES). Stored in plain so Harrison can list and hand it out again (never logged).
        legacy: the old own-key format "HC-XXXX-XXXX-XXXX" with only its hash kept (the deprecated
        /api/admin/own-key-codes route and `python -m app.hear.codes`); the answer then carries it once."""
        if type not in CODE_TYPES:
            raise ValueError(f"type: one of {', '.join(CODE_TYPES)}")
        label = " ".join((label or "").split())[:120]
        value = float(value or 0)
        if type == "free_hours" and not 0 < value <= 1000:
            raise ValueError("free_hours: value is the hours to add, more than 0 and at most 1000")
        if type == "percent_off" and not (value == int(value) and 1 <= value <= 99):
            raise ValueError("percent_off: value is a whole percent from 1 to 99")
        if type == "own_key":
            value = 0.0
        if not isinstance(max_uses, int) or not 1 <= max_uses <= 100_000:
            raise ValueError("max_uses: from 1 to 100000")
        if congregation_id is not None and self.congregation(congregation_id) is None:
            raise KeyError(congregation_id)
        for _ in range(5):
            code = new_own_key_code() if legacy else new_code()
            try:
                cur = self._x("INSERT INTO codes (code, code_hash, type, value, label, congregation_id, max_uses,"
                              " expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                              (None if legacy else code, hash_token(normalize_code(code)), type, value, label,
                               congregation_id, max_uses, expires_at, time.time()))
            except sqlite3.IntegrityError:
                continue
            return {**self.code(cur.lastrowid), "code": code}
        raise RuntimeError("could not make a unique code")

    def _code_row(self, code: str) -> sqlite3.Row | None:   # inside the lock
        c = normalize_code(code)
        return self._db.execute("SELECT * FROM codes WHERE code_hash = ?", (hash_token(c),)).fetchone() if c else None

    @staticmethod
    def _usable(row: sqlite3.Row | None, cid: int | None, now: float) -> bool:
        return bool(row is not None and row["voided_at"] is None
                    and (row["expires_at"] is None or row["expires_at"] > now)
                    and row["uses"] < row["max_uses"]
                    and (row["congregation_id"] is None or row["congregation_id"] == cid))

    def check_code(self, code: str, cid: int | None = None, type: str | None = None) -> dict[str, Any] | None:
        """The code, if `cid` (None: a congregation not made yet, at sign-up) could redeem it now (and it is of
        `type`, when given); else None. Uses nothing up."""
        with self._lock:
            row = self._code_row(code)
        if not self._usable(row, cid, time.time()) or (type and row["type"] != type):
            return None
        return self.code(row["id"])

    def redeem_code(self, code: str, cid: int, type: str | None = None) -> dict[str, Any]:
        """Use a code for congregation `cid`, all in one transaction: own_key switches its billing to own_key,
        free_hours adds a credit of that many hours that never expire (cents 0, payment id "code:<redemption id>"),
        percent_off is kept as a pending discount for its next bundle purchase. Returns {code, redemption,
        already}: already True when this congregation had redeemed this code before (a retried request; nothing
        happens again). CodeError("invalid") for an unknown, voided, expired, used-up or someone else's code (or
        not of `type`), CodeError("pending_discount") for a percent_off while another discount still waits."""
        now = time.time()
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._code_row(code)
                if row is None or (type and row["type"] != type):
                    raise CodeError("invalid")
                red = self._db.execute("SELECT * FROM code_redemptions WHERE code_id = ? AND congregation_id = ?",
                                       (row["id"], cid)).fetchone()
                if red is not None:
                    if row["type"] == "own_key":   # asked again (a retry): make sure the switch holds
                        self._db.execute("UPDATE congregations SET billing = 'own_key' WHERE id = ?", (cid,))
                    self._db.execute("COMMIT")
                    return {"code": self._code_dict(row), "redemption": dict(red), "already": True}
                if not self._usable(row, cid, now):
                    raise CodeError("invalid")
                if row["type"] == "percent_off" and self._db.execute(
                        "SELECT 1 FROM code_redemptions r JOIN codes k ON k.id = r.code_id WHERE r.congregation_id = ?"
                        " AND r.status = 'pending' AND k.type = 'percent_off'", (cid,)).fetchone():
                    raise CodeError("pending_discount")
                status = "pending" if row["type"] == "percent_off" else "applied"
                rid = self._db.execute("INSERT INTO code_redemptions (code_id, congregation_id, at, status)"
                                       " VALUES (?,?,?,?)", (row["id"], cid, now, status)).lastrowid
                self._db.execute("UPDATE codes SET uses = uses + 1 WHERE id = ?", (row["id"],))
                if row["type"] == "own_key":
                    self._db.execute("UPDATE congregations SET billing = 'own_key' WHERE id = ?", (cid,))
                elif row["type"] == "free_hours":
                    self._db.execute("INSERT INTO credits (congregation_id, payment_id, cents, seconds, created_at)"
                                     " VALUES (?,?,0,?,?)", (cid, f"{CODE_CREDIT_PREFIX}{rid}", row["value"] * 3600, now))
                red = self._db.execute("SELECT * FROM code_redemptions WHERE id = ?", (rid,)).fetchone()
                row = self._db.execute("SELECT * FROM codes WHERE id = ?", (row["id"],)).fetchone()
                self._db.execute("COMMIT")
                return {"code": self._code_dict(row), "redemption": dict(red), "already": False}
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def pending_discount(self, cid: int) -> dict[str, Any] | None:
        """The percent_off waiting for this congregation's next bundle purchase: {redemption_id, code_id, percent,
        label}, or None."""
        rows = self._q("SELECT r.id, r.code_id, k.value, k.label FROM code_redemptions r JOIN codes k ON k.id = r.code_id"
                       " WHERE r.congregation_id = ? AND r.status = 'pending' AND k.type = 'percent_off'"
                       " ORDER BY r.id LIMIT 1", (cid,))
        return ({"redemption_id": rows[0]["id"], "code_id": rows[0]["code_id"], "percent": int(rows[0]["value"]),
                 "label": rows[0]["label"]} if rows else None)

    def consume_discount(self, redemption_id: int, payment_id: str) -> bool:
        """A purchase with the discount was paid: it is used up (once; again is False)."""
        return self._x("UPDATE code_redemptions SET status = 'consumed', payment_id = ?, consumed_at = ?"
                       " WHERE id = ? AND status = 'pending'", (payment_id, time.time(), redemption_id)).rowcount > 0

    def _code_dict(self, r: sqlite3.Row) -> dict[str, Any]:
        now = time.time()
        status = ("void" if r["voided_at"] else "used" if r["uses"] >= r["max_uses"]
                  else "expired" if r["expires_at"] is not None and r["expires_at"] <= now else "active")
        return {"id": r["id"], "code": format_code(r["code"]) if r["code"] else None, "type": r["type"],
                "value": int(r["value"]) if r["value"] == int(r["value"]) else r["value"], "label": r["label"],
                "congregation_id": r["congregation_id"], "max_uses": r["max_uses"], "uses": r["uses"],
                "expires_at": r["expires_at"], "voided_at": r["voided_at"], "created_at": r["created_at"],
                "status": status}

    def code(self, kid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM codes WHERE id = ?", (kid,))
        return self._code_dict(rows[0]) if rows else None

    def code_redemptions(self, kid: int | None = None, cid: int | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if kid is not None:
            where.append("r.code_id = ?"); args.append(kid)
        if cid is not None:
            where.append("r.congregation_id = ?"); args.append(cid)
        rows = self._q("SELECT r.*, c.slug, c.name, k.type, k.value, k.label FROM code_redemptions r"
                       " JOIN congregations c ON c.id = r.congregation_id JOIN codes k ON k.id = r.code_id"
                       + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY r.id", tuple(args))
        return [dict(r) for r in rows]

    def codes(self, type: str | None = None, status: str | None = None, congregation_id: int | None = None,
              q: str = "") -> list[dict[str, Any]]:
        """Every code, newest first, each with its redemptions; filters: type, status (active, used, expired,
        void), congregation_id (assigned to it, or redeemed by it), q (in the label or the code)."""
        reds: dict[int, list] = {}
        for r in self.code_redemptions():
            reds.setdefault(r["code_id"], []).append(
                {"congregation_id": r["congregation_id"], "slug": r["slug"], "name": r["name"], "at": r["at"],
                 "status": r["status"], "payment_id": r["payment_id"], "consumed_at": r["consumed_at"]})
        out = []
        for row in self._q("SELECT * FROM codes ORDER BY id DESC"):
            d = {**self._code_dict(row), "redemptions": reds.get(row["id"], [])}
            if type and d["type"] != type or status and d["status"] != status:
                continue
            if congregation_id is not None and d["congregation_id"] != congregation_id and not any(
                    x["congregation_id"] == congregation_id for x in d["redemptions"]):
                continue
            if q and q.lower() not in d["label"].lower() and q.upper().replace("-", "") not in (
                    d["code"] or "").replace("-", ""):
                continue
            out.append(d)
        return out

    def void_code(self, kid: int) -> str | None:
        """Void a code so it stops working (a partly used multi-use code too). Returns its status after ("void";
        voiding again is fine), "used" when it was already used up (nothing changes), None for an unknown id."""
        with self._lock:
            self._db.execute("UPDATE codes SET voided_at = ? WHERE id = ? AND uses < max_uses AND voided_at IS NULL",
                             (time.time(), kid))
            row = self._db.execute("SELECT uses, max_uses, voided_at FROM codes WHERE id = ?", (kid,)).fetchone()
        return None if row is None else "void" if row["voided_at"] else "used"

    # The own-key code calls from before the codes table (the CLI and /api/admin/own-key-codes, deprecated):
    def mint_own_key_code(self, label: str) -> str:
        """A one-time own-key code in the old format, only its hash kept: this is the one time it can be seen."""
        label = " ".join((label or "").split())[:120]
        if not label:
            raise ValueError("say who the code is for")
        return self.create_code("own_key", label=label, legacy=True)["code"]

    def own_key_code_ok(self, code: str) -> bool:
        return self.check_code(code, None, "own_key") is not None

    def redeem_own_key_code(self, code: str, cid: int) -> bool:
        try:
            self.redeem_code(code, cid, "own_key")
            return True
        except CodeError:
            return False

    def own_key_codes(self, cid: int | None = None) -> list[dict[str, Any]]:
        """The own-key codes (or those congregation `cid` used), newest first, as the old route listed them:
        status "unused", "used" or "void", who used it, never the code itself."""
        out = []
        for d in self.codes(type="own_key", congregation_id=cid):
            r = d["redemptions"][0] if d["redemptions"] else None
            if cid is not None and (r is None or r["congregation_id"] != cid):
                continue
            out.append({"id": d["id"], "label": d["label"], "created_at": d["created_at"],
                        "used_at": r["at"] if r else None, "voided_at": d["voided_at"],
                        "status": "used" if r else "void" if d["voided_at"] else "unused",
                        "used_by": r["slug"] if r else None, "used_by_id": r["congregation_id"] if r else None,
                        "used_by_name": r["name"] if r else None})
        return out

    def void_own_key_code(self, kid: int) -> str | None:
        row = self.code(kid)
        return None if row is None or row["type"] != "own_key" else self.void_code(kid)

    # ------------------------------------------------------------ saved card and auto top-up
    def payment_profile(self, cid: int) -> dict[str, Any]:
        """The congregation's Stripe customer, saved card and auto top-up settings (all empty by default)."""
        rows = self._q("SELECT * FROM payment_profiles WHERE congregation_id = ?", (cid,))
        if rows:
            d = dict(rows[0])
            d["auto_topup"] = bool(d["auto_topup"])
            return d
        return {"congregation_id": cid, "customer_id": None, "payment_method_id": None, "card_brand": None,
                "card_last4": None, "card_exp_month": None, "card_exp_year": None, "card_saved_at": None,
                "auto_topup": False, "auto_topup_cents": None, "auto_last_at": None, "auto_last_pi": None,
                "updated_at": None}

    def _profile_set(self, cid: int, **fields) -> dict[str, Any]:
        fields["updated_at"] = time.time()
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO payment_profiles (congregation_id) VALUES (?)", (cid,))
            self._db.execute(f"UPDATE payment_profiles SET {', '.join(f'{k} = ?' for k in fields)}"
                             " WHERE congregation_id = ?", (*fields.values(), cid))
        return self.payment_profile(cid)

    def set_customer(self, cid: int, customer_id: str) -> dict[str, Any]:
        return self._profile_set(cid, customer_id=customer_id)

    def set_card(self, cid: int, payment_method_id: str, brand: str | None, last4: str | None,
                 exp_month: int | None = None, exp_year: int | None = None) -> dict[str, Any]:
        return self._profile_set(cid, payment_method_id=payment_method_id, card_brand=brand, card_last4=last4,
                                 card_exp_month=exp_month, card_exp_year=exp_year, card_saved_at=time.time())

    def clear_card(self, cid: int) -> dict[str, Any]:
        """The card is removed: auto top-up goes off with it."""
        return self._profile_set(cid, payment_method_id=None, card_brand=None, card_last4=None, card_exp_month=None,
                                 card_exp_year=None, card_saved_at=None, auto_topup=0)

    def set_auto_topup(self, cid: int, enabled: bool, cents: int | None = None) -> dict[str, Any]:
        if cents is not None and bundle(cents) is None:
            raise ValueError("not a bundle")
        f: dict[str, Any] = {"auto_topup": int(bool(enabled))}
        if cents is not None:
            f["auto_topup_cents"] = cents
        return self._profile_set(cid, **f)

    def claim_auto_charge(self, cid: int, now: float | None = None, every_s: float = AUTO_TOPUP_EVERY_S) -> bool:
        """Take the one auto top-up allowed per `every_s` (24 h): True once, and False for anyone asking again within
        that time, so two meetings ending together never charge twice."""
        now = time.time() if now is None else now
        return self._x("UPDATE payment_profiles SET auto_last_at = ? WHERE congregation_id = ? AND auto_topup = 1"
                       " AND payment_method_id IS NOT NULL AND (auto_last_at IS NULL OR auto_last_at <= ?)",
                       (now, cid, now - every_s)).rowcount > 0

    def set_auto_last_pi(self, cid: int, pi: str) -> None:
        self._profile_set(cid, auto_last_pi=pi)

    def congregation_by_customer(self, customer_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT congregation_id FROM payment_profiles WHERE customer_id = ?", (customer_id or "",))
        return self.congregation(rows[0]["congregation_id"]) if rows else None

    # ------------------------------------------------------------ devices
    def add_device(self, cid: int, name: str, platform: str | None = None) -> str:
        """Create a device and return its token (the only time it exists in clear)."""
        return self._add_device(cid, name, platform)[0]

    def _add_device(self, cid: int, name: str, platform: str | None = None) -> tuple[str, int]:
        token = new_token("hcd")
        cur = self._x("INSERT INTO devices (congregation_id, name, token_hash, created_at, platform) VALUES (?,?,?,?,?)",
                      (cid, (name or "")[:60] or "Mac", hash_token(token), time.time(), device_platform(platform)))
        return token, cur.lastrowid

    def device_by_token(self, token: str) -> dict[str, Any] | None:
        """The device with this token; None for an unknown or revoked one."""
        rows = self._q("SELECT id, congregation_id, name FROM devices WHERE token_hash = ? AND revoked_at IS NULL",
                       (hash_token(token),))
        return dict(rows[0]) if rows else None

    def device(self, did: int) -> dict[str, Any] | None:
        rows = self._q("SELECT id, congregation_id, name, created_at, last_seen, app_version, revoked_at, platform"
                       " FROM devices"
                       " WHERE id = ?", (did,))
        return dict(rows[0]) if rows else None

    def revoke_device(self, did: int) -> bool:
        """The device's token stops working for good (the row stays, for the history). False for an unknown id;
        revoking again is fine."""
        self._x("UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (time.time(), did))
        return self.device(did) is not None

    def touch_device(self, did: int, app_version: str | None = None, platform: str | None = None) -> None:
        """The device connected (or said hello): last seen now, and its app version and platform when it told us."""
        v = (app_version or "").strip()[:40] or None
        self._x("UPDATE devices SET last_seen = ?, app_version = COALESCE(?, app_version),"
                " platform = COALESCE(?, platform) WHERE id = ?", (time.time(), v, device_platform(platform), did))

    def devices(self, cid: int, revoked: bool = False) -> list[dict[str, Any]]:
        """The congregation's devices; revoked ones (with revoked_at) only when asked."""
        return [dict(r) for r in self._q("SELECT id, name, created_at, last_seen, app_version, revoked_at, platform"
                                         " FROM devices"
                                         " WHERE congregation_id = ?" + ("" if revoked else " AND revoked_at IS NULL"),
                                         (cid,))]

    def all_devices(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT id, congregation_id, name, created_at, last_seen, app_version,"
                                         " platform FROM devices ORDER BY id")]

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
                                                   billing if billing in BILLING_TYPES else "credits",
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

    # ------------------------------------------------------------ meeting health reports (app/health.py)
    def record_health(self, cid: int, device_id: int | None, reports: list[dict[str, Any]],
                      now: float | None = None) -> int:
        """Store the Mac's health reports (already checked by the API: dicts with a known kind). Each names its hub
        session code (may be "": the hub hadn't named it, or refused the meeting); the session is looked up among the
        congregation's own. A report's `at` (the Mac's clock) is kept unless it is more than a day off. Reports older
        than HEALTH_KEEP_S are deleted. Returns how many were stored."""
        now = time.time() if now is None else now
        ids: dict[str, int | None] = {}
        rows = []
        for r in reports:
            code = str(r.get("session") or "")[:40]
            if code not in ids:
                found = self._q("SELECT id FROM sessions WHERE congregation_id = ? AND code = ? ORDER BY id DESC"
                                " LIMIT 1", (cid, code)) if code else []
                ids[code] = found[0]["id"] if found else None
            at = r.get("at")
            at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) and abs(at - now) < 86400 \
                else now
            rows.append((cid, device_id, ids[code], code, str(r.get("meeting") or "")[:40], r["kind"], at, now,
                         json.dumps(r, separators=(",", ":"))))
        with self._lock:
            self._db.executemany("INSERT INTO health (congregation_id, device_id, session_id, code, meeting, kind, at,"
                                 " received_at, report) VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self._db.execute("DELETE FROM health WHERE received_at < ?", (now - HEALTH_KEEP_S,))
        return len(rows)

    def health(self, cid: int, since: float = 0.0, limit: int = 1000, session: str = "") -> list[dict[str, Any]]:
        """A congregation's health reports received after `since` (unix time), oldest first, at most `limit` (the
        newest of them)."""
        sql = "SELECT * FROM health WHERE congregation_id = ? AND received_at > ?"
        args: tuple = (cid, since)
        if session:
            sql, args = sql + " AND code = ?", args + (session,)
        rows = self._q(f"SELECT * FROM ({sql} ORDER BY id DESC LIMIT ?) ORDER BY id", args + (limit,))
        return [{**{k: r[k] for k in ("id", "device_id", "session_id", "code", "meeting", "kind", "at", "received_at")},
                 "report": json.loads(r["report"])} for r in rows]

    # ------------------------------------------------------------ diagnostic logs (app/sharelog.py)
    def record_logs(self, cid: int, device_id: int | None, kind: str, lines: list[dict[str, Any]], note: str = "",
                    now: float | None = None) -> int:
        """Store one batch of a Mac's scrubbed log lines (already checked by the API). Batches older than LOGS_KEEP_S
        are deleted, and a congregation's oldest "stream" batches once it holds more than LOGS_KEEP_BYTES. Returns
        the batch id (the "Reference #" the Mac shows after "Send logs to support")."""
        now = time.time() if now is None else now
        text = json.dumps(lines, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            cur = self._db.execute("INSERT INTO device_logs (congregation_id, device_id, kind, note, received_at,"
                                   " n_lines, bytes, lines) VALUES (?,?,?,?,?,?,?,?)",
                                   (cid, device_id, kind, note, now, len(lines), len(text.encode()), text))
            rid = int(cur.lastrowid)
            self._db.execute("DELETE FROM device_logs WHERE received_at < ?", (now - LOGS_KEEP_S,))
            total = self._db.execute("SELECT COALESCE(SUM(bytes), 0) FROM device_logs WHERE congregation_id = ?",
                                     (cid,)).fetchone()[0]
            if total > LOGS_KEEP_BYTES:
                for r in self._db.execute("SELECT id, bytes FROM device_logs WHERE congregation_id = ? AND"
                                          " kind = 'stream' AND id != ? ORDER BY id", (cid, rid)).fetchall():
                    if total <= LOGS_KEEP_BYTES:
                        break
                    self._db.execute("DELETE FROM device_logs WHERE id = ?", (r[0],))
                    total -= r[1]
        return rid

    def logs(self, cid: int, since: float = 0.0, limit: int = 200, kind: str = "",
             device_id: int | None = None) -> list[dict[str, Any]]:
        """A congregation's log batches received after `since` (unix time), oldest first, at most `limit` (the
        newest of them)."""
        sql = "SELECT * FROM device_logs WHERE congregation_id = ? AND received_at > ?"
        args: tuple = (cid, since)
        if kind:
            sql, args = sql + " AND kind = ?", args + (kind,)
        if device_id is not None:
            sql, args = sql + " AND device_id = ?", args + (device_id,)
        rows = self._q(f"SELECT * FROM ({sql} ORDER BY id DESC LIMIT ?) ORDER BY id", args + (limit,))
        return [{**{k: r[k] for k in ("id", "device_id", "kind", "note", "received_at", "n_lines")},
                 "lines": json.loads(r["lines"])} for r in rows]

    def log_batch(self, ref: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM device_logs WHERE id = ?", (ref,))
        if not rows:
            return None
        r = rows[0]
        return {**{k: r[k] for k in ("id", "congregation_id", "device_id", "kind", "note", "received_at", "n_lines")},
                "lines": json.loads(r["lines"])}

    def speech_by_language(self, cid: int) -> dict[str, float]:
        """Seconds of heard speech per language, over all of a congregation's sessions."""
        rows = self._q("SELECT p.lang, SUM(p.seconds) AS s FROM speech p JOIN sessions s ON s.id = p.session_id"
                       " WHERE s.congregation_id = ? GROUP BY p.lang ORDER BY p.lang", (cid,))
        return {r["lang"]: round(r["s"], 2) for r in rows}

    # ------------------------------------------------------------ credits (the ledger)
    def add_credit(self, cid: int, payment_id: str, cents: int,
                   seconds: float | None = None) -> tuple[dict[str, Any], bool]:
        """Credit a top-up paid with Stripe payment `payment_id`: `seconds` of language time (default: the bundle's
        hours for a bundle price, else $5 per hour, `credit_seconds`). Idempotent: the same payment again returns the
        stored credit and False. ValueError for a blank id, a non-positive amount, or a payment id already used with
        another amount or congregation."""
        pid = (payment_id or "").strip()
        if not pid or len(pid) > 255:
            raise ValueError("a payment id is needed")
        if not isinstance(cents, int) or isinstance(cents, bool) or cents <= 0:
            raise ValueError("the amount must be a positive number of cents")
        secs = credit_seconds(cents) if seconds is None else float(seconds)
        if not secs > 0:
            raise ValueError("the credit must be some language time")
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
                                       " VALUES (?,?,?,?,?)", (cid, pid, cents, secs, time.time()))
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
        `payment_id`: that much comes back off the balance pro rata, at the top-up's own rate (cents / seconds: refunding
$25 of the $50 bundle takes 5.5 of its 11 hours). Idempotent and
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

    def _walk(self, cid: int, now: float | None = None) -> dict[str, Any]:
        """The ledger of one congregation, worked out from its meetings and top-ups. Credit-billed meetings, in the
        order they started, first use their month's free hour (FREE_SECONDS per UTC month, by the meeting's start);
        the rest is taken from the top-ups, oldest first, each at its own rate; what no top-up covers is overdrawn.
        Returns sessions {id: {secs, free, paid, cost_cents}} (all of its metered meetings; own-key and internal ones
        cost nothing), unused {credit id: seconds}, overdrawn (seconds), used/metered seconds and this month's use."""
        now = time.time() if now is None else now
        rows = self._q("SELECT s.id, s.started_at, s.billing, SUM(p.seconds) AS secs FROM sessions s"
                       " JOIN speech p ON p.session_id = s.id WHERE s.congregation_id = ?"
                       " GROUP BY s.id ORDER BY s.started_at, s.id", (cid,))
        credits = [dict(r) for r in self._q("SELECT * FROM credits WHERE congregation_id = ? ORDER BY id", (cid,))]
        left = {c["id"]: _not_refunded(c) for c in credits}
        rate = {c["id"]: c["cents"] / c["seconds"] for c in credits}   # cents per second, the top-up's own (0: a code)
        months: dict[str, float] = {}
        sessions: dict[int, dict[str, float]] = {}
        over = used = metered = 0.0
        for r in rows:
            secs = r["secs"] or 0.0
            metered += secs
            free = paid = cost = 0.0
            if r["billing"] == "credits":
                used += secs
                m = month_of(r["started_at"])
                free = max(0.0, min(secs, FREE_SECONDS - months.get(m, 0.0)))
                months[m] = months.get(m, 0.0) + secs
                paid = need = secs - free
                for c in credits:
                    if need <= 0:
                        break
                    take = min(need, left[c["id"]])
                    if take > 0:
                        left[c["id"]] -= take
                        need -= take
                        cost += take * rate[c["id"]]
                if need > 0:
                    over += need
                    cost += need * LEGACY_CENTS_PER_LANGUAGE_HOUR / 3600
            sessions[r["id"]] = {"secs": secs, "free": free, "paid": paid, "cost_cents": cost}
        return {"sessions": sessions, "unused": left, "rate": rate, "overdrawn": over, "used": used,
                "metered": metered, "month": month_of(now), "month_used": months.get(month_of(now), 0.0),
                "credits": credits}

    def unused_cents(self, credit: dict[str, Any]) -> int:
        """What the unused part of a top-up is worth at its own rate: refunded on request (used hours are final)."""
        w = self._walk(credit["congregation_id"])
        return max(0, round(w["unused"].get(credit["id"], 0.0) * w["rate"].get(credit["id"], 0.0)))

    def balance(self, cid: int, now: float | None = None) -> dict[str, Any]:
        """Where a congregation stands, in language-minutes (and dollars for what was bought):
        metered_min  all heard speech, own-key meetings included (stats)
        used_min     what its credit-billed meetings used, all time; month_used_min: this month (UTC)
        free_left_min  what is left of this month's free hour (free_month "2026-11", free_resets_at: next 1st, UTC)
        balance_min  bought minutes not used yet (negative when overdrawn); balance_usd: what those are worth at the
                     rates they were bought at (an overdraft at $5 per hour)
        bought_min / bought_usd  all top-ups less refunds; left_min (free + balance), can_start (False only for
        credits with nothing left)."""
        now = time.time() if now is None else now
        w = self._walk(cid, now)
        unused = sum(v for v in w["unused"].values() if v > 0)
        bal = unused - w["overdrawn"]
        usd = (sum(v * w["rate"][k] for k, v in w["unused"].items() if v > 0)
               - w["overdrawn"] * LEGACY_CENTS_PER_LANGUAGE_HOUR / 3600) / 100
        cents = sum(c["cents"] - c["refunded_cents"] for c in w["credits"])
        bought = sum(_not_refunded(c) for c in w["credits"])
        free_left = max(0.0, FREE_SECONDS - w["month_used"])
        cong = self.congregation(cid)
        return {"metered_min": round(w["metered"] / 60, 2), "used_min": round(w["used"] / 60, 2),
                "month_used_min": round(w["month_used"] / 60, 2), "free_month": w["month"],
                "free_min_per_month": FREE_SECONDS // 60, "free_left_min": round(free_left / 60, 2),
                "free_resets_at": next_month_start(now),
                "balance_min": round(bal / 60, 2), "balance_usd": round(usd, 2), "bought_min": round(bought / 60, 2),
                "bought_usd": round(cents / 100, 2), "left_min": round((free_left + bal) / 60, 2),
                "can_start": (cong or {}).get("billing") in NO_CREDIT_NEEDED or free_left + bal > 0}

    def can_start(self, cong: dict[str, Any]) -> bool:
        """May this congregation start a new meeting? Own key and internal: always. Credits: while this month's free hour or
        the balance has something left. A meeting already running is never stopped; only the next one waits for a top-up."""
        return cong["billing"] in NO_CREDIT_NEEDED or self.balance(cong["id"])["can_start"]

    # ------------------------------------------------------------ congregation details (Mac Settings, /account)
    def update_congregation(self, cid: int, *, name: str | None = None, stake: str | None = None,
                            meeting_times: list | None = None, timezone: str | None = None) -> dict[str, Any] | None:
        """Change what the congregation calls itself and when it meets; None leaves a field as it is. The slug
        (listener link) doesn't follow the name: that is change_slug."""
        sets, args = [], []
        for col, v in (("name", name), ("stake", stake), ("timezone", timezone)):
            if v is not None:
                sets.append(f"{col} = ?")
                args.append(v if col != "stake" else (v or None))
        if meeting_times is not None:
            sets.append("meeting_times = ?")
            args.append(json.dumps(meeting_times))
        if sets:
            self._x(f"UPDATE congregations SET {', '.join(sets)} WHERE id = ?", (*args, cid))
        return self.congregation(cid)

    # ------------------------------------------------------------ hearchurch.com/start and /account (app/hear/web.py)
    def new_web_code(self, email: str, purpose: str, ttl_s: float = WEB_CODE_TTL_S) -> str:
        """A 6-digit code e-mailed to `email` ("signup": prove the address before the congregation is made;
        "login": sign in to /account). Earlier unused codes of that address and purpose stop working."""
        if purpose not in ("signup", "login"):
            raise ValueError(purpose)
        email = email.strip().lower()
        code = f"{secrets.randbelow(10 ** 6):06d}"
        now = time.time()
        with self._lock:
            self._db.execute("UPDATE web_codes SET expires_at = ? WHERE email = ? AND purpose = ? AND used_at IS NULL"
                             " AND expires_at > ?", (now, email, purpose, now))
            self._db.execute("INSERT INTO web_codes (email, purpose, code_hash, created_at, expires_at)"
                             " VALUES (?,?,?,?,?)", (email, purpose, hash_token(f"{email}:{code}"), now, now + ttl_s))
        return code

    def use_web_code(self, email: str, purpose: str, code: str) -> bool:
        """Is `code` the live code for this address and purpose? Uses it up if so. Each wrong guess counts; after
        WEB_CODE_TRIES the code is dead."""
        email = email.strip().lower()
        code = re.sub(r"\s", "", code or "")
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT id, code_hash, attempts FROM web_codes WHERE email = ? AND purpose = ?"
                                   " AND used_at IS NULL AND expires_at > ? ORDER BY id DESC LIMIT 1",
                                   (email, purpose, now)).fetchone()
            if row is None or row["attempts"] >= WEB_CODE_TRIES:
                return False
            if not (code.isdigit() and hmac.compare_digest(row["code_hash"], hash_token(f"{email}:{code}"))):
                self._db.execute("UPDATE web_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
                return False
            return self._db.execute("UPDATE web_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
                                    (now, row["id"])).rowcount == 1

    def new_web_link(self, email: str, cid: int, ttl_s: float = WEB_LINK_TTL_S) -> str:
        """A one-time sign-in link token for the Mac's "Buy credit / Manage billing" button: it opens
        hearchurch.com/account already signed in as the congregation's main address. Kept as a web_codes row
        (purpose "link:<congregation id>"), only its hash."""
        token = new_token("hcl")
        now = time.time()
        self._x("INSERT INTO web_codes (email, purpose, code_hash, created_at, expires_at) VALUES (?,?,?,?,?)",
                (email.strip().lower(), f"link:{cid}", hash_token(token), now, now + ttl_s))
        return token

    def use_web_link(self, token: str) -> tuple[str, int] | None:
        """(address, congregation id) for a live, unused link token, which is used up; None otherwise."""
        if not token or len(token) > 200:
            return None
        now = time.time()
        with self._lock:
            row = self._db.execute("SELECT id, email, purpose FROM web_codes WHERE code_hash = ? AND purpose LIKE 'link:%'"
                                   " AND used_at IS NULL AND expires_at > ?", (hash_token(token), now)).fetchone()
            if row is None or self._db.execute("UPDATE web_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
                                               (now, row["id"])).rowcount != 1:
                return None
        return row["email"], int(row["purpose"].split(":", 1)[1])

    def new_web_session(self, email: str, cid: int | None, ttl_s: float = WEB_SESSION_TTL_S) -> tuple[str, str]:
        """A signed-in browser: returns (session token for the cookie, CSRF token). Only hashes of the token are kept."""
        token, csrf = new_token("hcw"), secrets.token_urlsafe(24)
        now = time.time()
        self._x("INSERT INTO web_sessions (token_hash, csrf, email, congregation_id, created_at, expires_at, last_seen)"
                " VALUES (?,?,?,?,?,?,?)", (hash_token(token), csrf, email.strip().lower(), cid, now, now + ttl_s, now))
        return token, csrf

    def web_session(self, token: str) -> dict[str, Any] | None:
        """The live session with this token (not expired, not signed out), or None."""
        if not token:
            return None
        rows = self._q("SELECT * FROM web_sessions WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?",
                       (hash_token(token), time.time()))
        return dict(rows[0]) if rows else None

    def touch_web_session(self, sid: int) -> None:
        self._x("UPDATE web_sessions SET last_seen = ? WHERE id = ?", (time.time(), sid))

    def set_web_session_congregation(self, sid: int, cid: int) -> None:
        self._x("UPDATE web_sessions SET congregation_id = ? WHERE id = ?", (cid, sid))

    def revoke_web_session(self, sid: int) -> None:
        self._x("UPDATE web_sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?", (time.time(), sid))

    def revoke_web_sessions(self, email: str, cid: int | None = None) -> int:
        """Sign an address out everywhere (of one congregation, or all). Returns how many sessions ended."""
        sql = "UPDATE web_sessions SET revoked_at = ? WHERE email = ? AND revoked_at IS NULL"
        args: tuple = (time.time(), email.strip().lower())
        if cid is not None:
            sql += " AND congregation_id = ?"
            args += (cid,)
        return self._x(sql, args).rowcount
