"""Read inbound mail from an SMTP account's mailbox over IMAP.

SMTP is send-only, so the reply/bounce sweeps (which for Gmail talk to the Gmail
API) had no way to see what landed in an Outlook/Office365/custom-host mailbox.
This module gives them one: a small synchronous IMAP reader that fetches recent
messages and flattens each into the same shape the sweeps already consume.

Synchronous (``imaplib`` is blocking) — async callers invoke it via
``asyncio.to_thread`` exactly like ``smtp_sender``/``gmail_sender``.
"""

import email
import imaplib
import logging
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from typing import Optional

from app.services import crypto

logger = logging.getLogger(__name__)

# Cap how much of a mailbox one sweep pulls, so a huge inbox can't wedge a worker.
_MAX_MESSAGES = 200
_IMAP_TIMEOUT = 20


def derive_imap_host(account) -> str:
    """Best-effort IMAP host for an SMTP account.

    Uses an explicit ``imap_host`` when configured, otherwise derives it from the
    SMTP host: ``smtp.example.com`` → ``imap.example.com`` (the near-universal
    convention), falling back to the SMTP host unchanged.
    """
    explicit = getattr(account, "imap_host", None)
    if explicit:
        return explicit
    host = account.host or ""
    if host.startswith("smtp."):
        return "imap." + host[len("smtp."):]
    return host


def _decode(value: Optional[str]) -> str:
    """Decode an RFC 2047 encoded header to a plain string."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # pragma: no cover - defensive
        return value


def _extract_text(msg: Message) -> str:
    """Flatten an email message into searchable plain text.

    Prefers text/plain parts; falls back to stripped text/html. Also decodes any
    ``message/delivery-status`` / ``message/rfc822`` parts so DSN bodies (bounces)
    are parseable by ``bounce_ingestion``.
    """
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "message/delivery-status", "text/rfc822-headers"):
                chunks.append(_part_text(part))
            elif ctype == "text/html" and not chunks:
                html = _part_text(part)
                if html:
                    try:
                        from bs4 import BeautifulSoup

                        chunks.append(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
                    except Exception:  # pragma: no cover - defensive
                        chunks.append(html)
    else:
        chunks.append(_part_text(msg))
    return "\n".join(c for c in chunks if c)


def _part_text(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:  # pragma: no cover - defensive
        return ""


class SmtpMailboxReader:
    """Polls one SMTP account's IMAP mailbox for recent inbound messages."""

    def __init__(self, account):
        self.account = account

    def _connect(self) -> imaplib.IMAP4:
        host = derive_imap_host(self.account)
        port = getattr(self.account, "imap_port", None) or 993
        use_ssl = getattr(self.account, "imap_use_ssl", True)
        password = crypto.decrypt(self.account.password_encrypted)
        if use_ssl:
            conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(host, port, timeout=_IMAP_TIMEOUT)
        else:
            conn = imaplib.IMAP4(host, port, timeout=_IMAP_TIMEOUT)
            try:
                conn.starttls()
            except Exception:  # pragma: no cover - some hosts are plaintext
                pass
        conn.login(self.account.username, password)
        return conn

    def fetch_recent(self, days: int = 2, mailbox: str = "INBOX") -> list[dict]:
        """Fetch messages received in the last ``days`` days from ``mailbox``.

        Returns a list of dicts ``{message_id, from_email, from_raw, subject,
        text}`` newest-last. Never raises — a mailbox we can't reach yields an
        empty list, matching how the Gmail sweeps degrade.
        """
        try:
            conn = self._connect()
        except Exception as exc:
            logger.warning("IMAP connect failed for %s: %s", getattr(self.account, "email", "?"), exc)
            return []

        out: list[dict] = []
        try:
            conn.select(mailbox, readonly=True)
            since = (datetime.now(timezone.utc) - timedelta(days=max(days, 1))).strftime("%d-%b-%Y")
            typ, data = conn.search(None, "SINCE", since)
            if typ != "OK" or not data or not data[0]:
                return []
            ids = data[0].split()[-_MAX_MESSAGES:]
            for msg_id in ids:
                try:
                    typ, msg_data = conn.fetch(msg_id, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    if not isinstance(raw, (bytes, bytearray)):
                        continue
                    msg = email.message_from_bytes(raw)
                    from_raw = _decode(msg.get("From", ""))
                    from_email = parseaddr(from_raw)[1].strip().lower()
                    message_id = (msg.get("Message-ID") or "").strip()
                    out.append({
                        "message_id": message_id,
                        "from_email": from_email,
                        "from_raw": from_raw,
                        "subject": _decode(msg.get("Subject", "")),
                        "text": _extract_text(msg),
                    })
                except Exception as exc:  # pragma: no cover - per-message resilience
                    logger.debug("IMAP fetch failed for %s: %s", msg_id, exc)
        except Exception as exc:
            logger.warning("IMAP sweep failed for %s: %s", getattr(self.account, "email", "?"), exc)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return out
