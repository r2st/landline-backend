"""Smart reply orchestrator -- reads replies from ALL connected Gmail accounts,
analyzes with AI, drafts and sends responses.

Checks every connected Gmail account for incoming replies (not just the active
one), so replies to any sending identity are captured and processed.

Beyond auto-replying to clearly-positive intent, it also handles the three most
common objections automatically (see :func:`classify_objection`):

- **"too expensive"** → drafts a value-focused response with a concrete ROI angle.
- **"not now"** → schedules a follow-up reminder 30 days out.
- **"talk to <name>"** → creates a referral-outreach task for the named person.
"""

import base64
import logging
import re
from datetime import datetime, timezone, timedelta
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
from app.models.follow_up import FollowUp
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.services import crypto, google_oauth
from app.services.ai_service import get_ai_service

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Number of days to defer a "not now" lead before nudging again.
NOT_NOW_FOLLOW_UP_DAYS = 30

# Intents / actions that signal the prospect is open to a meeting (feature 2.5).
_MEETING_INTENT_MARKERS = ("interested", "meeting_request", "question")
_MEETING_ACTION_MARKERS = ("schedule_meeting", "schedule_demo")


def apply_scheduling_link(analysis: dict) -> bool:
    """When a reply shows meeting interest, ensure the draft offers a scheduling link.

    The AI already tries to weave the link in, but this guarantees it: if a
    ``scheduling_link`` is configured, the intent/action indicates the prospect
    wants to talk, and the drafted body doesn't already contain the link, append
    it. Returns True when meeting interest was detected (whether or not a link was
    added), so callers can surface a ``meeting_interest`` flag.
    """
    intent = (analysis or {}).get("intent") or ""
    action = (analysis or {}).get("suggested_action") or ""
    meeting_interest = intent in _MEETING_INTENT_MARKERS or action in _MEETING_ACTION_MARKERS
    if not meeting_interest:
        return False

    link = getattr(get_settings(), "scheduling_link", "") or ""
    draft = analysis.get("draft_reply")
    if link and isinstance(draft, dict):
        body = draft.get("body") or ""
        if link not in body:
            connector = "\n\n" if body and not body.endswith("\n") else ""
            draft["body"] = f"{body}{connector}Grab a time that works for you here: {link}"
    return True

# Objection detection --------------------------------------------------------
# Substrings that signal a price/budget objection.
_PRICE_MARKERS = (
    "too expensive", "expensive", "too much", "cost too", "out of budget",
    "no budget", "can't afford", "cannot afford", "pricey", "price is",
    "too costly", "budget", "cheaper",
)
# Substrings that signal a timing objection.
_TIMING_MARKERS = (
    "not now", "not right now", "not a good time", "bad time", "next quarter",
    "next year", "later this year", "circle back", "reach out in", "revisit in",
    "too busy", "swamped", "reconnect in", "check back",
)
# Patterns that point us at another person to contact.
_REFERRAL_PATTERNS = (
    re.compile(r"talk to ([A-Z][a-zA-Z.\-]+(?: [A-Z][a-zA-Z.\-]+)?)"),
    re.compile(r"speak (?:to|with) ([A-Z][a-zA-Z.\-]+(?: [A-Z][a-zA-Z.\-]+)?)"),
    re.compile(r"reach out to ([A-Z][a-zA-Z.\-]+(?: [A-Z][a-zA-Z.\-]+)?)"),
    re.compile(r"contact ([A-Z][a-zA-Z.\-]+(?: [A-Z][a-zA-Z.\-]+)?)"),
    re.compile(r"connect (?:you )?with ([A-Z][a-zA-Z.\-]+(?: [A-Z][a-zA-Z.\-]+)?)"),
)


def _extract_referral_name(reply_text: str) -> Optional[str]:
    """Pull the name a prospect points us to, if the reply names one."""
    for pattern in _REFERRAL_PATTERNS:
        m = pattern.search(reply_text or "")
        if m:
            name = m.group(1).strip(" .")
            # Guard against matching a common word capitalised at sentence start.
            if name and name.lower() not in {"us", "me", "our", "the", "them", "you"}:
                return name
    return None


def classify_objection(reply_text: str, analysis: Optional[dict] = None) -> Optional[dict]:
    """Classify a reply into an actionable objection, or ``None``.

    Combines keyword detection on the raw reply with the AI intent (when
    provided). Returns a dict ``{"type": "price"|"timing"|"referral", ...}``:

    - ``price``   — the prospect balked at cost.
    - ``timing``  — bad timing ("not now").
    - ``referral`` — they pointed us to someone else (``referral_name`` set when
      a name was extracted).

    Price is checked first (most actionable), then referral, then timing.
    """
    text = (reply_text or "").lower()
    intent = (analysis or {}).get("intent") or ""

    if any(marker in text for marker in _PRICE_MARKERS):
        return {"type": "price"}

    if intent == "referral" or _extract_referral_name(reply_text):
        return {"type": "referral", "referral_name": _extract_referral_name(reply_text)}

    if intent == "not_now" or any(marker in text for marker in _TIMING_MARKERS):
        return {"type": "timing"}

    return None


