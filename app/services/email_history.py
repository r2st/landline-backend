"""Unified email history service — inbox, threads, lead emails, reply sync.

Provides a unified view of all email communication (sent and received) across
connected Gmail accounts. Syncs incoming replies to the local database and
allows sending threaded replies through the Gmail API.
"""

import base64
import html as html_mod
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment
from app.services import crypto, google_oauth
from app.services.bounce_ingestion import is_bounce_message
from app.services.gmail_sender import GmailSender

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _parse_message_ids(*header_values: Optional[str]) -> set[str]:
    """Pull individual RFC-2822 Message-IDs out of In-Reply-To / References
    header values. Each is a space-separated list of ``<id@host>`` tokens; we
    return them both bracket-stripped and raw so a match works whichever form
    the sent message stored."""
    ids: set[str] = set()
    for value in header_values:
        if not value:
            continue
        for token in value.split():
            token = token.strip()
            if not token:
                continue
            ids.add(token)
            ids.add(token.strip("<>"))
    return ids


class EmailHistoryService:
    """Unified email history: inbox, threads, lead emails, and reply sync."""

    # ------------------------------------------------------------------
    # Gmail API helpers
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

    def _build_gmail_service(self, account: GmailAccount):
        """Build an authorized Gmail API service for the given account."""
        creds = self._build_credentials(account)
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # ------------------------------------------------------------------
    # Reply syncing
    # ------------------------------------------------------------------

    async def _is_outreach_reply(
        self,
        db: AsyncSession,
        from_email: str,
        thread_id: Optional[str],
        in_reply_to: Optional[str],
        references: Optional[str],
    ) -> bool:
        """Decide whether an inbound message is a reply to *our* outreach.

        The Dialtone inbox exists to surface prospect replies, not the whole
        Gmail inbox (newsletters, Kaggle digests, receipts). A message counts as
        an outreach reply if ANY of these hold — cheapest / strongest first:

        1. **Thread match** — it sits in a Gmail thread we started (an EmailSend
           or an enrollment carries that ``gmail_thread_id``). Replies stay in
           the same thread, so this catches essentially every genuine reply.
        2. **Header match** — its ``In-Reply-To`` / ``References`` name a
           message id we recorded when sending (best-effort: covers SMTP sends
           whose RFC Message-ID we stored).
        3. **Known outreach contact** — it's *from* an address we've actually
           emailed (a manual EmailSend, or a contact we enrolled — sequence
           sends land on OutreachEvent, not EmailSend, so the enrollment join is
           what catches those). This catches replies that broke threading.

        Anything else is inbox noise and is skipped (no EmailReply row created).
        """
        # 1. Thread match.
        if thread_id:
            hit = (
                await db.execute(
                    select(EmailSend.id)
                    .where(EmailSend.gmail_thread_id == thread_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if hit is not None:
                return True
            hit = (
                await db.execute(
                    select(OutreachEnrollment.id)
                    .where(OutreachEnrollment.gmail_thread_id == thread_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if hit is not None:
                return True

        # 2. In-Reply-To / References header match against stored sent-message ids.
        ref_ids = _parse_message_ids(in_reply_to, references)
        if ref_ids:
            hit = (
                await db.execute(
                    select(EmailSend.id)
                    .where(
                        or_(
                            EmailSend.gmail_message_id.in_(ref_ids),
                            EmailSend.provider_message_id.in_(ref_ids),
                        )
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if hit is not None:
                return True
            hit = (
                await db.execute(
                    select(OutreachEnrollment.id)
                    .where(OutreachEnrollment.gmail_message_id.in_(ref_ids))
                    .limit(1)
                )
            ).scalar_one_or_none()
            if hit is not None:
                return True

        # 3. Sender is a known outreach contact (we've emailed them).
        if from_email:
            hit = (
                await db.execute(
                    select(EmailSend.id)
                    .where(func.lower(EmailSend.to_email) == from_email)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if hit is not None:
                return True
            hit = (
                await db.execute(
                    select(OutreachEnrollment.id)
                    .join(Contact, Contact.id == OutreachEnrollment.contact_id)
                    .where(func.lower(Contact.email) == from_email)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if hit is not None:
                return True

        return False

    async def sync_replies_for_account(
        self, db: AsyncSession, account: GmailAccount
    ) -> dict:
        """Sync new incoming replies for a single Gmail account.

        Queries Gmail for messages received in the last 24 hours that are not
        from the account owner.  For each message, if the gmail_message_id is
        not already in email_replies AND the message is a reply to our outreach
        (see ``_is_outreach_reply``), creates a new EmailReply record and
        attempts to match the sender to a known contact. Inbox noise that isn't
        a reply to outreach (newsletters, receipts, digests) is skipped so the
        Dialtone inbox stays a prospect-reply surface, not a mail mirror.
        """
        synced = 0
        errors = 0
        skipped = 0

        try:
            service = self._build_gmail_service(account)

            # Get all connected account emails to skip internal messages
            connected_result = await db.execute(
                select(GmailAccount.email).where(GmailAccount.status == "connected")
            )
            connected_emails = {e.lower() for e in connected_result.scalars().all()}

            query = "in:inbox newer_than:1d -from:me"
            results = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=50)
                .execute()
            )
            messages = results.get("messages", [])

            for msg_info in messages:
                try:
                    # Check if we already have this message
                    existing = (
                        await db.execute(
                            select(EmailReply.id).where(
                                EmailReply.gmail_message_id == msg_info["id"]
                            )
                        )
                    ).scalar_one_or_none()
                    if existing:
                        continue

                    # Fetch full message
                    msg = (
                        service.users()
                        .messages()
                        .get(userId="me", id=msg_info["id"], format="full")
                        .execute()
                    )

                    headers = {
                        h["name"].lower(): h["value"]
                        for h in msg.get("payload", {}).get("headers", [])
                    }
                    from_email = headers.get("from", "")
                    to_email = headers.get("to", "")
                    subject = headers.get("subject", "")

                    # Extract bare email from "Name <email>" format
                    if "<" in from_email and ">" in from_email:
                        from_email = from_email.split("<")[1].split(">")[0].strip()
                    from_email = from_email.strip().lower()

                    # Skip emails from other connected accounts (already tracked as sent)
                    if from_email in connected_emails:
                        continue

                    # Skip delivery-failure / delay notices (DSNs). A mailer-daemon
                    # bounce often lands in the same thread as the send it failed,
                    # so ``_is_outreach_reply``'s thread/header match would wave it
                    # through and file it as a prospect reply. It is a bounce — the
                    # bounce-ingestion sweep owns it (bounce_records), and it must
                    # never masquerade as a reply in the inbox.
                    if is_bounce_message(from_email, subject):
                        skipped += 1
                        continue

                    gmail_thread_id = msg.get("threadId")

                    # Gate: only genuine replies to our outreach reach the inbox.
                    # Everything else (newsletters, Kaggle, receipts) is noise.
                    if not await self._is_outreach_reply(
                        db,
                        from_email,
                        gmail_thread_id,
                        headers.get("in-reply-to"),
                        headers.get("references"),
                    ):
                        skipped += 1
                        continue

                    if "<" in to_email and ">" in to_email:
                        to_email = to_email.split("<")[1].split(">")[0].strip()
                    to_email = to_email.strip().lower()

                    # Extract body content
                    body_text, body_html = self._extract_body(msg.get("payload", {}))
                    snippet = html_mod.unescape(msg.get("snippet", ""))[:500]

                    # Parse received timestamp
                    internal_date = msg.get("internalDate")
                    if internal_date:
                        received_at = datetime.fromtimestamp(
                            int(internal_date) / 1000, tz=timezone.utc
                        )
                    else:
                        received_at = datetime.now(timezone.utc)

                    # Match to a contact by email
                    contact = (
                        await db.execute(
                            select(Contact).where(
                                func.lower(Contact.email) == from_email
                            )
                        )
                    ).scalar_one_or_none()

                    contact_id = contact.id if contact else None
                    company_id = contact.company_id if contact else None

                    # Try to match to an original sent email via thread_id
                    email_send_id = None
                    if gmail_thread_id:
                        original_send = (
                            await db.execute(
                                select(EmailSend.id).where(
                                    EmailSend.gmail_thread_id == gmail_thread_id
                                ).order_by(EmailSend.sent_at.asc()).limit(1)
                            )
                        ).scalar_one_or_none()
                        if original_send:
                            email_send_id = original_send

                    reply = EmailReply(
                        email_send_id=email_send_id,
                        contact_id=contact_id,
                        company_id=company_id,
                        gmail_account_id=account.id,
                        gmail_message_id=msg_info["id"],
                        gmail_thread_id=gmail_thread_id,
                        from_email=from_email,
                        to_email=to_email,
                        subject=subject,
                        body_text=body_text,
                        body_html=body_html,
                        snippet=snippet,
                        received_at=received_at,
                        is_read=False,
                    )
                    db.add(reply)
                    synced += 1

                except Exception as e:
                    logger.debug(
                        "Error processing message %s: %s", msg_info.get("id"), str(e)
                    )
                    errors += 1

            await db.commit()

        except RefreshError:
            # Revoked/invalid refresh token (invalid_grant). Don't swallow it —
            # let sync_all_accounts self-heal by deactivating the account.
            raise
        except Exception as e:
            logger.error("Gmail sync error for %s: %s", account.email, str(e))
            errors += 1

        return {
            "synced": synced,
            "errors": errors,
            "skipped": skipped,
            "account": account.email,
        }

    async def sync_all_accounts(self, db: AsyncSession, user_id=None) -> dict:
        """Sync replies for all active, connected Gmail accounts.

        When *user_id* is provided, only syncs accounts owned by that user.
        """
        query = select(GmailAccount).where(GmailAccount.status == "connected")
        if user_id is not None:
            query = query.where(GmailAccount.user_id == user_id)
        result = await db.execute(query)
        accounts = result.scalars().all()

        total_synced = 0
        total_errors = 0
        total_skipped = 0

        deactivated = 0

        for account in accounts:
            try:
                r = await self.sync_replies_for_account(db, account)
                total_synced += r["synced"]
                total_errors += r["errors"]
                total_skipped += r.get("skipped", 0)
            except RefreshError as e:
                # Self-heal: the OAuth refresh token has been revoked
                # (invalid_grant), so this account can never sync again until
                # it is re-connected. Flip its status to "revoked" so the
                # connected-account filter above stops polling it — this ends
                # the recurring error-log noise every 5 minutes. Warn (not
                # error): this is an expected, handled condition. Then keep
                # syncing the remaining accounts.
                account.status = "revoked"
                await db.commit()
                deactivated += 1
                logger.warning(
                    "Gmail account %s auto-deactivated (status=revoked): "
                    "OAuth token revoked / invalid_grant (%s)",
                    account.email, str(e),
                )
            except Exception as e:
                logger.error("Failed to sync replies for %s: %s", account.email, str(e))
                total_errors += 1

        return {
            "synced": total_synced,
            "errors": total_errors,
            "skipped": total_skipped,
            "deactivated": deactivated,
        }

    # ------------------------------------------------------------------
    # Body extraction from Gmail API payload
    # ------------------------------------------------------------------

    def _extract_body(self, payload: dict) -> tuple[Optional[str], Optional[str]]:
        """Extract text and HTML body from a Gmail API message payload.

        Prefers text/plain for body_text and text/html for body_html.
        Handles both simple and multipart messages.
        """
        body_text = None
        body_html = None

        mime_type = payload.get("mimeType", "")
        parts = payload.get("parts", [])

        if not parts:
            # Simple message (not multipart)
            data = payload.get("body", {}).get("data", "")
            if data:
                decoded = self._decode_base64url(data)
                if "html" in mime_type:
                    body_html = decoded
                else:
                    body_text = decoded
            return body_text, body_html

        # Multipart — recurse through parts
        for part in parts:
            part_mime = part.get("mimeType", "")
            sub_parts = part.get("parts", [])

            if sub_parts:
                # Nested multipart (e.g. multipart/alternative inside multipart/mixed)
                sub_text, sub_html = self._extract_body(part)
                if sub_text and not body_text:
                    body_text = sub_text
                if sub_html and not body_html:
                    body_html = sub_html
            else:
                data = part.get("body", {}).get("data", "")
                if data:
                    decoded = self._decode_base64url(data)
                    if part_mime == "text/plain" and not body_text:
                        body_text = decoded
                    elif part_mime == "text/html" and not body_html:
                        body_html = decoded

        return body_text, body_html

    @staticmethod
    def _decode_base64url(data: str) -> str:
        """Decode a base64url-encoded string (Gmail API format)."""
        # Add padding if needed
        padded = data + "=" * (4 - len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Lead emails — all threads for a company
    # ------------------------------------------------------------------

    async def get_lead_emails(
        self, db: AsyncSession, company_id: uuid.UUID, page: int = 1, per_page: int = 20
    ) -> dict:
        """Get all email threads for a given company (lead).

        Finds all contacts for the company, then queries both email_sends and
        email_replies for those contacts. Merges, sorts by timestamp, and
        groups by gmail_thread_id.
        """
        # Find all contacts for this company
        contacts_result = await db.execute(
            select(Contact).where(Contact.company_id == company_id)
        )
        contacts = contacts_result.scalars().all()
        contact_ids = [c.id for c in contacts]
        contact_map = {c.id: c for c in contacts}

        # Load company name
        company = (
            await db.execute(select(Company).where(Company.id == company_id))
        ).scalar_one_or_none()
        company_name = company.name if company else None

        if not contact_ids:
            return {"threads": [], "total_messages": 0}

        # Map account id -> email so each message carries its owning address.
        account_email_map = await self._load_account_email_map(db)

        # Query sent emails for these contacts
        sends = (
            await db.execute(
                select(EmailSend).where(
                    EmailSend.contact_id.in_(contact_ids),
                    EmailSend.status != "skipped",
                )
            )
        ).scalars().all()

        # Query received emails for these contacts
        replies = (
            await db.execute(
                select(EmailReply).where(EmailReply.contact_id.in_(contact_ids))
            )
        ).scalars().all()

        # Convert to unified message dicts
        messages = []
        for s in sends:
            contact = contact_map.get(s.contact_id)
            messages.append(
                self._send_to_message(s, contact, company_name, company_id, account_email_map)
            )

        for r in replies:
            contact = contact_map.get(r.contact_id)
            messages.append(
                self._reply_to_message(
                    r, contact, company_name, company_id, None, account_email_map
                )
            )

        # Group into threads
        threads = self._group_into_threads(messages, company_name, company_id)

        # Sort threads by last activity descending
        threads.sort(key=lambda t: t["last_activity"], reverse=True)

        # Paginate
        total_messages = len(messages)
        start = (page - 1) * per_page
        end = start + per_page
        paginated = threads[start:end]

        return {"threads": paginated, "total_messages": total_messages}

    # ------------------------------------------------------------------
    # Unified inbox
    # ------------------------------------------------------------------

    async def get_inbox(
        self,
        db: AsyncSession,
        page: int = 1,
        per_page: int = 20,
        filter_type: str = "all",
        search: Optional[str] = None,
        user_gmail_emails: Optional[set[str]] = None,
        gmail_account_id: Optional[uuid.UUID] = None,
        account_email: Optional[str] = None,
    ) -> dict:
        """Unified inbox query with filters.

        filter_type: all | sent | received | opened | needs_reply
        user_gmail_emails: when provided, scope to only these email addresses
        gmail_account_id: when provided, scope to a single connected Gmail
            account (sends from it and replies received on it)
        account_email: when provided, scope to a single connected account by its
            address — sends *from* it and replies *to* it. Covers SMTP accounts
            too, which have no account-id FK on the message rows.
        search: optional text search on subject/from_email
        """
        # Load connected account emails for direction detection
        if user_gmail_emails is not None:
            connected_emails = user_gmail_emails
        else:
            connected_result = await db.execute(
                select(GmailAccount.email).where(GmailAccount.status == "connected")
            )
            connected_emails = {e.lower() for e in connected_result.scalars().all()}

        # Map account id -> email so each message carries its owning address.
        account_email_map = await self._load_account_email_map(db)

        # Build sent messages query
        sends_query = select(EmailSend).where(EmailSend.status != "skipped")
        replies_query = select(EmailReply)

        # Scope to user's accounts when authenticated
        if user_gmail_emails is not None and user_gmail_emails:
            sends_query = sends_query.where(
                func.lower(EmailSend.from_email).in_(user_gmail_emails)
            )
            replies_query = replies_query.where(
                func.lower(EmailReply.to_email).in_(user_gmail_emails)
            )

        # Scope to a single email account when requested. Sends are matched by
        # their sender account, replies by the account they were received on.
        if gmail_account_id is not None:
            sends_query = sends_query.where(
                EmailSend.sender_account_id == gmail_account_id
            )
            replies_query = replies_query.where(
                EmailReply.gmail_account_id == gmail_account_id
            )

        # Scope to a single email account by address. This is the SMTP-friendly
        # filter: SMTP sends/replies carry no account-id FK, so we resolve the
        # owning account from the address instead — a sent message belongs to
        # the address it was sent FROM, a received message to the address it was
        # sent TO. Mirrors the id-or-address matching in ``danger_monitor``.
        if account_email:
            addr = account_email.lower()
            sends_query = sends_query.where(
                func.lower(EmailSend.from_email) == addr
            )
            replies_query = replies_query.where(
                func.lower(EmailReply.to_email) == addr
            )

        if filter_type == "sent":
            # Only sent emails
            replies_query = None
        elif filter_type == "received":
            # Only received — exclude replies from our own connected accounts
            sends_query = None
            if connected_emails:
                replies_query = replies_query.where(
                    ~func.lower(EmailReply.from_email).in_(connected_emails)
                )
        elif filter_type == "opened":
            sends_query = sends_query.where(EmailSend.opened_at.isnot(None))
            replies_query = None
        elif filter_type == "needs_reply":
            # Threads where the last message is a received email (needs reply)
            pass  # We'll filter after grouping

        if search:
            search_term = f"%{search}%"
            if sends_query is not None:
                sends_query = sends_query.where(
                    or_(
                        EmailSend.subject.ilike(search_term),
                        EmailSend.to_email.ilike(search_term),
                        EmailSend.from_email.ilike(search_term),
                    )
                )
            if replies_query is not None:
                replies_query = replies_query.where(
                    or_(
                        EmailReply.subject.ilike(search_term),
                        EmailReply.from_email.ilike(search_term),
                        EmailReply.to_email.ilike(search_term),
                    )
                )

        messages = []

        # Bound how many rows each side pulls before thread-grouping — an inbox is
        # a recency view, so the most-recent N sends/replies are what matter, and
        # an unbounded scan of the whole history was the risk the audit flagged.
        # Pull a generous multiple of the page window so grouping still has enough
        # rows to assemble full threads for the requested page.
        scan_cap = max(per_page * 20, 500)
        if sends_query is not None:
            sends_query = sends_query.order_by(EmailSend.sent_at.desc()).limit(scan_cap)
        if replies_query is not None:
            replies_query = replies_query.order_by(EmailReply.received_at.desc()).limit(scan_cap)

        if sends_query is not None:
            sends = (await db.execute(sends_query)).scalars().all()
            for s in sends:
                contact = await self._load_contact(db, s.contact_id)
                company_name, cid = await self._load_company_for_contact(db, contact)
                messages.append(
                    self._send_to_message(s, contact, company_name, cid, account_email_map)
                )

        if replies_query is not None:
            replies = (await db.execute(replies_query)).scalars().all()
            for r in replies:
                contact = await self._load_contact(db, r.contact_id)
                company_name = None
                cid = r.company_id
                if cid:
                    company = (
                        await db.execute(select(Company.name).where(Company.id == cid))
                    ).scalar_one_or_none()
                    company_name = company
                elif contact and contact.company_id:
                    cid = contact.company_id
                    company = (
                        await db.execute(select(Company.name).where(Company.id == cid))
                    ).scalar_one_or_none()
                    company_name = company
                messages.append(
                    self._reply_to_message(
                        r, contact, company_name, cid, connected_emails, account_email_map
                    )
                )

        # Group into threads
        threads = self._group_into_threads(messages)

        # Filter needs_reply after grouping
        if filter_type == "needs_reply":
            threads = [
                t for t in threads
                if t["messages"] and t["messages"][-1]["direction"] == "received"
            ]

        # Sort by last activity
        threads.sort(key=lambda t: t["last_activity"], reverse=True)

        total = len(threads)
        start = (page - 1) * per_page
        end = start + per_page
        paginated = threads[start:end]

        return {"threads": paginated, "total": total, "page": page, "per_page": per_page}

    # ------------------------------------------------------------------
    # Thread detail
    # ------------------------------------------------------------------

    async def get_thread(self, db: AsyncSession, gmail_thread_id: str) -> dict:
        """Get all messages in a thread (both sent and received)."""
        # Query sent emails in this thread
        sends = (
            await db.execute(
                select(EmailSend).where(EmailSend.gmail_thread_id == gmail_thread_id)
            )
        ).scalars().all()

        # Query received emails in this thread
        replies = (
            await db.execute(
                select(EmailReply).where(EmailReply.gmail_thread_id == gmail_thread_id)
            )
        ).scalars().all()

        messages = []
        lead_name = None
        lead_id = None

        # Map account id -> email so each message carries its owning address.
        account_email_map = await self._load_account_email_map(db)

        for s in sends:
            contact = await self._load_contact(db, s.contact_id)
            company_name, cid = await self._load_company_for_contact(db, contact)
            if company_name and not lead_name:
                lead_name = company_name
                lead_id = cid
            messages.append(
                self._send_to_message(s, contact, company_name, cid, account_email_map)
            )

        for r in replies:
            contact = await self._load_contact(db, r.contact_id)
            company_name = None
            cid = r.company_id
            if cid:
                company = (
                    await db.execute(select(Company.name).where(Company.id == cid))
                ).scalar_one_or_none()
                company_name = company
            if company_name and not lead_name:
                lead_name = company_name
                lead_id = cid
            messages.append(
                self._reply_to_message(
                    r, contact, company_name, cid, None, account_email_map
                )
            )

        # Sort by timestamp
        messages.sort(key=lambda m: m["timestamp"])

        subject = messages[0]["subject"] if messages else None
        has_unread = any(not m["is_read"] for m in messages)
        last_activity = messages[-1]["timestamp"] if messages else datetime.now(timezone.utc)

        return {
            "gmail_thread_id": gmail_thread_id,
            "subject": subject,
            "messages": messages,
            "last_activity": last_activity,
            "has_unread": has_unread,
            "lead_name": lead_name,
            "lead_id": lead_id,
        }

    # ------------------------------------------------------------------
    # Send reply
    # ------------------------------------------------------------------

    async def send_reply(
        self,
        db: AsyncSession,
        email_id: uuid.UUID,
        body_html: str,
        body_text: Optional[str] = None,
    ) -> dict:
        """Send a reply in the same Gmail thread.

        Looks up the referenced email (EmailSend or EmailReply), determines the
        thread context, and sends a reply via the Gmail API.
        """
        # Try to find the original as an EmailSend
        original_send = (
            await db.execute(select(EmailSend).where(EmailSend.id == email_id))
        ).scalar_one_or_none()

        # Or as an EmailReply
        original_reply = None
        if not original_send:
            original_reply = (
                await db.execute(select(EmailReply).where(EmailReply.id == email_id))
            ).scalar_one_or_none()

        if not original_send and not original_reply:
            return {"status": "error", "detail": "Original email not found"}

        # Extract thread context
        if original_send:
            gmail_thread_id = original_send.gmail_thread_id
            gmail_message_id = original_send.gmail_message_id
            to_email = original_send.to_email
            subject = original_send.subject or ""
            contact_id = original_send.contact_id
            sender_account_id = original_send.sender_account_id
        else:
            gmail_thread_id = original_reply.gmail_thread_id
            gmail_message_id = original_reply.gmail_message_id
            to_email = original_reply.from_email  # Reply goes to the sender
            subject = original_reply.subject or ""
            contact_id = original_reply.contact_id
            sender_account_id = original_reply.gmail_account_id

        # If no gmail_thread_id, we can still send as a new email (unthreaded reply)
        # This handles outreach emails that were sent without Gmail API tracking.
        if not gmail_thread_id:
            logger.info("No Gmail thread ID for email %s — sending as new email", email_id)

        # Find the Gmail account to send from
        account = None
        if sender_account_id:
            account = (
                await db.execute(
                    select(GmailAccount).where(
                        GmailAccount.id == sender_account_id,
                        GmailAccount.status == "connected",
                    )
                )
            ).scalar_one_or_none()

        if not account:
            # Fall back to any connected account
            account = (
                await db.execute(
                    select(GmailAccount).where(
                        GmailAccount.status == "connected",
                    ).limit(1)
                )
            ).scalar_one_or_none()

        if not account:
            return {"status": "error", "detail": "No connected Gmail account available"}

        # Prepare subject with Re: prefix
        reply_subject = subject
        if not reply_subject.lower().startswith("re:"):
            reply_subject = f"Re: {reply_subject}"

        # Build In-Reply-To and References headers
        in_reply_to = f"<{gmail_message_id}>" if gmail_message_id else None
        references = in_reply_to

        # Don't pass synthetic or empty thread IDs to Gmail API
        effective_thread_id = gmail_thread_id if (gmail_thread_id and not gmail_thread_id.startswith("__solo_")) else None

        # Send via Gmail
        sender = GmailSender(account)
        send_result = sender.send_email(
            to_email=to_email,
            subject=reply_subject,
            body_html=body_html,
            body_text=body_text,
            thread_id=effective_thread_id,
            in_reply_to=in_reply_to,
            references=references,
        )

        if send_result.get("status") != "sent":
            return {
                "status": "error",
                "detail": send_result.get("detail", "Failed to send reply"),
            }

        # Get company_id from contact
        company_id = None
        if contact_id:
            contact = (
                await db.execute(
                    select(Contact.company_id).where(Contact.id == contact_id)
                )
            ).scalar_one_or_none()
            company_id = contact

        # Record the sent reply as an EmailSend, attributed to the mailbox owner.
        email_send = EmailSend(
            contact_id=contact_id,
            to_email=to_email,
            subject=reply_subject,
            status="sent",
            gmail_message_id=send_result.get("gmail_message_id"),
            gmail_thread_id=send_result.get("gmail_thread_id") or gmail_thread_id,
            body_html=body_html,
            from_email=account.email,
            sender_account_id=account.id,
            user_id=account.user_id,
        )
        db.add(email_send)
        await db.commit()

        return {
            "status": "sent",
            "gmail_message_id": send_result.get("gmail_message_id"),
        }

    # ------------------------------------------------------------------
    # Fetch thread from Gmail API (optional — for viewing threads not yet synced)
    # ------------------------------------------------------------------

    async def fetch_thread_messages(
        self, db: AsyncSession, gmail_thread_id: str
    ) -> list[dict]:
        """Fetch all messages in a Gmail thread from the API.

        This is an optional method for viewing threads whose replies haven't
        been synced yet.  Prefers local data when available.
        """
        # First check local data
        local_thread = await self.get_thread(db, gmail_thread_id)
        if local_thread.get("messages"):
            return local_thread["messages"]

        # Fall back to Gmail API
        account = (
            await db.execute(
                select(GmailAccount).where(
                    GmailAccount.status == "connected",
                ).limit(1)
            )
        ).scalar_one_or_none()

        if not account:
            return []

        try:
            service = self._build_gmail_service(account)
            thread = (
                service.users()
                .threads()
                .get(userId="me", id=gmail_thread_id, format="full")
                .execute()
            )

            messages = []
            for msg in thread.get("messages", []):
                headers = {
                    h["name"].lower(): h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                from_email = headers.get("from", "")
                if "<" in from_email and ">" in from_email:
                    from_email = from_email.split("<")[1].split(">")[0].strip()
                from_email = from_email.strip().lower()

                to_email = headers.get("to", "")
                if "<" in to_email and ">" in to_email:
                    to_email = to_email.split("<")[1].split(">")[0].strip()
                to_email = to_email.strip().lower()

                is_from_us = from_email == account.email.lower()
                body_text, body_html = self._extract_body(msg.get("payload", {}))

                internal_date = msg.get("internalDate")
                timestamp = datetime.now(timezone.utc)
                if internal_date:
                    timestamp = datetime.fromtimestamp(
                        int(internal_date) / 1000, tz=timezone.utc
                    )

                messages.append({
                    "id": msg["id"],
                    "direction": "sent" if is_from_us else "received",
                    "from_email": from_email,
                    "to_email": to_email,
                    "subject": headers.get("subject"),
                    "body_html": body_html,
                    "body_text": body_text,
                    "snippet": html_mod.unescape(msg.get("snippet", ""))[:500],
                    "status": "sent" if is_from_us else "received",
                    "gmail_thread_id": gmail_thread_id,
                    "gmail_message_id": msg["id"],
                    "timestamp": timestamp,
                    "opened_at": None,
                    "clicked_at": None,
                    "is_read": True,
                    "contact_id": None,
                    "contact_name": None,
                    "company_id": None,
                    "company_name": None,
                    "gmail_account_id": account.id,
                    "gmail_account_email": account.email,
                })

            return messages

        except Exception as e:
            logger.error("Failed to fetch thread %s from Gmail: %s", gmail_thread_id, str(e))
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_account_email_map(
        self, db: AsyncSession
    ) -> dict[uuid.UUID, str]:
        """Map every Gmail account id to its email address.

        Used to attach the owning account's email to each message so the
        frontend can label and filter the inbox by email account. Includes
        revoked accounts so historical messages still resolve an address.
        """
        result = await db.execute(select(GmailAccount.id, GmailAccount.email))
        return {row[0]: row[1] for row in result.all()}

    def _send_to_message(
        self,
        send: EmailSend,
        contact: Optional[Contact],
        company_name: Optional[str],
        company_id: Optional[uuid.UUID],
        account_email_map: Optional[dict[uuid.UUID, str]] = None,
    ) -> dict:
        """Convert an EmailSend to a unified message dict."""
        contact_name = None
        if contact:
            contact_name = contact.full_name

        # Sent messages belong to the sender's connected account.
        account_id = send.sender_account_id
        account_email = None
        if account_email_map and account_id:
            account_email = account_email_map.get(account_id)
        if not account_email:
            account_email = send.from_email or None

        return {
            "id": send.id,
            "direction": "sent",
            "from_email": send.from_email or "",
            "to_email": send.to_email or "",
            "subject": send.subject,
            "body_html": send.body_html,
            "body_text": None,
            "snippet": None,
            "status": send.status,
            "gmail_thread_id": send.gmail_thread_id,
            "gmail_message_id": send.gmail_message_id,
            "timestamp": send.sent_at,
            "opened_at": send.opened_at,
            "clicked_at": send.clicked_at,
            "is_read": True,  # Sent messages are always "read"
            "contact_id": send.contact_id,
            "contact_name": contact_name,
            "company_id": company_id,
            "company_name": company_name,
            "gmail_account_id": account_id,
            "gmail_account_email": account_email,
        }

    def _reply_to_message(
        self,
        reply: EmailReply,
        contact: Optional[Contact],
        company_name: Optional[str],
        company_id: Optional[uuid.UUID],
        connected_emails: Optional[set] = None,
        account_email_map: Optional[dict[uuid.UUID, str]] = None,
    ) -> dict:
        """Convert an EmailReply to a unified message dict."""
        contact_name = None
        if contact:
            contact_name = contact.full_name

        # Determine direction: if sender is one of our connected accounts, it's "sent"
        direction = "received"
        if connected_emails and reply.from_email and reply.from_email.lower() in connected_emails:
            direction = "sent"

        # Received messages belong to the connected account they landed in.
        account_id = reply.gmail_account_id
        account_email = None
        if account_email_map and account_id:
            account_email = account_email_map.get(account_id)
        if not account_email:
            account_email = reply.to_email or None

        return {
            "id": reply.id,
            "direction": direction,
            "from_email": reply.from_email,
            "to_email": reply.to_email or "",
            "subject": reply.subject,
            "body_html": reply.body_html,
            "body_text": reply.body_text,
            "snippet": html_mod.unescape(reply.snippet) if reply.snippet else None,
            "status": direction,
            "gmail_thread_id": reply.gmail_thread_id,
            "gmail_message_id": reply.gmail_message_id,
            "timestamp": reply.received_at,
            "opened_at": None,
            "clicked_at": None,
            "is_read": reply.is_read,
            "contact_id": reply.contact_id,
            "contact_name": contact_name,
            "company_id": company_id or reply.company_id,
            "company_name": company_name,
            "gmail_account_id": account_id,
            "gmail_account_email": account_email,
        }

    def _group_into_threads(
        self,
        messages: list[dict],
        default_lead_name: Optional[str] = None,
        default_lead_id: Optional[uuid.UUID] = None,
    ) -> list[dict]:
        """Group a list of messages into threads by gmail_thread_id.

        Messages without a gmail_thread_id are each treated as their own thread.
        """
        thread_map: dict[str, list[dict]] = defaultdict(list)
        seen_message_ids: set[str] = set()
        solo_counter = 0

        for msg in messages:
            # Deduplicate: skip if gmail_message_id already seen (e.g. same
            # email exists as both EmailSend and synced EmailReply)
            msg_id = msg.get("gmail_message_id")
            if msg_id:
                if msg_id in seen_message_ids:
                    continue
                seen_message_ids.add(msg_id)

            thread_id = msg.get("gmail_thread_id")
            if not thread_id:
                # Each message without a thread_id becomes its own thread
                solo_counter += 1
                thread_id = f"__solo_{msg['id']}_{solo_counter}"
                msg["gmail_thread_id"] = thread_id
            thread_map[thread_id].append(msg)

        threads = []
        for thread_id, thread_messages in thread_map.items():
            # Sort messages within the thread by timestamp
            thread_messages.sort(key=lambda m: m["timestamp"])

            subject = thread_messages[0].get("subject")
            last_activity = thread_messages[-1]["timestamp"]
            has_unread = any(not m["is_read"] for m in thread_messages)

            # Determine lead from messages
            lead_name = default_lead_name
            lead_id = default_lead_id
            if not lead_name:
                for m in thread_messages:
                    if m.get("company_name"):
                        lead_name = m["company_name"]
                        lead_id = m.get("company_id")
                        break

            threads.append({
                "gmail_thread_id": thread_id,
                "subject": subject,
                "messages": thread_messages,
                "last_activity": last_activity,
                "has_unread": has_unread,
                "lead_name": lead_name,
                "lead_id": lead_id,
            })

        return threads

    async def _load_contact(
        self, db: AsyncSession, contact_id: Optional[uuid.UUID]
    ) -> Optional[Contact]:
        """Load a contact by ID, returning None if not found or ID is None."""
        if not contact_id:
            return None
        return (
            await db.execute(select(Contact).where(Contact.id == contact_id))
        ).scalar_one_or_none()

    async def _load_company_for_contact(
        self, db: AsyncSession, contact: Optional[Contact]
    ) -> tuple[Optional[str], Optional[uuid.UUID]]:
        """Load company name and ID for a contact."""
        if not contact or not contact.company_id:
            return None, None
        company = (
            await db.execute(
                select(Company).where(Company.id == contact.company_id)
            )
        ).scalar_one_or_none()
        if company:
            return company.name, company.id
        return None, None
