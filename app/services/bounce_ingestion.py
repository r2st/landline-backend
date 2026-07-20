"""Ingest bounce notifications from the connected Gmail mailboxes.

``bounce_handler`` has always been able to classify and suppress a bounce, but
nothing ever called it: its only caller was a manual API endpoint on the
deliverability router. Meanwhile Gmail is the primary transport, so real bounces
arrive as *messages in the mailbox* — Delivery Status Notifications from
mailer-daemon — and the reply sweep drops them on the floor, because
``reply_tracker._match_reply_to_send`` matches on the sender and a DSN's sender
is a postmaster nobody has a Contact row for.

Net effect before this module: a dead address kept receiving the whole sequence,
``Contact.last_bounce_type`` was never set by anything automatic, and the bounce
handler's careful hard/soft logic ran only when a human hand-posted to an
endpoint.

This sweep closes that loop:

    find DSNs → parse recipient + status + diagnostic → record_bounce → suppress

Parsing prefers the RFC 3464 machine-readable part (``Final-Recipient``,
``Status``, ``Diagnostic-Code``) and falls back to scanning the text for an
address plus an SMTP status. Classification itself is delegated to
``bounce_handler.classify_bounce`` — this module decides *what bounced*, never
*how bad it is*.

Every notification is ledgered in ``BounceRecord`` before ``record_bounce``
runs, because ``record_bounce`` is not idempotent: it increments
``Contact.bounce_count``. Re-processing the same DSN on the next sweep would
inflate that counter and eventually suppress a perfectly live contact for no
reason other than how often we poll.
"""

import asyncio
import hashlib
import logging
import re
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.bounce_record import BounceRecord
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.services.bounce_handler import classify_bounce, record_bounce
from app.services.smtp_reader import SmtpMailboxReader

logger = logging.getLogger(__name__)

# Gmail search selecting delivery-failure notifications. Deliberately broad —
# a missed bounce keeps mailing a dead address, while a false positive costs
# only a parse that finds no recipient and is discarded.
BOUNCE_QUERY = (
    "newer_than:{days}d ("
    "from:mailer-daemon OR from:postmaster OR "
    'subject:"Delivery Status Notification" OR '
    'subject:"Undelivered Mail Returned to Sender" OR '
    'subject:"Delivery incomplete" OR '
    'subject:"Address not found"'
    ")"
)

# RFC 3464 machine-readable DSN fields.
_FINAL_RECIPIENT_RE = re.compile(
    r"^Final-Recipient:\s*(?:rfc822;)?\s*([^\s;]+@[^\s;]+)", re.IGNORECASE | re.MULTILINE
)
_ORIGINAL_RECIPIENT_RE = re.compile(
    r"^Original-Recipient:\s*(?:rfc822;)?\s*([^\s;]+@[^\s;]+)", re.IGNORECASE | re.MULTILINE
)
_STATUS_RE = re.compile(r"^Status:\s*([245]\.\d{1,3}\.\d{1,3})", re.IGNORECASE | re.MULTILINE)
_DIAGNOSTIC_RE = re.compile(
    r"^Diagnostic-Code:\s*(?:smtp;)?\s*(.+)$", re.IGNORECASE | re.MULTILINE
)

# Fallbacks for providers that don't emit a conformant DSN part.
_LOOSE_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_LOOSE_SMTP_CODE_RE = re.compile(r"\b([45]\d{2})[ -]")
_LOOSE_ENHANCED_RE = re.compile(r"\b([245]\.\d{1,3}\.\d{1,3})\b")

# Addresses that are never a real bounced recipient — matching one of these
# would suppress our own sender or the postmaster rather than the dead lead.
_IGNORED_LOCALPARTS = ("mailer-daemon", "postmaster", "noreply", "no-reply")

# IMAP has no server-side equivalent of Gmail's rich search, so the SMTP sweep
# fetches recent mail and classifies "is this a bounce?" client-side against the
# same senders/subjects the Gmail BOUNCE_QUERY targets.
_BOUNCE_SENDER_MARKERS = ("mailer-daemon", "postmaster")
_BOUNCE_SUBJECT_MARKERS = (
    "delivery status notification",
    "undelivered mail returned to sender",
    "delivery incomplete",
    "address not found",
    "undeliverable",
    "failure notice",
    "returned mail",
)


def _looks_like_bounce(from_email: str, subject: str) -> bool:
    """Cheap client-side filter mirroring the Gmail ``BOUNCE_QUERY``."""
    sender = (from_email or "").lower()
    subj = (subject or "").lower()
    if any(marker in sender for marker in _BOUNCE_SENDER_MARKERS):
        return True
    return any(marker in subj for marker in _BOUNCE_SUBJECT_MARKERS)


def _is_plausible_recipient(email: str) -> bool:
    local = email.split("@")[0].lower()
    return not any(local.startswith(p) for p in _IGNORED_LOCALPARTS)


