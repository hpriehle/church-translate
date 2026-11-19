"""Trial lifecycle e-mails to a new congregation's sign-up address (docs/trial-emails.md): welcome, day 3 without a
meeting, the trial three-quarters used, the trial ended. Each at most once per congregation (table trial_emails).

Off unless HC_TRIAL_EMAILS=1 (send through Resend) or HC_TRIAL_EMAILS=dry (log what would be sent, record nothing).
Only congregations with the free trial made at or after the cutoff, which is stored (hub_settings) the first time the
hub runs with HC_TRIAL_EMAILS=1; before that, dry runs count from the hub's start. Checked every CHECK_EVERY_S by
the hub, right after a sign-up is verified and after each meeting ends. Operator view: GET /api/admin/trial-emails.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from . import api
from .db import BUNDLES, TRIAL_DAYS, TRIAL_SECONDS

log = logging.getLogger("hear")

KINDS = ("welcome", "day3", "trial75", "ended")
CHECK_EVERY_S = 15 * 60
DAY = 86400
REPLY_TO = "maddie@hearchurch.com"
CUTOFF_KEY = "trial_emails_cutoff"
STARTED = time.time()            # dry runs before the first real enable count congregations from here
_dry_logged: set[tuple[int, str]] = set()   # dry run: log each would-be e-mail once per process


def mode() -> str:
    v = os.environ.get("HC_TRIAL_EMAILS", "").strip().lower()
    return "on" if v == "1" else "dry" if v == "dry" else "off"


def cutoff(db, store: bool = False) -> float:
    v = db.setting(CUTOFF_KEY, repr(time.time()) if store else None)
    return float(v) if v is not None else STARTED


def due(db, cong: dict[str, Any], now: float) -> tuple[list[str], dict[str, Any] | None]:
    """The e-mails this congregation should get now and hasn't had, and its trial (db.balance's)."""
    t = db.balance(cong["id"], now)["trial"]
    if t is None or not cong["email_verified"] or not cong["email"]:
        return [], t
    sent = db.trial_emails_sent(cong["id"])
    out = ["welcome"]
    if t["started_at"] is None:
        if now >= cong["created_at"] + 3 * DAY:
            out.append("day3")
    elif not t["active"]:
        out.append("ended")
    elif now >= t["started_at"] + 0.75 * TRIAL_DAYS * DAY or t["used_min"] * 60 >= 0.75 * TRIAL_SECONDS:
        out.append("trial75")
    return [k for k in out if k not in sent], t


def _n(x: float, unit: str) -> str:
    n = max(1, round(x))
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _bundles() -> str:
    parts = [f"${c // 100} for {h}" for c, h in BUNDLES]
    parts[0] += " hours"
    return ", ".join(parts[:-1]) + " or " + parts[-1]


def message(kind: str, cong: dict[str, Any], t: dict[str, Any]) -> tuple[str, str]:
    """(subject, plain text) for one e-mail."""
    church = api.display_name(cong)
    days, hours = TRIAL_DAYS, round(TRIAL_SECONDS / 3600)
    test = ("connect the cable from the sound board, press Start meeting, scan the QR code with your own phone, and "
            "have someone speak at the pulpit.")
    after = (f"Every month you get one free language-hour, renewing on the 1st. For more, buy prepaid hours: "
             f"{_bundles()}. They never expire, unused hours are refundable, and there's no monthly plan or contract. "
             "A meeting that's under way is never cut off.\n\nYou can buy hours any time at https://hearchurch.com/account")
    subject, body = {
        "welcome": ("Welcome to Hear Church",
                    f"Thanks for signing up {church}. Everything you need to get ready is here: "
                    "https://hearchurch.com/setup\n\n"
                    f"Before Sunday, do the 5-minute test: {test}\n\n"
                    f"Your free trial is {days} days, up to {hours} language-hours, whichever comes first. The {days} "
                    "days start with your first translated meeting, and a test with a phone listening counts, so do "
                    "it in the week you plan to start. No card is needed.\n\n"
                    "Questions? Just reply to this e-mail."),
        "day3": ("Need a hand setting up before Sunday?",
                 f"{church} signed up a few days ago and hasn't had a translated meeting yet. Need a hand setting up "
                 f"before Sunday?\n\nYour {days} free days haven't started: they begin with your first translated "
                 "meeting, so nothing is wasted.\n\n"
                 f"The 5-minute test: {test} The steps are here: https://hearchurch.com/setup\n\n"
                 "Stuck anywhere? Reply and I'll help, or we can do a 10-minute call."),
        "trial75": ("Your free trial is three-quarters used",
                    f"A quick heads-up: the free trial for {church} has about {_n(t['days_left'], 'day')} and "
                    f"{_n(t['hours_left'], 'language-hour')} left, whichever runs out first.\n\n"
                    f"After that, nothing is charged; we never took a card. {after}\n\n"
                    "How has it gone for your members? I'd love to hear, and to fix anything that got in the way."),
        "ended": ("Your free trial has ended",
                  f"The free trial for {church} has ended. Thank you for trying Hear Church.\n\n"
                  f"Nothing is charged, and you can keep going. {after}\n\n"
                  "If anything got in the way, reply and tell me. I read every answer."),
    }[kind]
    return subject, f"Hello,\n\n{body}\n\nMaddie\nHear Church\n"


async def check(cid: int | None = None, now: float | None = None) -> list[tuple[int, str]]:
    """Send (or, dry, log) what is due, for every congregation in scope or just `cid`. Returns [(id, kind)] sent or
    logged. Nothing when off, or when on without Resend configured."""
    m = mode()
    if m == "off":
        return []
    h = api._hear()
    if m == "on" and not h.resend.configured():
        return []
    now = time.time() if now is None else now
    done = []
    for cong in h.db.trial_email_congregations(cutoff(h.db, store=m == "on"), cid):
        kinds, t = due(h.db, cong, now)
        for kind in kinds:
            subject, text = message(kind, cong, t)
            if m == "dry":
                if (cong["id"], kind) not in _dry_logged:
                    _dry_logged.add((cong["id"], kind))
                    log.info("trial e-mail (dry run) %s to %s: %s", kind, cong["slug"], subject)
                    done.append((cong["id"], kind))
                continue
            if not h.db.claim_trial_email(cong["id"], kind):
                continue
            # shortcut: a failing address is retried every check (a log line each time); add a give-up if that shows
            if await h.resend.send(cong["email"], subject, text, reply_to=REPLY_TO):
                log.info("trial e-mail %s sent to %s", kind, cong["slug"])
                done.append((cong["id"], kind))
            else:
                h.db.unclaim_trial_email(cong["id"], kind)
    return done


def soon(cid: int) -> None:
    """After a sign-up or a meeting: check this congregation in the background (when on and in an event loop)."""
    if mode() == "off":
        return
    try:
        asyncio.get_running_loop().create_task(_safe(check(cid)))
    except RuntimeError:
        pass


async def _safe(coro) -> None:
    try:
        await coro
    except Exception:
        log.exception("trial e-mails")


async def run_forever() -> None:
    while True:
        await _safe(check())
        await asyncio.sleep(CHECK_EVERY_S)


def overview(db, now: float | None = None) -> dict[str, Any]:
    """For the operator: the mode, the cutoff, and per congregation in scope what was sent and what is due."""
    now = time.time() if now is None else now
    m = mode()
    stored = db.setting(CUTOFF_KEY)
    rows = []
    for c in db.trial_email_congregations(cutoff(db)):
        kinds, _ = due(db, c, now)
        rows.append({"id": c["id"], "slug": c["slug"], "name": api.display_name(c), "email": c["email"],
                     "created_at": c["created_at"], "sent": db.trial_emails_sent(c["id"]), "due": kinds})
    return {"mode": m, "cutoff": float(stored) if stored is not None else None, "counting_from": cutoff(db),
            "resend_configured": api._hear().resend.configured(), "congregations": rows}