def _fallback_roi_reply(contact_name: str, company_name: str) -> dict:
    """Deterministic value/ROI response used when AI generation is unavailable."""
    first = (contact_name or "there").split()[0]
    at_company = f" at {company_name}" if company_name else ""
    body = (
        f"Totally fair, {first} — let me put it in ROI terms instead of cost. "
        f"Most shops we work with{at_company} recover the spend within a quarter "
        "by cutting scrap and catching quality drift before parts ship. If I could "
        "show you the payback math on your own numbers in 15 minutes, worth a look?\n\n"
        "- Suman"
    )
    return {"subject": "Re: quick ROI math", "body": body}


async def generate_roi_response(
    ai, *, contact_name: str, company_name: str, reply_text: str
) -> dict:
    """Draft a value-focused reply to a price objection, framed around ROI.

    Never raises: falls back to a deterministic ROI reply when the API is
    unavailable or the response can't be parsed.
    """
    try:
        prompt = f"""A prospect pushed back on price. Write a short reply that reframes
cost as ROI for Apprend Technologies (manufacturing quality software — catches
quality drift in real time, cuts scrap, speeds audits).

Prospect: {contact_name} at {company_name}
Their reply: {reply_text[:500]}

Rules:
- Under 70 words, from the phone, sign off "- Suman".
- Don't argue about price. Reframe around payback: scrap reduction, fewer
  escapes, faster audits. Offer to run the payback math on THEIR numbers.
- No buzzwords, no exclamation marks.

Return ONLY valid JSON: {{"subject": "Re: ...", "body": "..."}}"""
        result = await ai._complete_json(prompt, max_tokens=400)
        if isinstance(result, dict) and result.get("body"):
            return {
                "subject": result.get("subject") or "Re: quick ROI math",
                "body": result["body"],
            }
    except Exception as e:  # noqa: BLE001 — never fail the caller
        logger.warning("ROI response generation failed: %s", e)
    return _fallback_roi_reply(contact_name, company_name)


async def handle_objection(
    db: AsyncSession,
    objection: dict,
    *,
    contact: Optional[Contact],
    company: Optional[Company],
    reply_text: str,
    contact_name: str = "",
    company_name: str = "",
    analysis: Optional[dict] = None,
    ai=None,
    user_id=None,
    now: Optional[datetime] = None,
) -> dict:
    """Take the automated action for a classified objection.

    - ``price``    → draft a value/ROI response (returned as ``draft``; not
      auto-sent, so a human can glance before it goes out).
    - ``timing``   → schedule a follow-up reminder ``NOT_NOW_FOLLOW_UP_DAYS``
      days out.
    - ``referral`` → create a referral-outreach follow-up task for the named
      person.

    Returns a dict describing what was done. Any DB rows are added to ``db`` and
    flushed but not committed (the caller owns the transaction).
    """
    now = now or datetime.now(timezone.utc)
    otype = objection.get("type")

    if otype == "price":
        if ai is None:
            ai = get_ai_service()
        draft = await generate_roi_response(
            ai,
            contact_name=contact_name or (contact.full_name if contact else ""),
            company_name=company_name or (company.name if company else ""),
            reply_text=reply_text,
        )
        return {"action": "roi_response", "objection": "price", "draft": draft}

    if otype == "timing":
        due = now + timedelta(days=NOT_NOW_FOLLOW_UP_DAYS)
        follow_up = FollowUp(
            user_id=user_id,
            contact_id=contact.id if contact else None,
            company_id=company.id if company else None,
            due_date=due,
            reason=f"Lead said 'not now' — nudge again in {NOT_NOW_FOLLOW_UP_DAYS} days",
            status="pending",
            notes=(reply_text or "")[:500],
        )
        db.add(follow_up)
        await db.flush()
        return {
            "action": "scheduled_follow_up",
            "objection": "timing",
            "follow_up_id": str(follow_up.id),
            "due_date": due.isoformat(),
        }

    if otype == "referral":
        name = objection.get("referral_name")
        who = name or "the referred contact"
        follow_up = FollowUp(
            user_id=user_id,
            contact_id=contact.id if contact else None,
            company_id=company.id if company else None,
            due_date=now,
            reason=f"Referral: reach out to {who} at {company_name or (company.name if company else 'the company')}",
            status="pending",
            notes=(reply_text or "")[:500],
        )
        db.add(follow_up)
        await db.flush()
        return {
            "action": "referral_task",
            "objection": "referral",
            "referral_name": name,
            "follow_up_id": str(follow_up.id),
        }

    return {"action": "none"}


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

        # On meeting interest, guarantee the draft offers a scheduling link (2.5).
        meeting_interest = apply_scheduling_link(analysis)

        # Handle common objections automatically (price / timing / referral).
        objection_action = None
        objection = classify_objection(body_text, analysis)
        if objection:
            try:
                objection_action = await handle_objection(
                    db,
                    objection,
                    contact=contact,
                    company=company,
                    reply_text=body_text,
                    contact_name=contact_name,
                    company_name=company_name,
                    analysis=analysis,
                    ai=ai,
                    user_id=gmail_account.user_id,
                )
                logger.info(
                    "Objection handled for %s (%s): %s",
                    contact_name, from_email, objection_action.get("action"),
                )
            except Exception as e:  # noqa: BLE001 — objection handling is best-effort
                logger.error("Objection handling failed for %s: %s", from_email, str(e))

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
                    from_email=gmail_account.email,
                    sender_account_id=gmail_account.id,
                    user_id=gmail_account.user_id,
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
            "objection_action": objection_action,
            "meeting_interest": meeting_interest,
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