def parse_bounce(raw_text: str) -> Optional[dict]:
    """Extract ``{email, status_code, diagnostic, kind}`` from a DSN body.

    Returns None when no plausible bounced recipient can be identified — the
    query is intentionally broad, so "this isn't actually a bounce" is a normal
    and expected outcome, not an error.
    """
    if not raw_text:
        return None

    email = None
    for pattern in (_FINAL_RECIPIENT_RE, _ORIGINAL_RECIPIENT_RE):
        match = pattern.search(raw_text)
        if match and _is_plausible_recipient(match.group(1)):
            email = match.group(1).strip().lower().strip("<>")
            break

    status = None
    status_match = _STATUS_RE.search(raw_text)
    if status_match:
        status = status_match.group(1)

    diagnostic = ""
    diag_match = _DIAGNOSTIC_RE.search(raw_text)
    if diag_match:
        diagnostic = diag_match.group(1).strip()

    # Fallback: no conformant DSN part. Find the first plausible address and any
    # SMTP status in the text.
    if email is None:
        for candidate in _LOOSE_EMAIL_RE.findall(raw_text):
            cand = candidate.strip().lower()
            if _is_plausible_recipient(cand):
                email = cand
                break
    if email is None:
        return None

    if status is None:
        enhanced = _LOOSE_ENHANCED_RE.search(raw_text)
        if enhanced:
            status = enhanced.group(1)
        else:
            code = _LOOSE_SMTP_CODE_RE.search(raw_text)
            if code:
                # Map a bare SMTP code onto the enhanced form bounce_handler
                # understands: 5xx → permanent, 4xx → transient.
                status = f"{code.group(1)[0]}.0.0"

    if not diagnostic:
        diagnostic = raw_text[:500]

    kind = classify_bounce(status_code=status, diagnostic=diagnostic)
    return {"email": email, "status_code": status, "diagnostic": diagnostic[:2000], "kind": kind}


