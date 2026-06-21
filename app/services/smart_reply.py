"""Smart reply orchestrator -- reads replies from ALL connected Gmail accounts,
analyzes with AI, drafts and sends responses.

Checks every connected Gmail account for incoming replies (not just the active
one), so replies to any sending identity are captured and processed.
"""

import base64
import logging
from datetime import datetime, timezone
from email.mime.text import MIMEText
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
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.services import crypto, google_oauth
from app.services.ai_service import get_ai_service

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"


class SmartReplyService:
    """Reads incoming replies from ALL connected accounts, classifies intent,
    drafts responses, and optionally auto-sends."""

    async def process_replies(self, db: AsyncSession) -> list[dict]:
        """Main entry: check ALL connected Gmail accounts for replies."""
        settings = get_settings()

        # Query ALL connected accounts, not just the active one
        result = await db.execute(
            select(GmailAccount).where(
                GmailAccount.status == "connected",
            )
        )
        accounts = result.scalars().all()

        all_results = []
        for account in accounts:
            try:
                results = await self._process_account(db, account, settings)
                all_results.extend(results)
            except Exception as e:
                logger.error("Smart reply failed for %s: %s", account.email, str(e))

        return all_results

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
            scopes=google_oauth.SCOPES,
        )

    async def _process_account(
        self, db: AsyncSession, gmail_account: GmailAccount, settings
    ) -> list[dict]:
        """Process replies for a single Gmail account."""
        try:
            creds = self._build_credentials(gmail_account)
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)

            query = "in:inbox is:unread newer_than:2h -from:me"
            results_list = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=20)
                .execute()
            )

            messages = results_list.get("messages", [])
            processed = []

            for msg_info in messages:
                try:
                    result = await self._process_single_reply(
                        db, service, msg_info["id"], gmail_account
                    )
                    if result:
                        processed.append(result)
                except Exception as e:
                    logger.debug(
                        "Error processing message %s: %s",
                        msg_info.get("id"),
                        str(e),
                    )

            return processed

        except Exception as e:
            logger.error("Gmail API error for %s: %s", gmail_account.email, str(e))
            return []

    async def _process_single_reply(
        self,
        db: AsyncSession,
        gmail_service,
        message_id: str,
        gmail_account: GmailAccount,
    ) -> Optional[dict]:
        """Process a single incoming message -- match, analyze, draft, optionally send."""

        msg = (
            gmail_service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from_email = headers.get("from", "")
        subject = headers.get("subject", "")

        sender_name = ""
        if "<" in from_email and ">" in from_email:
            sender_name = from_email.split("<")[0].strip().strip('"')
            from_email = from_email.split("<")[1].split(">")[0].strip()
        else:
            sender_name = from_email.split("@")[0]
        from_email = from_email.strip().lower()

        body_text = self._extract_body(msg.get("payload", {}))
        if not body_text:
            return None

        # Match to a sent email
        result = await db.execute(
            select(EmailSend)
            .where(EmailSend.to_email == from_email)
            .where(EmailSend.status.in_(["sent", "opened", "clicked", "replied"]))
            .order_by(EmailSend.sent_at.desc())
            .limit(1)
        )
        email_send = result.scalar_one_or_none()

        if not email_send:
            return None

        if email_send.status == "replied" and email_send.replied_at:
            return None

        # Get contact and company info
        contact = None
        company = None
        contact_name = sender_name
        company_name = ""

        if email_send.contact_id:
            result = await db.execute(
                select(Contact).where(Contact.id == email_send.contact_id)
            )
            contact = result.scalar_one_or_none()
            if contact:
                first = contact.first_name or ""
                last = contact.last_name or ""
                contact_name = f"{first} {last}".strip() or sender_name
                if contact.company_id:
                    result = await db.execute(
                        select(Company).where(Company.id == contact.company_id)
                    )
                    company = result.scalar_one_or_none()
                    if company:
                        company_name = company.name

        # Update email send status
        await db.execute(
            update(EmailSend)
            .where(EmailSend.id == email_send.id)
            .values(status="replied", replied_at=datetime.now(timezone.utc))
        )

        # Stop active outreach sequences
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
                reply_event = OutreachEvent(
                    enrollment_id=enrollment.id,
                    step_number=enrollment.current_step,
                    event_type="replied",
                    occurred_at=datetime.now(timezone.utc),
                    event_metadata={
                        "from_email": from_email,
                        "subject": subject,
                        "receiving_account": gmail_account.email,
                    },
                )
                db.add(reply_event)

        # Auto-advance company stage
        if company and company.stage in ("new", "researched", "scored", "contacted"):
            company.stage = "engaged"

        # Analyze with AI
        ai = get_ai_service()
        analysis = await ai.analyze_reply(
            reply_body=body_text,
            original_subject=email_send.subject or "",
            original_body="",
            contact_name=contact_name,
            company_name=company_name,
        )

        await db.commit()

        # Auto-send response if conditions are met
        auto_sent = False
        settings = get_settings()
        if (
            analysis.get("should_auto_send")
            and analysis.get("draft_reply")
            and getattr(settings, "auto_reply_enabled", False)
        ):
            try:
                draft = analysis["draft_reply"]
                self._send_reply(
                    gmail_service,
                    to_email=from_email,
                    subject=draft.get("subject", "Re: " + subject),
                    body=draft.get("body", ""),
                    thread_id=msg.get("threadId"),
                    in_reply_to=headers.get("message-id", ""),
                )
                auto_sent = True

                auto_send = EmailSend(
                    to_email=from_email,
                    subject=draft.get("subject", "Re: " + subject),
                    status="sent",
                    sent_at=datetime.now(timezone.utc),
                    contact_id=contact.id if contact else None,
                )
                db.add(auto_send)
                await db.commit()

                logger.info(
                    "Auto-replied to %s (%s) via %s — intent: %s",
                    contact_name, from_email, gmail_account.email,
                    analysis.get("intent"),
                )
            except Exception as e:
                logger.error("Auto-reply send failed via %s: %s", gmail_account.email, str(e))
                auto_sent = False

        # Mark Gmail message as read
        try:
            gmail_service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
        except Exception:
            pass

        return {
            "from_email": from_email,
            "contact_name": contact_name,
            "company_name": company_name,
            "subject": subject,
            "reply_snippet": body_text[:200],
            "receiving_account": gmail_account.email,
            "analysis": analysis,
            "auto_sent": auto_sent,
        }

    def _extract_body(self, payload: dict) -> str:
        """Extract plain text body from Gmail message payload."""
        if payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode(
                "utf-8", errors="replace"
            )

        parts = payload.get("parts", [])
        for part in parts:
            if (
                part.get("mimeType") == "text/plain"
                and part.get("body", {}).get("data")
            ):
                return base64.urlsafe_b64decode(part["body"]["data"]).decode(
                    "utf-8", errors="replace"
                )

        for part in parts:
            if (
                part.get("mimeType") == "text/html"
                and part.get("body", {}).get("data")
            ):
                from bs4 import BeautifulSoup

                html = base64.urlsafe_b64decode(part["body"]["data"]).decode(
                    "utf-8", errors="replace"
                )
                return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

        for part in parts:
            if "parts" in part:
                result = self._extract_body(part)
                if result:
                    return result

        return ""

    def _send_reply(
        self,
        gmail_service,
        to_email: str,
        subject: str,
        body: str,
        thread_id: str = None,
        in_reply_to: str = "",
    ):
        """Send a reply via Gmail API, in the same thread."""
        msg = MIMEText(body)
        msg["to"] = to_email
        msg["subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

        send_body = {"raw": raw}
        if thread_id:
            send_body["threadId"] = thread_id

        gmail_service.users().messages().send(userId="me", body=send_body).execute()


def get_smart_reply_service() -> SmartReplyService:
    return SmartReplyService()
