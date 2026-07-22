"""Ingest complaint / feedback-loop (FBL) reports from connected mailboxes.

Gmail is the primary transport and exposes **no** feedback loop — the only
complaint signal it gives is Google Postmaster's *aggregate* spam-rate, which
never names the person who complained. So a recipient who hits "report spam"
keeps getting the whole sequence, and we only ever see a rising spam-rate we
can't act on per-recipient.

Providers that *do* run an FBL (Yahoo, Outlook/Hotmail, AOL, Comcast, La Poste,
…) send an **ARF report** (RFC 5965, ``multipart/report; report-type=
feedback-report``) to the feedback address for the sending domain. When that
address forwards to a monitored mailbox, the report simply lands as a message —
exactly like a bounce DSN. This sweep reads those reports:

    find ARF/FBL report → parse the complained-about recipient → record_complaint

Parsing prefers the machine-readable ``message/feedback-report`` part
(``Feedback-Type``, ``Original-Rcpt-To``) and falls back to the embedded
original message's ``To:`` header. Suppression itself is delegated to
``complaint_handler.record_complaint`` — a complaint is a hard, compliance-grade
do-not-contact, handled the same way as a hard bounce.

Every report is ledgered in ``ComplaintRecord`` (unique ``gmail_message_id``)
before suppression runs, so a repeated sweep over the rolling lookback window is
a no-op instead of re-asserting suppression on every pass.
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
from app.models.complaint_record import ComplaintRecord
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.services.complaint_handler import record_complaint
from app.services.smtp_reader import SmtpMailboxReader

logger = logging.getLogger(__name__)

# Gmail search selecting feedback-loop / abuse reports. Deliberately broad — a
# missed complaint keeps mailing someone who reported us as spam, while a false
# positive costs only a parse that finds no recipient and is discarded.
COMPLAINT_QUERY = (
    "newer_than:{days}d ("
    "from:feedback-loop OR from:fbl OR from:scomp OR from:abuse OR from:complaints OR "
    '"Feedback-Type: abuse" OR "feedback-type:abuse" OR '
    '"This is an email abuse report" OR '
    '"This is a Complaint Feedback Loop" OR '
    'subject:"Email Feedback Report" OR '
    'subject:"Abuse Report" OR '
    'subject:"complaint" OR '
    'subject:"spam report" OR '
    'subject:"FBL"'
    ")"
)

# RFC 5965 ARF machine-readable fields.
_FEEDBACK_TYPE_RE = re.compile(r"^Feedback-Type:\s*([\w-]+)", re.IGNORECASE | re.MULTILINE)
_ORIGINAL_RCPT_RE = re.compile(
    r"^Original-Rcpt-To:\s*(?:rfc822;)?\s*([^\s;<>]+@[^\s;<>]+)", re.IGNORECASE | re.MULTILINE
)
_REMOVAL_RCPT_RE = re.compile(
    r"^Removal-Recipient:\s*(?:rfc822;)?\s*([^\s;<>]+@[^\s;<>]+)", re.IGNORECASE | re.MULTILINE
)
# The embedded original message names the complainer in its own To: header.
_ORIGINAL_TO_RE = re.compile(
    r"^To:\s*.*?([\w.+-]+@[\w-]+\.[\w.-]+)", re.IGNORECASE | re.MULTILINE
)
_SOURCE_RE = re.compile(r"^(?:Reporting-MTA|Source):\s*(.+)$", re.IGNORECASE | re.MULTILINE)

# Known ARF feedback types (RFC 5965). Anything else is normalised to 'other'.
_KNOWN_FEEDBACK_TYPES = ("abuse", "fraud", "virus", "not-spam", "auth-failure", "other")

# Addresses that are never the real complainer — the reporting infrastructure or
# our own sending identity. Matching one of these would suppress the wrong party.
_IGNORED_LOCALPARTS = (
    "mailer-daemon", "postmaster", "noreply", "no-reply", "feedback-loop",
    "fbl", "scomp", "abuse", "complaints", "staff",
)

# Client-side "is this a complaint?" filter for the IMAP path (no server-side
# rich search), mirroring the Gmail COMPLAINT_QUERY.
_COMPLAINT_SENDER_MARKERS = ("feedback-loop", "fbl", "scomp", "abuse", "complaints")
_COMPLAINT_SUBJECT_MARKERS = (
    "email feedback report",
    "abuse report",
    "complaint",
    "spam report",
    "feedback loop",
    "fbl",
)
_COMPLAINT_BODY_MARKERS = ("feedback-type", "this is an email abuse report")


def is_complaint_message(from_email: str, subject: str, body: str = "") -> bool:
    """Whether an inbound message is an ARF/FBL complaint report."""
    sender = (from_email or "").lower()
    subj = (subject or "").lower()
    text = (body or "").lower()
    if any(marker in sender for marker in _COMPLAINT_SENDER_MARKERS):
        return True
    if any(marker in subj for marker in _COMPLAINT_SUBJECT_MARKERS):
        return True
    return any(marker in text for marker in _COMPLAINT_BODY_MARKERS)


def _is_plausible_recipient(email: str) -> bool:
    local = email.split("@")[0].lower()
    return not any(local.startswith(p) for p in _IGNORED_LOCALPARTS)


def _normalize_feedback_type(raw: Optional[str]) -> str:
    ft = (raw or "abuse").strip().lower()
    return ft if ft in _KNOWN_FEEDBACK_TYPES else "other"


def parse_complaint(raw_text: str) -> Optional[dict]:
    """Extract ``{email, feedback_type, source, diagnostic}`` from an ARF report.

    Returns None when no plausible complained-about recipient can be identified —
    the query is intentionally broad, so "this isn't actually a complaint" is a
    normal, expected outcome rather than an error.
    """
    if not raw_text:
        return None

    email = None
    for pattern in (_ORIGINAL_RCPT_RE, _REMOVAL_RCPT_RE):
        match = pattern.search(raw_text)
        if match and _is_plausible_recipient(match.group(1)):
            email = match.group(1).strip().lower().strip("<>")
            break

    # Fallback: the embedded original message's To: header (the complainer).
    if email is None:
        for match in _ORIGINAL_TO_RE.finditer(raw_text):
            cand = match.group(1).strip().lower().strip("<>")
            if _is_plausible_recipient(cand):
                email = cand
                break

    if email is None:
        return None

    ft_match = _FEEDBACK_TYPE_RE.search(raw_text)
    feedback_type = _normalize_feedback_type(ft_match.group(1) if ft_match else None)

    source = None
    src_match = _SOURCE_RE.search(raw_text)
    if src_match:
        source = src_match.group(1).strip()[:255]

    return {
        "email": email,
        "feedback_type": feedback_type,
        "source": source,
        "diagnostic": raw_text[:2000],
    }


class ComplaintIngestionService:
    """Sweeps connected mailboxes for FBL reports and suppresses complainers."""

    def _build_credentials(self, account: GmailAccount) -> Credentials:
        """Build OAuth credentials from the account's encrypted tokens.

        Mirrors ``bounce_ingestion._build_credentials`` — a stale access token is
        survivable (the refresh token mints a new one), so it degrades to None
        rather than failing the sweep.
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
        """Flatten a Gmail message (snippet + every decoded part) into text."""
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
                select(ComplaintRecord.id).where(
                    ComplaintRecord.gmail_message_id == gmail_message_id
                )
            )
        ).scalar_one_or_none() is not None

    async def _gexec(self, request):
        """Run a blocking googleapiclient request off the event loop."""
        return await asyncio.to_thread(request.execute)

    async def ingest_account(
        self, db: AsyncSession, account: GmailAccount, lookback_days: int = 3
    ) -> list[dict]:
        """Scan one Gmail mailbox for FBL reports and suppress complainers."""
        processed: list[dict] = []
        try:
            creds = self._build_credentials(account)
            service = await asyncio.to_thread(
                build, "gmail", "v1", credentials=creds, cache_discovery=False
            )
            results = await self._gexec(
                service.users().messages().list(
                    userId="me",
                    q=COMPLAINT_QUERY.format(days=lookback_days),
                    maxResults=100,
                )
            )
        except Exception as exc:
            logger.error("Complaint scan failed for %s: %s", account.email, exc)
            return processed

        for msg_info in results.get("messages", []) or []:
            msg_id = msg_info.get("id")
            if not msg_id:
                continue
            try:
                if await self._already_processed(db, msg_id):
                    continue

                msg = await self._gexec(
                    service.users().messages().get(userId="me", id=msg_id, format="full")
                )
                parsed = parse_complaint(self._message_text(msg))
                if not parsed:
                    logger.debug("Message %s matched the complaint query but parsed no recipient", msg_id)
                    continue

                # Ledger BEFORE handling so the dedup key exists even if
                # suppression then fails and the row is rolled back-and-retried.
                record = ComplaintRecord(
                    gmail_message_id=msg_id,
                    gmail_account_id=account.id,
                    recipient_email=parsed["email"],
                    feedback_type=parsed["feedback_type"],
                    source=parsed["source"],
                    diagnostic=parsed["diagnostic"],
                    result={},
                )
                db.add(record)
                await db.flush()

                result = await record_complaint(
                    db,
                    parsed["email"],
                    feedback_type=parsed["feedback_type"],
                    diagnostic=parsed["diagnostic"],
                )
                record.result = result
                await db.commit()

                processed.append({"gmail_message_id": msg_id, **parsed, "result": result})
                logger.info(
                    "Ingested %s complaint for %s (suppressed=%s)",
                    parsed["feedback_type"], parsed["email"], result.get("suppressed"),
                )
            except Exception as exc:
                await db.rollback()
                logger.warning("Failed to ingest complaint %s: %s", msg_id, exc)

        return processed

    async def ingest_smtp_account(
        self, db: AsyncSession, account: SmtpAccount, lookback_days: int = 3
    ) -> list[dict]:
        """Scan one SMTP mailbox (over IMAP) for FBL reports.

        Mirrors ``ingest_account`` but reads via IMAP and dedups on a synthetic
        key (``smtp:<account>:<message-id>``).
        """
        processed: list[dict] = []
        reader = SmtpMailboxReader(account)
        try:
            messages = await asyncio.to_thread(reader.fetch_recent, lookback_days)
        except Exception as exc:
            logger.error("SMTP complaint scan failed for %s: %s", account.email, exc)
            return processed

        for msg in messages:
            text = msg.get("text") or ""
            if not is_complaint_message(msg.get("from_email", ""), msg.get("subject", ""), text):
                continue
            raw_id = msg.get("message_id") or hashlib.sha1(
                (msg.get("from_email", "") + msg.get("subject", "") + text[:500]).encode(
                    "utf-8", "replace"
                )
            ).hexdigest()
            dedup_key = f"smtp:{account.id}:{raw_id}"[:255]
            try:
                if await self._already_processed(db, dedup_key):
                    continue
                parsed = parse_complaint(text)
                if not parsed:
                    logger.debug("SMTP message %s matched complaint filter but parsed no recipient", dedup_key)
                    continue

                record = ComplaintRecord(
                    gmail_message_id=dedup_key,
                    gmail_account_id=None,  # sourced from an SMTP mailbox
                    recipient_email=parsed["email"],
                    feedback_type=parsed["feedback_type"],
                    source=parsed["source"],
                    diagnostic=parsed["diagnostic"],
                    result={},
                )
                db.add(record)
                await db.flush()

                result = await record_complaint(
                    db,
                    parsed["email"],
                    feedback_type=parsed["feedback_type"],
                    diagnostic=parsed["diagnostic"],
                )
                record.result = result
                await db.commit()

                processed.append({"gmail_message_id": dedup_key, **parsed, "result": result})
                logger.info(
                    "Ingested %s SMTP complaint for %s (suppressed=%s)",
                    parsed["feedback_type"], parsed["email"], result.get("suppressed"),
                )
            except Exception as exc:
                await db.rollback()
                logger.warning("Failed to ingest SMTP complaint %s: %s", dedup_key, exc)

        return processed

    async def ingest_all_accounts(self, db: AsyncSession, lookback_days: int = 3) -> dict:
        """Scan every active connected mailbox (Gmail API + SMTP over IMAP)."""
        accounts = (
            await db.execute(select(GmailAccount).where(GmailAccount.is_active == True))  # noqa: E712
        ).scalars().all()

        total: list[dict] = []
        for account in accounts:
            total.extend(await self.ingest_account(db, account, lookback_days))

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

        suppressed = sum(1 for c in total if (c.get("result") or {}).get("suppressed"))
        return {
            "accounts_scanned": len(accounts) + len(smtp_accounts),
            "complaints_ingested": len(total),
            "contacts_suppressed": suppressed,
        }


_service: Optional[ComplaintIngestionService] = None


def get_complaint_ingestion_service() -> ComplaintIngestionService:
    global _service
    if _service is None:
        _service = ComplaintIngestionService()
    return _service
