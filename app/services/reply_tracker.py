"""Reply tracking service -- detects when contacts reply to outreach emails.

Periodically queries each connected Gmail account's inbox for messages from
addresses we have previously emailed.  When a reply is found the service:

    1. Marks the EmailSend record as ``replied`` (with timestamp).
    2. Stops the active outreach enrollment (sets status ``replied``).
    3. Logs an OutreachEvent so the timeline stays accurate.
    4. Auto-advances the parent Company stage to ``engaged`` when appropriate.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.interaction import Interaction
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.smtp_account import SmtpAccount
from app.services import crypto, google_oauth
from app.services.bounce_ingestion import is_bounce_message
from app.services.smtp_reader import SmtpMailboxReader

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Local-parts that are never a human prospect replying to us. A real reply comes
# from a person's mailbox, not from a newsletter engine or a system daemon. These
# are matched as substrings against the sender's local-part so "newsletter",
# "news", "noreply", "no-reply", "notifications", "mailer-daemon" etc. are all
# filtered. This is what keeps Squarespace/Kaggle/Google-Cloud/Claude.com bulk
# mail out of the reply tracker — and it also guards the same-domain fallback
# below from matching automated senders on a domain we happen to have emailed.
# Kept deliberately tight: only patterns that are *never* a human replying.
# Shared-but-human mailboxes (info@, hello@, support@, sales@, a shop's team@)
# are intentionally NOT here — a small prospect often replies from one, and
# dropping a genuine reply is worse than letting one newsletter slip through.
_AUTOMATED_LOCALPART_MARKERS = (
    "no-reply", "noreply", "no_reply", "do-not-reply", "do_not_reply", "donotreply",
    "mailer-daemon", "mail-daemon", "postmaster", "newsletter", "news@",
    "notifications", "notification", "notify@", "updates@", "update@",
    "digest", "bounce", "bounces", "unsubscribe", "mailer@", "auto-confirm",
    "automated", "alerts", "alert@", "marketing@", "campaigns", "email@",
)

# Free / consumer webmail providers. Same-domain reply matching must NOT trigger
# for these — everyone shares "gmail.com", so "we emailed a@gmail.com" tells us
# nothing about "b@gmail.com". Same-domain matching is only meaningful for a
# company's own mail domain.
_FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com", "fastmail.com",
    "pm.me", "hey.com", "tutanota.com", "qq.com", "163.com", "126.com",
})


def _domain_of(email: str) -> str:
    """Lowercased domain part of an address, or '' if it has none."""
    email = (email or "").strip().lower()
    return email.split("@", 1)[1] if "@" in email else ""


def _is_automated_sender(from_email: str) -> bool:
    """Whether ``from_email`` is an automated/bulk sender, not a human prospect.

    A genuine reply comes from a person's mailbox. Newsletters, notification
    engines, and system daemons never are, so they must never advance a send,
    pause an enrollment, or advance a company stage.
    """
    local = (from_email or "").strip().lower().split("@", 1)[0]
    if not local:
        return True
    full = (from_email or "").strip().lower()
    for marker in _AUTOMATED_LOCALPART_MARKERS:
        # Markers ending in "@" pin the whole local-part (e.g. "news@" must not
        # match "andrews@"); the rest are substring hits on the local-part.
        if marker.endswith("@"):
            if full.startswith(marker):
                return True
        elif marker in local:
            return True
    return False


class ReplyTracker:
    """Checks Gmail for replies to outreach emails and updates enrollment status."""

    async def check_for_replies(self, db: AsyncSession) -> list[dict]:
        """Check all active Gmail accounts for replies to sent outreach emails."""
        result = await db.execute(
            select(GmailAccount).where(
                GmailAccount.is_active == True,  # noqa: E712
                GmailAccount.status == "connected",
            )
        )
        accounts = result.scalars().all()

        all_replies: list[dict] = []
        for account in accounts:
            try:
                replies = await self._check_account_replies(db, account)
                all_replies.extend(replies)
            except Exception as e:
                logger.error(
                    "Failed to check replies for %s: %s", account.email, str(e)
                )

        # Also poll SMTP mailboxes over IMAP — otherwise a contact on an
        # SMTP-sent sequence keeps getting follow-ups after they've replied.
        smtp_accounts = (
            await db.execute(
                select(SmtpAccount).where(
                    SmtpAccount.status == "connected",
                    SmtpAccount.imap_enabled == True,  # noqa: E712
                )
            )
        ).scalars().all()
        for account in smtp_accounts:
            try:
                replies = await self._check_smtp_account_replies(db, account)
                all_replies.extend(replies)
            except Exception as e:
                logger.error(
                    "Failed to check SMTP replies for %s: %s", account.email, str(e)
                )

        return all_replies

    async def _check_smtp_account_replies(
        self, db: AsyncSession, account: SmtpAccount
    ) -> list[dict]:
        """Check a single SMTP mailbox (over IMAP) for replies to outreach.

        Reuses ``_match_reply_to_send`` so an SMTP reply advances the send,
        pauses the enrollment, and advances the company stage exactly like a
        Gmail reply does.
        """
        reader = SmtpMailboxReader(account)
        messages = await asyncio.to_thread(reader.fetch_recent, 1)

        own_address = (account.email or "").strip().lower()
        replies_found: list[dict] = []
        for msg in messages:
            from_email = (msg.get("from_email") or "").strip().lower()
            if not from_email or from_email == own_address:
                continue  # skip our own sent copies / empty senders
            subject = msg.get("subject", "")
            # Skip bulk/automated mail and delivery notices — a prospect reply
            # never comes from a newsletter engine or mailer-daemon.
            if _is_automated_sender(from_email) or is_bounce_message(from_email, subject):
                continue
            try:
                reply_info = await self._match_reply_to_send(db, from_email, subject)
            except Exception as e:
                logger.debug("SMTP reply match failed for %s: %s", from_email, e)
                continue
            if reply_info:
                replies_found.append({
                    "from_email": from_email,
                    "subject": subject,
                    "receiving_account": account.email,
                    **reply_info,
                })
        return replies_found

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_credentials(self, account: GmailAccount) -> Credentials:
        """Build OAuth credentials from encrypted tokens stored on the account."""
        settings = get_settings()
        refresh_token = crypto.decrypt(account.refresh_token_encrypted)
        access_token = None
        if account.access_token_encrypted:
            try:
                access_token = crypto.decrypt(account.access_token_encrypted)
            except Exception:
                access_token = None

        return Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri=_TOKEN_URI,
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            scopes=google_oauth.credential_scopes_for(account.scopes),
        )

    async def _check_account_replies(
        self, db: AsyncSession, account: GmailAccount
    ) -> list[dict]:
        """Check a single Gmail account for replies."""
        try:
            creds = self._build_credentials(account)
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)

            # Search for replies received in the last 24 hours that were not
            # sent by us.  This covers both threaded replies and fresh messages
            # from addresses we have emailed.
            query = "in:inbox newer_than:1d -from:me"
            results = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=50)
                .execute()
            )
            messages = results.get("messages", [])
            replies_found: list[dict] = []

            for msg_info in messages:
                try:
                    msg = (
                        service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=msg_info["id"],
                            format="metadata",
                            metadataHeaders=[
                                "From",
                                "Subject",
                                "In-Reply-To",
                                "References",
                            ],
                        )
                        .execute()
                    )

                    headers = {
                        h["name"].lower(): h["value"]
                        for h in msg.get("payload", {}).get("headers", [])
                    }
                    from_email = headers.get("from", "")
                    subject = headers.get("subject", "")

                    # Extract bare email from "Name <email>" format
                    if "<" in from_email and ">" in from_email:
                        from_email = from_email.split("<")[1].split(">")[0].strip()
                    from_email = from_email.strip().lower()

                    # Skip bulk/automated mail and delivery notices. The founder's
                    # mailbox also receives Squarespace/Kaggle/Google-Cloud/Claude
                    # newsletters; none are prospect replies, so they must not
                    # touch a send, enrollment, or company stage.
                    if _is_automated_sender(from_email) or is_bounce_message(
                        from_email, subject
                    ):
                        continue

                    # Try to match this message to an outreach we sent
                    reply_info = await self._match_reply_to_send(
                        db, from_email, subject
                    )
                    if reply_info:
                        replies_found.append(
                            {
                                "from_email": from_email,
                                "subject": subject,
                                "gmail_message_id": msg_info["id"],
                                **reply_info,
                            }
                        )

                except Exception as e:
                    logger.debug(
                        "Error processing message %s: %s",
                        msg_info.get("id"),
                        str(e),
                    )

            return replies_found

        except Exception as e:
            logger.error("Gmail API error for %s: %s", account.email, str(e))
            return []

    async def _match_reply_to_send(
        self, db: AsyncSession, from_email: str, subject: str
    ) -> Optional[dict]:
        """Match an incoming email to a previously sent outreach email.

        If matched, updates the EmailSend status to ``replied``, pauses the
        enrollment, records an event, and (when applicable) advances the
        company stage.
        """
        # Find the most recent email we sent to this address that has not
        # already been marked as replied.
        result = await db.execute(
            select(EmailSend)
            .where(func.lower(EmailSend.to_email) == from_email)
            .where(EmailSend.status.in_(["sent", "opened", "clicked"]))
            .order_by(EmailSend.sent_at.desc())
            .limit(1)
        )
        email_send = result.scalar_one_or_none()

        # Same-domain fallback: we emailed info@shop.com and john@shop.com
        # replies. Exact-address matching misses that, so fall back to the most
        # recent send to *anyone* at the same company domain. Guarded so it never
        # over-matches: free webmail domains are excluded (gmail.com is not a
        # company), and the automated-sender filter upstream keeps noreply@ off
        # this path.
        if email_send is None:
            domain = _domain_of(from_email)
            if domain and domain not in _FREE_EMAIL_DOMAINS:
                result = await db.execute(
                    select(EmailSend)
                    .where(func.lower(EmailSend.to_email).like(f"%@{domain}"))
                    .where(EmailSend.status.in_(["sent", "opened", "clicked"]))
                    .order_by(EmailSend.sent_at.desc())
                    .limit(1)
                )
                email_send = result.scalar_one_or_none()
                if email_send is not None:
                    logger.info(
                        "Reply from %s matched send to %s by shared domain %s",
                        from_email, email_send.to_email, domain,
                    )

        if not email_send:
            return None

        # -- Mark the send as replied --
        await db.execute(
            update(EmailSend)
            .where(EmailSend.id == email_send.id)
            .values(status="replied", replied_at=datetime.now(timezone.utc))
        )

        # -- Load the contact --
        contact = None
        if email_send.contact_id:
            result = await db.execute(
                select(Contact).where(Contact.id == email_send.contact_id)
            )
            contact = result.scalar_one_or_none()

        # -- Update enrollment: stop follow-ups --
        enrollment_updated = False
        if contact:
            result = await db.execute(
                select(OutreachEnrollment)
                .where(OutreachEnrollment.contact_id == contact.id)
                .where(OutreachEnrollment.status == "active")
            )
            enrollment = result.scalar_one_or_none()

            if enrollment:
                enrollment.status = "replied"
                enrollment.completed_at = datetime.now(timezone.utc)

                # Record a reply event on the enrollment timeline, attributed to
                # the step that was actually sent (NOT current_step, which the
                # send path has already advanced past — see outreach_events).
                from app.services.outreach_events import build_reply_event

                reply_event = await build_reply_event(
                    db,
                    enrollment,
                    metadata={
                        "from_email": from_email,
                        "subject": subject,
                    },
                )
                db.add(reply_event)
                enrollment_updated = True

            # Log an interaction record for the reply
            interaction = Interaction(
                contact_id=contact.id,
                company_id=contact.company_id,
                interaction_type="email_reply_received",
                subject=subject,
                notes=f"Reply received from {from_email}",
                occurred_at=datetime.now(timezone.utc),
            )
            db.add(interaction)

            # Auto-advance company stage when it is early in the pipeline
            if contact.company_id:
                result = await db.execute(
                    select(Company).where(Company.id == contact.company_id)
                )
                company = result.scalar_one_or_none()
                if company and company.stage in (
                    "new",
                    "researched",
                    "scored",
                    "contacted",
                ):
                    company.stage = "engaged"
                    logger.info(
                        "Auto-advanced company %s to 'engaged' due to reply",
                        company.name,
                    )

        await db.commit()

        return {
            "send_id": str(email_send.id),
            "contact_id": str(contact.id) if contact else None,
            "contact_name": (
                f"{contact.first_name or ''} {contact.last_name or ''}".strip()
                if contact
                else None
            ),
            "enrollment_paused": enrollment_updated,
            "original_subject": email_send.subject,
        }


def get_reply_tracker() -> ReplyTracker:
    """Factory function for the reply tracker singleton."""
    return ReplyTracker()
