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

import asyncio
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
from app.services.bounce_ingestion import is_bounce_message

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Number of days to defer a "not now" lead before nudging again.
NOT_NOW_FOLLOW_UP_DAYS = 30

# Reply intents that count as a "positive" outcome in the funnel analytics — the
# prospect showed real interest (wants to talk, asked a qualifying question, or
# asked to meet). Everything else (not_now / not_interested / referral /
# auto_reply) is a reply but not a positive one.
POSITIVE_REPLY_INTENTS = frozenset({"interested", "question", "meeting_request"})

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


# Auto-send safety floor (P1 fix) --------------------------------------------
# Header signals that an inbound message is machine-generated (out-of-office
# autoresponder, mailing list, bulk blast). Auto-replying to any of these is
# either useless (a bot) or actively harmful (a loop / a list). We must never
# auto-send to them — detection mirrors RFC 3834 (Auto-Submitted) plus the
# de-facto Precedence / List-* conventions.
def is_automated_message(headers: dict) -> Optional[str]:
    """Return the header reason a message looks machine-generated, else ``None``.

    ``headers`` is the lower-cased name→value map already built by the caller.
    Checked signals:

    - ``Auto-Submitted`` present and not ``no`` (RFC 3834 autoresponders/DSNs).
    - ``Precedence`` of ``bulk``/``list``/``junk`` (bulk mail convention).
    - Any ``List-*`` header (``List-Id``/``List-Unsubscribe``/…) — mailing list.
    - ``X-Autoreply`` / ``X-Autorespond`` (vendor out-of-office markers).
    """
    auto_submitted = (headers.get("auto-submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return f"auto-submitted:{auto_submitted}"

    precedence = (headers.get("precedence") or "").strip().lower()
    if precedence in ("bulk", "list", "junk"):
        return f"precedence:{precedence}"

    for name in headers:
        if name.startswith("list-"):
            return f"header:{name}"

    if headers.get("x-autoreply") or headers.get("x-autorespond"):
        return "x-autoreply"

    return None


class SmartReplyService:
    """Reads incoming replies from ALL connected accounts, classifies intent,
    drafts responses, and optionally auto-sends."""

    @staticmethod
    async def _gexec(request):
        """Run a blocking googleapiclient request off the event loop.

        The Gmail client is synchronous; calling ``.execute()`` inline blocks the
        whole asyncio loop for the duration of the network round-trip. Offloading
        to a thread (P1 fix) keeps the loop free while a mailbox is being read.
        """
        return await asyncio.to_thread(request.execute)

    async def connected_account_ids(self, db: AsyncSession) -> list:
        """IDs of every connected Gmail account in the current org context.

        Used by the fan-out dispatcher so each mailbox is processed by its own
        Celery task instead of a serial in-process loop.
        """
        result = await db.execute(
            select(GmailAccount.id).where(GmailAccount.status == "connected")
        )
        return list(result.scalars().all())

    async def process_account_by_id(self, db: AsyncSession, account_id) -> list[dict]:
        """Process one mailbox by id (the per-account fan-out entry point)."""
        account = (
            await db.execute(select(GmailAccount).where(GmailAccount.id == account_id))
        ).scalar_one_or_none()
        if account is None or account.status != "connected":
            return []
        return await self._process_account(db, account, get_settings())

    async def process_replies(self, db: AsyncSession) -> list[dict]:
        """Serial fallback: check ALL connected Gmail accounts for replies.

        The scheduled path fans out one Celery task per account (see
        ``process_account_replies``); this in-process loop is retained for direct
        callers and tests. Gmail I/O is offloaded off the event loop so even the
        serial path never blocks the loop for a whole mailbox.
        """
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
            scopes=google_oauth.credential_scopes_for(account.scopes),
        )

    async def _process_account(
        self, db: AsyncSession, gmail_account: GmailAccount, settings
    ) -> list[dict]:
        """Process replies for a single Gmail account."""
        try:
            creds = self._build_credentials(gmail_account)
            service = await asyncio.to_thread(
                build, "gmail", "v1", credentials=creds, cache_discovery=False
            )

            query = "in:inbox is:unread newer_than:2h -from:me"
            results_list = await self._gexec(
                service.users().messages().list(userId="me", q=query, maxResults=20)
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

        msg = await self._gexec(
            gmail_service.users().messages().get(userId="me", id=message_id, format="full")
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

        # A delivery-status notification is a bounce, not a reply. It lands in
        # the *same thread* as the send it failed, so the header/thread matcher
        # below ties it straight to the enrollment and files it as a prospect
        # reply. The damage is threefold: the enrollment is marked 'replied' —
        # permanently stopping outreach to a contact who never answered — the
        # funnel counts a reply that never happened, and the auto-responder can
        # draft a reply *to mailer-daemon*. ``email_history`` already gates its
        # own sync this way; this path was missing the same guard.
        # Bounces belong to the bounce-ingestion sweep (bounce_records).
        if is_bounce_message(from_email, subject):
            logger.debug(
                "Skipping delivery-status notification from %s (%s)",
                from_email, subject,
            )
            return None

        body_text = self._extract_body(msg.get("payload", {}))
        if not body_text:
            return None

        # Match to a prior send by recipient address. A *sequence* send is
        # recorded as an OutreachEvent and has NO EmailSend row, so a missing
        # EmailSend is not a reason to drop the reply — we fall through to
        # enrollment matching (by contact, then by threading headers) below.
        # EmailSend, when present, is the legacy-engagement send we also mark
        # 'replied'.
        result = await db.execute(
            select(EmailSend)
            .where(EmailSend.to_email == from_email)
            .where(EmailSend.status.in_(["sent", "opened", "clicked", "replied"]))
            .order_by(EmailSend.sent_at.desc())
            .limit(1)
        )
        email_send = result.scalar_one_or_none()

        # Dedup for the EmailSend path: this send is already answered.
        if email_send is not None and email_send.status == "replied" and email_send.replied_at:
            return None

        # Resolve the contact: prefer the EmailSend's contact, else match the
        # sender address to a known contact.
        contact = None
        company = None
        contact_name = sender_name
        company_name = ""

        if email_send is not None and email_send.contact_id:
            contact = (
                await db.execute(select(Contact).where(Contact.id == email_send.contact_id))
            ).scalar_one_or_none()
        if contact is None:
            contact = (
                await db.execute(select(Contact).where(Contact.email == from_email))
            ).scalars().first()

        # Match the enrollment this reply stops. Broadened beyond 'active': a
        # reply that lands while a step is parked for review, after the sequence
        # completed, or while paused must still stop the enrollment and record the
        # reply (see reply_matching.REPLY_STOPPABLE_STATUSES). By contact first,
        # then by the reply's threading headers against the ids the send path
        # stored on the enrollment — this is what lets a reply to a *sequence*
        # send (no EmailSend row) still be recognised, stopped, and answered.
        from app.services.reply_matching import (
            find_stoppable_enrollment,
            match_enrollment_by_headers,
        )

        enrollment = None
        # Scope enrollment matching to the account's own org: threading ids and
        # contact rows are not globally unique, so an explicit org filter keeps a
        # reply from ever matching another tenant's enrollment even when this
        # runs without a bound request context.
        account_org_id = gmail_account.org_id
        if contact is not None:
            enrollment = await find_stoppable_enrollment(
                db, contact.id, org_id=account_org_id
            )
        if enrollment is None:
            enrollment = await match_enrollment_by_headers(
                db,
                headers.get("in-reply-to"),
                headers.get("references"),
                org_id=account_org_id,
            )
        # A header match can identify the contact when the address didn't.
        if enrollment is not None and contact is None and enrollment.contact_id:
            contact = (
                await db.execute(select(Contact).where(Contact.id == enrollment.contact_id))
            ).scalar_one_or_none()

        # Nothing ties this inbound message to our outreach — no prior send and no
        # live enrollment to stop. A reply we already processed also lands here
        # ('replied' is excluded from both matchers), so this doubles as dedup for
        # the sequence path. Treat as inbox noise and skip.
        if email_send is None and enrollment is None:
            return None

        # Fill contact/company display fields now that the contact is resolved.
        if contact is not None:
            first = contact.first_name or ""
            last = contact.last_name or ""
            contact_name = f"{first} {last}".strip() or sender_name
            if contact.company_id:
                company = (
                    await db.execute(select(Company).where(Company.id == contact.company_id))
                ).scalar_one_or_none()
                if company:
                    company_name = company.name

        # Mark the legacy-engagement send replied (only when there was one).
        if email_send is not None:
            await db.execute(
                update(EmailSend)
                .where(EmailSend.id == email_send.id)
                .values(status="replied", replied_at=datetime.now(timezone.utc))
            )

        # Stop the matched enrollment and record the reply on the timeline.
        reply_event = None
        if enrollment is not None:
            enrollment.status = "replied"
            enrollment.completed_at = datetime.now(timezone.utc)
            # Attributed to the step actually sent, not current_step — the send
            # path advances the counter before the reply arrives.
            from app.services.outreach_events import build_reply_event

            reply_event = await build_reply_event(
                db,
                enrollment,
                metadata={
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
            original_subject=(email_send.subject if email_send is not None else subject) or "",
            original_body="",
            contact_name=contact_name,
            company_name=company_name,
        )

        # On meeting interest, guarantee the draft offers a scheduling link (2.5).
        meeting_interest = apply_scheduling_link(analysis)

        # Stamp the reply's intent onto the timeline event so the funnel
        # analytics can count "positive" replies straight off OutreachEvent
        # (its primary source) without re-classifying. positive == the intent
        # signalled real interest.
        if reply_event is not None:
            intent = (analysis or {}).get("intent") or ""
            reply_event.event_metadata = {
                **(reply_event.event_metadata or {}),
                "intent": intent,
                "positive": intent in POSITIVE_REPLY_INTENTS,
            }

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

        # Auto-send response only when every safety floor is cleared (P1 fix).
        # An auto-reply is an autonomous outbound send, so before firing we
        # require ALL of:
        #   - the global sending kill switch is on;
        #   - THIS org has opted in to auto-replies (per-org, not just the global
        #     config default);
        #   - the AI itself said should_auto_send AND produced a draft;
        #   - the classification confidence clears the min threshold;
        #   - the inbound message is NOT machine-generated (auto-submitted /
        #     bulk / mailing-list) — never reply to an OOO bot or a list, and
        #     never risk an auto-reply loop.
        # Every skip is logged with its reason so a suppressed auto-send is
        # auditable rather than silent.
        auto_sent = False
        auto_send_decision: dict = {"sent": False, "reason": None}
        settings = get_settings()
        from app.services import system_settings

        sending_on = await system_settings.is_sending_enabled(db)
        org_opted_in = await system_settings.is_auto_reply_enabled(db)
        min_conf = float(getattr(settings, "auto_reply_min_confidence", 0.75) or 0.0)
        try:
            confidence = float(analysis.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        automated_reason = is_automated_message(headers)

        if not sending_on:
            auto_send_decision["reason"] = "kill_switch_engaged"
        elif not org_opted_in:
            auto_send_decision["reason"] = "org_opt_out"
        elif not (analysis.get("should_auto_send") and analysis.get("draft_reply")):
            auto_send_decision["reason"] = "ai_declined"
        elif automated_reason is not None:
            auto_send_decision["reason"] = f"automated_message({automated_reason})"
        elif confidence < min_conf:
            auto_send_decision["reason"] = f"low_confidence({confidence:.2f}<{min_conf:.2f})"

        should_auto_send = auto_send_decision["reason"] is None
        if not should_auto_send and analysis.get("should_auto_send"):
            # The AI wanted to send but a safety floor blocked it — log so the
            # decision is visible in the ops trail.
            logger.info(
                "Auto-send suppressed for %s (%s): %s (intent=%s, conf=%.2f)",
                contact_name, from_email, auto_send_decision["reason"],
                analysis.get("intent"), confidence,
            )

        if should_auto_send:
            try:
                draft = analysis["draft_reply"]
                await self._send_reply(
                    gmail_service,
                    to_email=from_email,
                    subject=draft.get("subject", "Re: " + subject),
                    body=draft.get("body", ""),
                    thread_id=msg.get("threadId"),
                    in_reply_to=headers.get("message-id", ""),
                )
                auto_sent = True
                auto_send_decision = {"sent": True, "reason": None}

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
                auto_send_decision = {"sent": False, "reason": "send_failed"}

        # Mark Gmail message as read
        try:
            await self._gexec(
                gmail_service.users().messages().modify(
                    userId="me",
                    id=message_id,
                    body={"removeLabelIds": ["UNREAD"]},
                )
            )
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
            "auto_send_decision": auto_send_decision,
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

    async def _send_reply(
        self,
        gmail_service,
        to_email: str,
        subject: str,
        body: str,
        thread_id: Optional[str] = None,
        in_reply_to: str = "",
    ):
        """Send a reply via Gmail API, in the same thread (non-blocking)."""
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

        await self._gexec(
            gmail_service.users().messages().send(userId="me", body=send_body)
        )


def get_smart_reply_service() -> SmartReplyService:
    return SmartReplyService()
