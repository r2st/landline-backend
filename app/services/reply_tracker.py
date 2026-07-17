"""Reply tracking service -- detects when contacts reply to outreach emails.

Periodically queries each connected Gmail account's inbox for messages from
addresses we have previously emailed.  When a reply is found the service:

    1. Marks the EmailSend record as ``replied`` (with timestamp).
    2. Stops the active outreach enrollment (sets status ``replied``).
    3. Logs an OutreachEvent so the timeline stays accurate.
    4. Auto-advances the parent Company stage to ``engaged`` when appropriate.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.interaction import Interaction
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.services import crypto, google_oauth

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"


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

        return all_replies

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
            .where(EmailSend.to_email == from_email)
            .where(EmailSend.status.in_(["sent", "opened", "clicked"]))
            .order_by(EmailSend.sent_at.desc())
            .limit(1)
        )
        email_send = result.scalar_one_or_none()

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