class BounceIngestionService:
    """Sweeps connected mailboxes for DSNs and routes them to the bounce handler."""

    def _build_credentials(self, account: GmailAccount) -> Credentials:
        """Build OAuth credentials from the account's encrypted tokens.

        The tokens live in ``*_encrypted`` columns — there is no plaintext
        ``access_token``/``refresh_token`` attribute on the model. Reading those
        names raised AttributeError on every sweep, which the caller caught,
        logged and turned into "0 bounces", so this never ingested anything.

        A stale access token is survivable (the refresh token mints a new one),
        so it degrades to None rather than failing the sweep — matching
        ``email_history._build_credentials``.
        """
        from app.services.crypto import decrypt

        settings = get_settings()
        access_token = None
        if account.access_token_encrypted:
            try:
                access_token = decrypt(account.access_token_encrypted)
            except Exception:
                access_token = None
        return Credentials(
            token=access_token,
            refresh_token=decrypt(account.refresh_token_encrypted),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
        )

    @staticmethod
    def _message_text(msg: dict) -> str:
        """Flatten a Gmail message into searchable text.

        Uses the snippet plus every decoded text part, so a DSN is parsed the
        same whether the provider puts the detail in message/delivery-status,
        text/plain, or only the snippet.
        """
        import base64

        chunks: list[str] = []
        snippet = msg.get("snippet")
        if snippet:
            chunks.append(snippet)

        def _walk(part: dict) -> None:
            body = part.get("body", {}) or {}
            data = body.get("data")
            if data:
                try:
                    chunks.append(
                        base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", "replace")
                    )
                except Exception:  # pragma: no cover - defensive
                    pass
            for sub in part.get("parts", []) or []:
                _walk(sub)

        _walk(msg.get("payload", {}) or {})
        return "\n".join(chunks)

    async def _already_processed(self, db: AsyncSession, gmail_message_id: str) -> bool:
        return (
            await db.execute(
                select(BounceRecord.id).where(
                    BounceRecord.gmail_message_id == gmail_message_id
                )
            )
        ).scalar_one_or_none() is not None

    async def ingest_account(
        self, db: AsyncSession, account: GmailAccount, lookback_days: int = 3
    ) -> list[dict]:
        """Scan one mailbox for bounce notifications and record them."""
        processed: list[dict] = []
        try:
            creds = self._build_credentials(account)
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            results = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    q=BOUNCE_QUERY.format(days=lookback_days),
                    maxResults=100,
                )
                .execute()
            )
        except Exception as exc:
            logger.error("Bounce scan failed for %s: %s", account.email, exc)
            return processed

        for msg_info in results.get("messages", []) or []:
            msg_id = msg_info.get("id")
            if not msg_id:
                continue
            try:
                if await self._already_processed(db, msg_id):
                    continue

                msg = (
                    service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="full")
                    .execute()
                )
                parsed = parse_bounce(self._message_text(msg))
                if not parsed:
                    logger.debug("Message %s matched the bounce query but parsed no recipient", msg_id)
                    continue

                # Ledger BEFORE handling: record_bounce increments a counter, so
                # the dedup key must exist even if the handler then fails.
                record = BounceRecord(
                    gmail_message_id=msg_id,
                    gmail_account_id=account.id,
                    recipient_email=parsed["email"],
                    bounce_kind=parsed["kind"],
                    status_code=parsed["status_code"],
                    diagnostic=parsed["diagnostic"],
                    result={},
                )
                db.add(record)
                await db.flush()

                result = await record_bounce(
                    db,
                    parsed["email"],
                    status_code=parsed["status_code"],
                    diagnostic=parsed["diagnostic"],
                    kind=parsed["kind"],
                )
                record.result = result
                await db.commit()

                processed.append({"gmail_message_id": msg_id, **parsed, "result": result})
                logger.info(
                    "Ingested %s bounce for %s (suppressed=%s)",
                    parsed["kind"], parsed["email"], result.get("suppressed"),
                )
            except Exception as exc:
                await db.rollback()
                logger.warning("Failed to ingest bounce %s: %s", msg_id, exc)

        return processed

    async def ingest_smtp_account(
        self, db: AsyncSession, account: SmtpAccount, lookback_days: int = 3
    ) -> list[dict]:
        """Scan one SMTP mailbox (over IMAP) for bounce notifications.

        Mirrors ``ingest_account`` but reads via IMAP and dedups on a synthetic
        key (``smtp:<account>:<message-id>``) since there is no Gmail message id.
        ``BounceRecord.gmail_account_id`` is left NULL — the FK targets
        ``gmail_accounts`` and this bounce came from an SMTP mailbox.
        """
        processed: list[dict] = []
        reader = SmtpMailboxReader(account)
        try:
            messages = await asyncio.to_thread(reader.fetch_recent, lookback_days)
        except Exception as exc:
            logger.error("SMTP bounce scan failed for %s: %s", account.email, exc)
            return processed

        for msg in messages:
            if not _looks_like_bounce(msg.get("from_email", ""), msg.get("subject", "")):
                continue
            text = msg.get("text") or ""
            # Stable dedup key: prefer the RFC Message-ID, else a hash of the body.
            raw_id = msg.get("message_id") or hashlib.sha1(
                (msg.get("from_email", "") + msg.get("subject", "") + text[:500]).encode(
                    "utf-8", "replace"
                )
            ).hexdigest()
            dedup_key = f"smtp:{account.id}:{raw_id}"[:255]
            try:
                if await self._already_processed(db, dedup_key):
                    continue
                parsed = parse_bounce(text)
                if not parsed:
                    logger.debug("SMTP message %s matched bounce filter but parsed no recipient", dedup_key)
                    continue

                record = BounceRecord(
                    gmail_message_id=dedup_key,
                    gmail_account_id=None,  # sourced from an SMTP mailbox
                    recipient_email=parsed["email"],
                    bounce_kind=parsed["kind"],
                    status_code=parsed["status_code"],
                    diagnostic=parsed["diagnostic"],
                    result={},
                )
                db.add(record)
                await db.flush()

                result = await record_bounce(
                    db,
                    parsed["email"],
                    status_code=parsed["status_code"],
                    diagnostic=parsed["diagnostic"],
                    kind=parsed["kind"],
                )
                record.result = result
                await db.commit()

                processed.append({"gmail_message_id": dedup_key, **parsed, "result": result})
                logger.info(
                    "Ingested %s SMTP bounce for %s (suppressed=%s)",
                    parsed["kind"], parsed["email"], result.get("suppressed"),
                )
            except Exception as exc:
                await db.rollback()
                logger.warning("Failed to ingest SMTP bounce %s: %s", dedup_key, exc)

        return processed

    async def ingest_all_accounts(self, db: AsyncSession, lookback_days: int = 3) -> dict:
        """Scan every active connected mailbox (Gmail API + SMTP over IMAP)."""
        accounts = (
            await db.execute(select(GmailAccount).where(GmailAccount.is_active == True))  # noqa: E712
        ).scalars().all()

        total: list[dict] = []
        for account in accounts:
            total.extend(await self.ingest_account(db, account, lookback_days))

        # SMTP mailboxes — same bounce-closing loop, read over IMAP.
        smtp_accounts = (
            await db.execute(
                select(SmtpAccount).where(
                    SmtpAccount.status == "connected",
                    SmtpAccount.imap_enabled == True,  # noqa: E712
                )
            )
        ).scalars().all()
        for account in smtp_accounts:
            total.extend(await self.ingest_smtp_account(db, account, lookback_days))

        hard = sum(1 for b in total if b.get("kind") == "hard")
        return {
            "accounts_scanned": len(accounts) + len(smtp_accounts),
            "bounces_ingested": len(total),
            "hard": hard,
            "soft": len(total) - hard,
        }


_service: Optional[BounceIngestionService] = None


def get_bounce_ingestion_service() -> BounceIngestionService:
    global _service
    if _service is None:
        _service = BounceIngestionService()
    return _service
