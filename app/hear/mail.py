"""E-mail out, and per-IP rate limits for the sign-up endpoints.

Mailer (the older magic links): SMTP when a host is configured (settings [hearchurch] or HEARCHURCH_SMTP_*
variables); otherwise each message is appended to a log file (logs/outbox.log), which is how it is tested.
Resend (device codes, "Email me a code"): Resend's HTTP API with RESEND_API_KEY, from
"Hear Church <no-reply@hearchurch.com>". Without the key the routes that need it answer 503 not_configured.
"""
from __future__ import annotations

import logging
import os
import smtplib
import time
from collections import defaultdict
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger("hear.mail")


class Mailer:
    def __init__(self, outbox: Path, host: str = "", port: int = 587, user: str = "", password: str = "",
                 sender: str = "", starttls: bool = True):
        self.outbox = outbox
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sender = sender or user or "Hear Church <noreply@localhost>"
        self.starttls = starttls

    @classmethod
    def from_settings(cls, cfg, outbox: Path) -> "Mailer":
        """Settings first, then HEARCHURCH_SMTP_HOST/PORT/USER/PASSWORD/FROM (keeps the password out of the repo)."""
        env = os.environ.get
        return cls(outbox, cfg.smtp_host or env("HEARCHURCH_SMTP_HOST", ""),
                   cfg.smtp_port or int(env("HEARCHURCH_SMTP_PORT", "587")),
                   cfg.smtp_user or env("HEARCHURCH_SMTP_USER", ""), env("HEARCHURCH_SMTP_PASSWORD", ""),
                   cfg.smtp_from or env("HEARCHURCH_SMTP_FROM", ""), cfg.smtp_starttls)

    def send(self, to: str, subject: str, body: str) -> None:
        if self.host:
            msg = EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
            msg.set_content(body)
            try:
                with smtplib.SMTP(self.host, self.port, timeout=15) as s:
                    if self.starttls:
                        s.starttls()
                    if self.user:
                        s.login(self.user, self.password)
                    s.send_message(msg)
                return
            except Exception as e:  # never lose the link: fall back to the log
                log.warning("SMTP send to %s failed (%s): writing to the outbox log", to, e)
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        with open(self.outbox, "a", encoding="utf-8") as f:
            f.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')}\nTo: {to}\nSubject: {subject}\n\n{body}\n\n")


RESEND_URL = "https://api.resend.com/emails"
RESEND_FROM = "Hear Church <no-reply@hearchurch.com>"


def resend_key() -> str:
    """RESEND_API_KEY from the environment (on the hub: from ~/.config/hearchurch/resend.env via the service's
    environment file); "" when not set."""
    return os.environ.get("RESEND_API_KEY", "").strip()


class Resend:
    """Sends the account e-mails (device codes) through Resend's HTTP API, from no-reply@hearchurch.com. Never logs
    the key, the code or the message body."""

    def __init__(self, key: str | None = None, sender: str = RESEND_FROM):
        self._key, self.sender = key, sender

    @property
    def key(self) -> str:
        return self._key if self._key is not None else resend_key()

    def configured(self) -> bool:
        return bool(self.key)

    async def send(self, to: str, subject: str, text: str) -> bool:
        """One message to one address. True when Resend took it; failures are logged (status only) and False."""
        import httpx

        key = self.key
        if not key:
            return False
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                r = await http.post(RESEND_URL, headers={"Authorization": f"Bearer {key}"},
                                    json={"from": self.sender, "to": [to], "subject": subject, "text": text})
        except Exception as e:
            log.warning("resend: no answer (%s)", type(e).__name__)
            return False
        if r.status_code >= 300:
            log.warning("resend: HTTP %d", r.status_code)
            return False
        return True


class RateLimiter:
    """At most `limit` hits per `window_s` seconds for each key (an IP, an address)."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window_s = limit, window_s
        self.hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        now = time.time()
        recent = [t for t in self.hits[key] if now - t < self.window_s]
        ok = len(recent) < self.limit
        if ok:
            recent.append(now)
        if recent:
            self.hits[key] = recent
        else:
            self.hits.pop(key, None)
        if len(self.hits) > 20000:  # forget idle keys
            for k in [k for k, v in self.hits.items() if now - v[-1] >= self.window_s]:
                del self.hits[k]
        return ok
