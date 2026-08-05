"""Unified email history service — inbox, threads, lead emails, reply sync.

Provides a unified view of all email communication (sent and received) across
connected Gmail accounts. Syncs incoming replies to the local database and
allows sending threaded replies through the Gmail API.
"""

import asyncio
import html as html_mod
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import and_, func, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import NON_WIRE_STATUS_VALUES, EmailSend
from app.models.gmail_account import GmailAccount
from app.models.inbox_label import InboxLabel, InboxThreadLabel
from app.models.inbox_thread_state import InboxThreadState
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.services import crypto, google_oauth
from app.services.bounce_ingestion import is_bounce_message
from app.services.gmail_body import decode_body
from app.services.reply_matching import bound_header_tokens
from app.services.sql_search import LIKE_ESCAPE, contains_pattern, endswith_pattern
from app.services.gmail_sender import GmailSender

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _parse_message_ids(*header_values: Optional[str]) -> set[str]:
    """Pull individual RFC-2822 Message-IDs out of In-Reply-To / References
    header values. Each is a space-separated list of ``<id@host>`` tokens; we
    return them both bracket-stripped and raw so a match works whichever form
    the sent message stored.

    **Bounded**, for the same reason ``reply_matching._message_id_tokens`` is.
    These two headers are written by the *sender* of the inbound message —
    attacker-controlled input arriving from the open internet, bounded by
    nothing between the mail server and here. Every token becomes several bind
    parameters in the ``IN`` clauses ``_is_outreach_reply`` builds from this
    set, and every driver refuses a statement past its own parameter ceiling
    (psycopg: 65535). One crafted reply with a few thousand ids therefore raised
    *inside the mailbox sweep*, which is the failure a mailbox cannot recover
    from on its own: ingestion stops, and no replies at all reads exactly like
    no replies yet. `reply_matching` was capped for precisely this; this second
    parser was written before that and never was.

    :func:`~app.services.reply_matching.bound_header_tokens` owns which tokens
    survive — the direct parent in ``In-Reply-To`` is kept whole and only the
    ``References`` chain is cut, from its oldest end. The cap costs a real
    conversation nothing: RFC 5322 encourages trimming the chain, and the id
    that threads a reply to *our* send is the most recent one in it.
    """
    per_header: list[list[str]] = []
    seen: set[str] = set()
    for value in header_values:
        found: list[str] = []
        for token in (value or "").split():
            token = token.strip()
            if token and token not in seen:
                seen.add(token)
                found.append(token)
        per_header.append(found)

    # Bound the *tokens*, then expand each to its two matchable forms — so the
    # ceiling on bind parameters is 2x the cap and not 2x the header.
    ids: set[str] = set()
    for token in bound_header_tokens(per_header):
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

    @staticmethod
    async def _gexec(request):
        """Run one Gmail API request off the event loop.

        ``googleapiclient`` is synchronous: ``request.execute()`` is a blocking
        HTTPS round trip. Called straight from a coroutine it stops *every*
        other coroutine in the process for its duration, and the sync below
        makes one of these per message — up to 50 per mailbox, across every
        connected account — behind ``POST /api/v1/emails/sync``, which is a
        request-path endpoint. One operator pressing "Sync now" therefore
        stalled the whole API worker, for every tenant on it, for as long as
        Gmail took to answer. Nothing here is slow in the CPU sense; the worker
        is simply waiting on a socket with the loop in its hand.

        ``smart_reply`` has offloaded the identical calls since it was written
        (its own ``_gexec``). This module did not, which is the only reason the
        two behave differently under load.
        """
        return await asyncio.to_thread(request.execute)

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

        # Held as plain values. A rollback — which the per-message handler below
        # now does — expires every instance loaded on this session, and reading
        # an expired attribute back needs a database round trip that the sync
        # ORM accessor cannot make from a coroutine (``MissingGreenlet``). So
        # the recovery would itself break the messages after the bad one.
        account_id = account.id
        account_email = account.email

        try:
            # Off the loop as well: ``build`` refreshes credentials and, on some
            # client versions, fetches the discovery document — both network.
            service = await asyncio.to_thread(self._build_gmail_service, account)

            # Get all connected account emails to skip internal messages
            connected_result = await db.execute(
                select(GmailAccount.email).where(GmailAccount.status == "connected")
            )
            connected_emails = {e.lower() for e in connected_result.scalars().all()}

            query = "in:inbox newer_than:1d -from:me"
            results = await self._gexec(
                service.users().messages().list(userId="me", q=query, maxResults=50)
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
                    msg = await self._gexec(
                        service.users()
                        .messages()
                        .get(userId="me", id=msg_info["id"], format="full")
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

                    # Match to a contact by email. ``contacts`` carries no
                    # unique constraint at all and the scraper re-creates rows,
                    # so a duplicated address is expected — the same duplicate
                    # that used to break every unsubscribe link. Asserting a
                    # single row here aborted the whole mailbox sync over one of
                    # them, and on the mail path that reads as "no replies yet"
                    # rather than as a failure. Earliest row wins, so a thread
                    # keeps attaching to the same contact across syncs.
                    contact = (
                        await db.execute(
                            select(Contact)
                            .where(func.lower(Contact.email) == from_email)
                            .order_by(Contact.created_at.asc(), Contact.id.asc())
                            .limit(1)
                        )
                    ).scalars().first()

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
                        gmail_account_id=account_id,
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
                    # Committed per message, not per mailbox. The rollback in
                    # the handler below is what makes one bad message survivable
                    # at all, and a rollback discards everything still pending —
                    # so batching the commit would mean recovering from message
                    # 2 by throwing away message 1.
                    await db.commit()
                    synced += 1

                except Exception as e:
                    # A failed statement leaves the session in a state where
                    # every later statement raises until someone rolls back.
                    # Nothing did, so the message after the bad one failed, and
                    # the one after that, and the commit at the end — and then
                    # the *next mailbox* in the sweep, on the same session, and
                    # the caller's session after that. One crafted reply to one
                    # mailbox stopped reply ingestion for the whole org, and a
                    # mailbox that stops ingesting is indistinguishable from a
                    # quiet one.
                    await db.rollback()
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
            logger.error("Gmail sync error for %s: %s", account_email, str(e))
            # Same reasoning as the per-message handler: the failure may well
            # have been a statement, and the caller keeps using this session.
            try:
                await db.rollback()
            except Exception:  # pragma: no cover - rollback of a dead session
                pass
            errors += 1

        return {
            "synced": synced,
            "errors": errors,
            "skipped": skipped,
            "account": account_email,
        }

    async def sync_all_accounts(self, db: AsyncSession, user_id=None) -> dict:
        """Sync replies for all active, connected Gmail accounts.

        When *user_id* is provided, only syncs accounts owned by that user.
        """
        query = select(GmailAccount).where(GmailAccount.status == "connected")
        if user_id is not None:
            query = query.where(GmailAccount.user_id == user_id)
        result = await db.execute(query)
        # Ids, not instances. A mailbox that fails is recovered from with a
        # rollback, and a rollback expires every instance on this session — so
        # holding the list would leave the *remaining* mailboxes as expired
        # objects whose next attribute read is a lazy refresh from a coroutine
        # (``MissingGreenlet``). Re-reading each row is the recovery working.
        account_ids = [a.id for a in result.scalars().all()]

        total_synced = 0
        total_errors = 0
        total_skipped = 0

        deactivated = 0

        for account_id in account_ids:
            account = await db.get(GmailAccount, account_id)
            if account is None:  # pragma: no cover - deleted mid-sweep
                continue
            account_email = account.email
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
                    account_email, str(e),
                )
            except Exception as e:
                logger.error("Failed to sync replies for %s: %s", account_email, str(e))
                # The account after this one shares the session. If what failed
                # was a statement, leaving it unrolled-back makes every mailbox
                # behind this one fail too — the sweep stops at the first bad
                # mailbox and reports the whole org as having no replies.
                try:
                    await db.rollback()
                except Exception:  # pragma: no cover - rollback of a dead session
                    pass
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
        """Decode a base64url-encoded string (Gmail API format).

        Delegates to ``gmail_body.decode_body``, which is the one decoder. This
        module was the only one of four that padded at all, so it kept working
        while the bounce and complaint sweeps silently read two message bodies
        in three as empty.
        """
        return decode_body(data)

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
                    EmailSend.status.notin_(NON_WIRE_STATUS_VALUES),
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

        # Sequence sends for these contacts (no EmailSend row exists for them).
        sequence_rows = await self._fetch_sequence_sends(db, contact_ids=contact_ids)
        engagement = await self._sequence_engagement(
            db, {enr.id for _, enr in sequence_rows}
        )
        for event, enrollment in sequence_rows:
            messages.append(
                self._sequence_send_to_message(
                    event,
                    enrollment,
                    contact_map.get(enrollment.contact_id),
                    company_name,
                    company_id,
                    account_email_map,
                    engagement.get((enrollment.id, event.step_number)),
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
    # Sequence sends (OutreachEvent)
    # ------------------------------------------------------------------
    #
    # The sequence engine (``app.tasks.outreach.send_sequence_step``) records a
    # real send as ``OutreachEvent(event_type="sent")`` and deliberately writes
    # NO ``EmailSend`` row — see the comments around the event construction
    # there. Every mail surface in this service was built on ``EmailSend``
    # alone, so the outbound half of Dialtone's own outreach was invisible: the
    # email sat in the sender's Gmail "Sent" folder, and the Landline inbox
    # showed nothing (or, worse, showed a prospect's reply hanging under a
    # thread with no preceding message).
    #
    # The event's ``metadata`` already carries everything needed to render the
    # message — ``to_email``, ``subject``, ``body_html`` (the human copy, before
    # tracking instrumentation), plus ``from_email`` / ``sender_account_id`` /
    # ``gmail_thread_id`` / ``gmail_message_id`` merged in from the dispatcher's
    # send result. So this is a read-side union, not a second write path: the
    # send accounting (warm-up counters, deliverability, A/B attribution) keeps
    # its single source of truth and only the *view* is made whole.

    @staticmethod
    def _sequence_send_identity(
        event: OutreachEvent, enrollment: OutreachEnrollment
    ) -> tuple[Optional[uuid.UUID], Optional[str]]:
        """The (account id, from address) a sequence send actually went out on.

        The event's metadata is authoritative: sender rotation can move a later
        step onto a different mailbox, and the enrollment column only remembers
        the *most recent* one. The enrollment is the fallback for older events
        recorded before the dispatcher merged its result into the metadata.
        """
        meta = event.event_metadata or {}
        account_id = None
        raw_id = meta.get("sender_account_id")
        if raw_id:
            try:
                account_id = uuid.UUID(str(raw_id))
            except (ValueError, AttributeError, TypeError):
                account_id = None
        if account_id is None:
            account_id = enrollment.sender_account_id

        from_email = meta.get("from_email") or meta.get("sender")
        return account_id, (from_email.lower() if from_email else None)

    async def _fetch_sequence_sends(
        self,
        db: AsyncSession,
        *,
        scopes: Optional[list[tuple[set, set]]] = None,
        gmail_thread_id: Optional[str] = None,
        contact_ids: Optional[list] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> list[tuple[OutreachEvent, OutreachEnrollment]]:
        """Load ``sent`` outreach events with their enrollment, newest first.

        ``scopes`` is a list of ``(account_ids, emails)`` pairs; a send is kept
        only if it satisfies EVERY pair (its sending account id is in that
        pair's id set, or its from-address is in that pair's address set). That
        mirrors how ``get_inbox`` layers the caller's own-mailbox scope on top
        of an optional single-account filter — both must hold.

        The scope test needs the event's JSON metadata, so it runs in Python
        rather than SQL (the column is JSONB in Postgres but a plain JSON string
        under the SQLite test shim, and no portable predicate spans both).
        ``limit`` therefore bounds the rows *scanned*, exactly like the
        ``scan_cap`` the send/reply queries use.
        """
        query = (
            select(OutreachEvent, OutreachEnrollment)
            .join(
                OutreachEnrollment,
                OutreachEvent.enrollment_id == OutreachEnrollment.id,
            )
            .where(OutreachEvent.event_type == "sent")
        )
        if gmail_thread_id is not None:
            query = query.where(
                OutreachEnrollment.gmail_thread_id == gmail_thread_id
            )
        if contact_ids is not None:
            if not contact_ids:
                return []
            query = query.where(OutreachEnrollment.contact_id.in_(contact_ids))
        if date_from is not None:
            query = query.where(OutreachEvent.occurred_at >= date_from)
        if date_to is not None:
            query = query.where(OutreachEvent.occurred_at <= date_to)

        query = query.order_by(OutreachEvent.occurred_at.desc())
        if limit is not None:
            query = query.limit(limit)

        rows = (await db.execute(query)).all()

        if not scopes:
            return [(event, enrollment) for event, enrollment in rows]

        kept = []
        for event, enrollment in rows:
            account_id, from_email = self._sequence_send_identity(event, enrollment)
            if all(
                (account_id is not None and account_id in account_ids)
                or (from_email is not None and from_email in emails)
                for account_ids, emails in scopes
            ):
                kept.append((event, enrollment))
        return kept

    async def _sequence_engagement(
        self, db: AsyncSession, enrollment_ids
    ) -> dict[tuple, dict]:
        """First open / click per ``(enrollment_id, step_number)``.

        Sequence engagement lands on sibling ``opened`` / ``clicked`` events
        rather than on columns of the send row, so the inbox's ``opened`` filter
        and the per-message open/click stamps have to be assembled here.
        """
        ids = list(enrollment_ids)
        if not ids:
            return {}
        rows = (
            await db.execute(
                select(
                    OutreachEvent.enrollment_id,
                    OutreachEvent.step_number,
                    OutreachEvent.event_type,
                    func.min(OutreachEvent.occurred_at),
                )
                .where(
                    OutreachEvent.enrollment_id.in_(ids),
                    OutreachEvent.event_type.in_(("opened", "clicked")),
                )
                .group_by(
                    OutreachEvent.enrollment_id,
                    OutreachEvent.step_number,
                    OutreachEvent.event_type,
                )
            )
        ).all()

        engagement: dict[tuple, dict] = {}
        for enrollment_id, step_number, event_type, occurred_at in rows:
            entry = engagement.setdefault((enrollment_id, step_number), {})
            key = "opened_at" if event_type == "opened" else "clicked_at"
            entry[key] = occurred_at
        return engagement

    def _sequence_send_to_message(
        self,
        event: OutreachEvent,
        enrollment: OutreachEnrollment,
        contact: Optional[Contact],
        company_name: Optional[str],
        company_id: Optional[uuid.UUID],
        account_email_map: Optional[dict[uuid.UUID, str]] = None,
        engagement: Optional[dict] = None,
    ) -> dict:
        """Convert a ``sent`` OutreachEvent to a unified message dict."""
        meta = event.event_metadata or {}
        account_id, from_email = self._sequence_send_identity(event, enrollment)

        account_email = None
        if account_email_map is not None and account_id:
            account_email = account_email_map.get(account_id)
            if account_email is None:
                # An id that resolves to no connected Gmail account is an SMTP
                # sender (the dispatcher reports both under the same key). Don't
                # publish it as ``gmail_account_id`` — the frontend filters on
                # that field and would match it against Gmail accounts.
                account_id = None
        if not from_email:
            from_email = account_email.lower() if account_email else None
        if not account_email:
            account_email = from_email

        engagement = engagement or {}

        return {
            "id": event.id,
            "direction": "sent",
            "from_email": from_email or "",
            "to_email": meta.get("to_email")
            or (contact.email if contact is not None else "")
            or "",
            "subject": meta.get("subject"),
            "body_html": meta.get("body_html"),
            "body_text": None,
            "snippet": None,
            "status": "sent",
            "gmail_thread_id": meta.get("gmail_thread_id")
            or enrollment.gmail_thread_id,
            "gmail_message_id": meta.get("gmail_message_id"),
            "timestamp": event.occurred_at,
            "opened_at": engagement.get("opened_at"),
            "clicked_at": engagement.get("clicked_at"),
            "is_read": True,  # Sent messages are always "read"
            "contact_id": enrollment.contact_id,
            "contact_name": contact.full_name if contact is not None else None,
            "company_id": company_id,
            "company_name": company_name,
            "gmail_account_id": account_id,
            "gmail_account_email": account_email,
            # Classification/priority live only on incoming replies.
            "reply_classification": None,
            "priority_flag": False,
        }

    @staticmethod
    def _message_matches_search(message: dict, search: str) -> bool:
        """Case-insensitive substring match over a message's readable fields.

        The SQL side of the inbox does this with ILIKE; sequence sends carry
        their copy in JSON metadata and are filtered in Python, so this keeps
        the two paths matching on the same fields.
        """
        needle = search.lower()
        return any(
            value and needle in value.lower()
            for value in (
                message.get("subject"),
                message.get("to_email"),
                message.get("from_email"),
                message.get("body_html"),
            )
        )

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
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        state_user_id: Optional[uuid.UUID] = None,
        label_id: Optional[uuid.UUID] = None,
        now: Optional[datetime] = None,
    ) -> dict:
        """Unified inbox query with filters.

        filter_type: all | sent | received | opened | needs_reply | priority |
            archived | snoozed
        user_gmail_emails: when provided, scope to only these email addresses
        gmail_account_id: when provided, scope to a single connected Gmail
            account (sends from it and replies received on it)
        account_email: when provided, scope to a single connected account by its
            address — sends *from* it and replies *to* it. Covers SMTP accounts
            too, which have no account-id FK on the message rows.
        search: optional full-text search on subject / from / to / body
        date_from / date_to: optional inclusive bounds on message timestamp
            (sent_at for sends, received_at for replies)
        state_user_id: the user whose per-thread archive state applies. Archived
            threads are excluded from every view except ``archived``, which shows
            only them. Snoozed threads (``snoozed_until`` still in the future)
            are hidden from every view except ``snoozed``, which shows only them
            — snoozing is auto-reversing, so a thread reappears the moment its
            snooze passes. When None (legacy/unscoped admin), no archive/snooze/
            label state is applied.
        label_id: when provided (with ``state_user_id``), scope to threads the
            user has tagged with this label.
        now: injectable "current time" for snooze comparison (tests). Defaults
            to ``datetime.now(timezone.utc)``.
        """
        now = now or datetime.now(timezone.utc)
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
        # Reverse index: email -> the connected Gmail account ids using it. Lets
        # us scope by the authoritative account-id FK, not just the message's
        # header address. This matters because a received reply's ``to_email``
        # is the "To" header, which for a connected mailbox is frequently *not*
        # the account's own address — shared aliases / distribution lists (a
        # prospect replies to ``team@`` that lands in the connected ``support@``
        # box), multiple recipients, or plus-addressing all diverge. Those
        # replies still carry the correct ``gmail_account_id``, so matching on
        # the FK keeps them in the inbox where address-only matching dropped them.
        ids_by_email: dict[str, list] = {}
        for acc_id, acc_email in account_email_map.items():
            if acc_email:
                ids_by_email.setdefault(acc_email.lower(), []).append(acc_id)

        def _account_ids_for(addrs) -> list:
            ids: list = []
            for a in addrs:
                ids.extend(ids_by_email.get(a, ()))
            return ids

        # Build sent messages query
        sends_query = select(EmailSend).where(
            EmailSend.status.notin_(NON_WIRE_STATUS_VALUES)
        )
        replies_query = select(EmailReply)
        # Sequence sends live on OutreachEvent, not EmailSend (see
        # ``_fetch_sequence_sends``). They are outbound, so they follow the
        # sends query: present in every view that shows sent mail, absent from
        # the received-only ones. ``seq_scopes`` accumulates the same mailbox
        # scoping the two SQL queries below apply.
        include_sequence_sends = True
        seq_scopes: list[tuple[set, set]] = []

        # Scope to user's accounts when authenticated. A message belongs to the
        # user if it carries one of their account-id FKs OR its header address is
        # one of theirs (the FK covers alias/multi-recipient Gmail replies; the
        # address covers SMTP mail, which carries no FK).
        if user_gmail_emails is not None and user_gmail_emails:
            user_account_ids = _account_ids_for(user_gmail_emails)
            seq_scopes.append((set(user_account_ids), set(user_gmail_emails)))
            sends_query = sends_query.where(
                or_(
                    EmailSend.sender_account_id.in_(user_account_ids),
                    func.lower(EmailSend.from_email).in_(user_gmail_emails),
                )
            )
            replies_query = replies_query.where(
                or_(
                    EmailReply.gmail_account_id.in_(user_account_ids),
                    func.lower(EmailReply.to_email).in_(user_gmail_emails),
                )
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
            seq_scopes.append(({gmail_account_id}, set()))

        # Scope to a single email account by address. Matches on the connected
        # account's id FK *or* the header address, for the same reason as the
        # user scope above: Gmail replies to a shared alias / with extra
        # recipients keep the right ``gmail_account_id`` but a divergent
        # ``to_email``, so an address-only filter silently hid them the moment a
        # user selected that account. SMTP sends/replies carry no id FK, so the
        # address arm still covers them. Mirrors ``danger_monitor``.
        if account_email:
            addr = account_email.lower()
            addr_account_ids = _account_ids_for([addr])
            sends_query = sends_query.where(
                or_(
                    EmailSend.sender_account_id.in_(addr_account_ids),
                    func.lower(EmailSend.from_email) == addr,
                )
            )
            replies_query = replies_query.where(
                or_(
                    EmailReply.gmail_account_id.in_(addr_account_ids),
                    func.lower(EmailReply.to_email) == addr,
                )
            )
            seq_scopes.append((set(addr_account_ids), {addr}))

        if filter_type == "sent":
            # Only sent emails
            replies_query = None
        elif filter_type == "received":
            # Only received — exclude replies from our own connected accounts
            sends_query = None
            include_sequence_sends = False
            if connected_emails:
                replies_query = replies_query.where(
                    ~func.lower(EmailReply.from_email).in_(connected_emails)
                )
        elif filter_type == "opened":
            sends_query = sends_query.where(EmailSend.opened_at.isnot(None))
            replies_query = None
            # Sequence sends carry no ``opened_at`` column — the open is a
            # sibling event — so they are narrowed after the engagement map is
            # assembled below.
        elif filter_type == "needs_reply":
            # Threads where the last message is a received email (needs reply)
            pass  # We'll filter after grouping
        elif filter_type == "priority":
            # Hot-lead view: threads carrying a priority-flagged reply. The flag
            # lives on incoming replies, so a thread is priority iff one of its
            # replies is flagged. We still load the thread's sends below so the
            # full conversation renders; the priority narrowing is applied after
            # grouping.
            if replies_query is not None:
                replies_query = replies_query.where(EmailReply.priority_flag.is_(True))
        # "archived" and "all" scan normally; archive membership is applied after
        # grouping via the per-user thread-state set.

        if search:
            search_term = contains_pattern(search)
            if sends_query is not None:
                # Sent copy lives in body_html (no separate plaintext column).
                sends_query = sends_query.where(
                    or_(
                        EmailSend.subject.ilike(search_term, escape=LIKE_ESCAPE),
                        EmailSend.to_email.ilike(search_term, escape=LIKE_ESCAPE),
                        EmailSend.from_email.ilike(search_term, escape=LIKE_ESCAPE),
                        EmailSend.body_html.ilike(search_term, escape=LIKE_ESCAPE),
                    )
                )
            if replies_query is not None:
                # Full-text: also match the reply body (plain + html), so a rep
                # searching for a phrase inside an email finds the thread.
                replies_query = replies_query.where(
                    or_(
                        EmailReply.subject.ilike(search_term, escape=LIKE_ESCAPE),
                        EmailReply.from_email.ilike(search_term, escape=LIKE_ESCAPE),
                        EmailReply.to_email.ilike(search_term, escape=LIKE_ESCAPE),
                        EmailReply.body_text.ilike(search_term, escape=LIKE_ESCAPE),
                        EmailReply.body_html.ilike(search_term, escape=LIKE_ESCAPE),
                    )
                )

        # Inclusive date-range bounds on the message timestamp.
        if date_from is not None:
            if sends_query is not None:
                sends_query = sends_query.where(EmailSend.sent_at >= date_from)
            if replies_query is not None:
                replies_query = replies_query.where(EmailReply.received_at >= date_from)
        if date_to is not None:
            if sends_query is not None:
                sends_query = sends_query.where(EmailSend.sent_at <= date_to)
            if replies_query is not None:
                replies_query = replies_query.where(EmailReply.received_at <= date_to)

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

        sends = (
            (await db.execute(sends_query)).scalars().all()
            if sends_query is not None
            else []
        )
        replies = (
            (await db.execute(replies_query)).scalars().all()
            if replies_query is not None
            else []
        )

        # Sequence sends — the outbound mail Dialtone's own sequences put on the
        # wire, which has no EmailSend row to be found by the query above.
        sequence_rows: list[tuple[OutreachEvent, OutreachEnrollment]] = []
        sequence_engagement: dict[tuple, dict] = {}
        if include_sequence_sends:
            sequence_rows = await self._fetch_sequence_sends(
                db,
                scopes=seq_scopes,
                date_from=date_from,
                date_to=date_to,
                limit=scan_cap,
            )
            sequence_engagement = await self._sequence_engagement(
                db, {enr.id for _, enr in sequence_rows}
            )
            if filter_type == "opened":
                sequence_rows = [
                    (event, enr)
                    for event, enr in sequence_rows
                    if sequence_engagement.get((enr.id, event.step_number), {}).get(
                        "opened_at"
                    )
                    is not None
                ]

        # Batch-load the contacts and companies referenced by this page of rows.
        # Inbox assembly used to be an N+1 — up to two queries per send and per
        # reply (contact, then company) — which with the scan cap of ~500 rows a
        # side meant ~2000 round-trips to render one inbox page. Collect the ids
        # up front and resolve them in two grouped queries, then build the
        # message dicts from the in-memory maps.
        contact_ids = {
            row.contact_id
            for row in (*sends, *replies, *(enr for _, enr in sequence_rows))
            if getattr(row, "contact_id", None)
        }
        contacts_by_id: dict[uuid.UUID, Contact] = {}
        if contact_ids:
            contact_rows = (
                await db.execute(select(Contact).where(Contact.id.in_(contact_ids)))
            ).scalars().all()
            contacts_by_id = {c.id: c for c in contact_rows}

        # Company ids come from two places: a reply may carry its own company_id,
        # and any contact (from a send or a reply) may point at a company.
        company_ids: set[uuid.UUID] = set()
        for r in replies:
            if r.company_id:
                company_ids.add(r.company_id)
        for c in contacts_by_id.values():
            if c.company_id:
                company_ids.add(c.company_id)
        company_name_by_id: dict[uuid.UUID, Optional[str]] = {}
        if company_ids:
            company_rows = (
                await db.execute(
                    select(Company.id, Company.name).where(Company.id.in_(company_ids))
                )
            ).all()
            company_name_by_id = {row[0]: row[1] for row in company_rows}

        for s in sends:
            contact = contacts_by_id.get(s.contact_id) if s.contact_id else None
            # Mirror _load_company_for_contact: name+id only when the contact
            # points at a company row that actually exists.
            company_name, cid = None, None
            if contact and contact.company_id in company_name_by_id:
                cid = contact.company_id
                company_name = company_name_by_id[cid]
            messages.append(
                self._send_to_message(s, contact, company_name, cid, account_email_map)
            )

        for r in replies:
            contact = contacts_by_id.get(r.contact_id) if r.contact_id else None
            # Reply's own company_id wins; else fall back to the contact's.
            company_name = None
            cid = r.company_id
            if cid:
                company_name = company_name_by_id.get(cid)
            elif contact and contact.company_id:
                cid = contact.company_id
                company_name = company_name_by_id.get(cid)
            messages.append(
                self._reply_to_message(
                    r, contact, company_name, cid, connected_emails, account_email_map
                )
            )

        for event, enrollment in sequence_rows:
            contact = (
                contacts_by_id.get(enrollment.contact_id)
                if enrollment.contact_id
                else None
            )
            company_name, cid = None, None
            if contact and contact.company_id in company_name_by_id:
                cid = contact.company_id
                company_name = company_name_by_id[cid]
            message = self._sequence_send_to_message(
                event,
                enrollment,
                contact,
                company_name,
                cid,
                account_email_map,
                sequence_engagement.get((enrollment.id, event.step_number)),
            )
            # The SQL arms of this query already applied ``search``; the
            # sequence arm can only be filtered once its JSON copy is unpacked.
            if search and not self._message_matches_search(message, search):
                continue
            messages.append(message)

        # Group into threads
        threads = self._group_into_threads(messages)

        # Per-user thread state: archive, snooze, and labels for these threads.
        archived_ids: set[str] = set()
        snoozed_until_by_id: dict[str, datetime] = {}
        labels_by_id: dict[str, list[dict]] = {}
        if state_user_id is not None:
            candidate_ids = [t["gmail_thread_id"] for t in threads]
            if candidate_ids:
                archived_ids, snoozed_until_by_id = await self._thread_state(
                    db, state_user_id, candidate_ids, now=now
                )
                labels_by_id = await self._labels_by_thread(
                    db, state_user_id, candidate_ids
                )
        for t in threads:
            tid = t["gmail_thread_id"]
            t["is_archived"] = tid in archived_ids
            snoozed_until = snoozed_until_by_id.get(tid)
            t["snoozed_until"] = snoozed_until
            t["labels"] = labels_by_id.get(tid, [])

        # Apply post-grouping filters BEFORE counting/paginating so ``total``
        # always equals the number of threads actually in the view (P0-4).
        if filter_type == "archived":
            # The Archived tab shows only archived threads.
            threads = [t for t in threads if t["is_archived"]]
        elif filter_type == "snoozed":
            # The Snoozed tab shows only threads still under an active snooze
            # (and not archived — archive wins).
            threads = [
                t for t in threads
                if t.get("snoozed_until") is not None and not t["is_archived"]
            ]
        else:
            # Every other view hides archived and currently-snoozed threads.
            threads = [
                t for t in threads
                if not t["is_archived"] and t.get("snoozed_until") is None
            ]

        # Label scope: keep only threads carrying the requested label.
        if label_id is not None:
            label_key = str(label_id)
            threads = [
                t for t in threads
                if any(str(l["id"]) == label_key for l in t.get("labels", []))
            ]

        if filter_type == "needs_reply":
            threads = [
                t for t in threads
                if t["messages"] and t["messages"][-1]["direction"] == "received"
            ]
        elif filter_type == "priority":
            threads = [t for t in threads if t["priority"]]

        # Sort by last activity
        threads.sort(key=lambda t: t["last_activity"], reverse=True)

        total = len(threads)
        start = (page - 1) * per_page
        end = start + per_page
        paginated = threads[start:end]

        return {"threads": paginated, "total": total, "page": page, "per_page": per_page}

    # ------------------------------------------------------------------
    # Read state + archive (per-user thread state)
    # ------------------------------------------------------------------

    async def _archived_thread_ids(
        self, db: AsyncSession, user_id: uuid.UUID, thread_ids: list[str]
    ) -> set[str]:
        """The subset of ``thread_ids`` this user has archived."""
        if not thread_ids:
            return set()
        rows = await db.execute(
            select(InboxThreadState.gmail_thread_id).where(
                InboxThreadState.user_id == user_id,
                InboxThreadState.is_archived.is_(True),
                InboxThreadState.gmail_thread_id.in_(thread_ids),
            )
        )
        return set(rows.scalars().all())

    async def _thread_state(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        thread_ids: list[str],
        now: Optional[datetime] = None,
    ) -> tuple[set[str], dict[str, datetime]]:
        """One-shot per-user archive + active-snooze state for ``thread_ids``.

        Returns ``(archived_ids, snoozed_until_by_id)`` where the snooze map only
        includes threads whose ``snoozed_until`` is still in the future — an
        elapsed snooze is treated as not-snoozed, which is what makes snoozing
        auto-reverse without a sweep.
        """
        if not thread_ids:
            return set(), {}
        now = now or datetime.now(timezone.utc)
        rows = (
            await db.execute(
                select(
                    InboxThreadState.gmail_thread_id,
                    InboxThreadState.is_archived,
                    InboxThreadState.snoozed_until,
                ).where(
                    InboxThreadState.user_id == user_id,
                    InboxThreadState.gmail_thread_id.in_(thread_ids),
                )
            )
        ).all()
        archived: set[str] = set()
        snoozed: dict[str, datetime] = {}
        for thread_id, is_archived, snoozed_until in rows:
            if is_archived:
                archived.add(thread_id)
            if snoozed_until is not None:
                # Normalize to aware UTC before comparing — SQLite hands back
                # naive datetimes even for a timezone=True column.
                su = snoozed_until
                if su.tzinfo is None:
                    su = su.replace(tzinfo=timezone.utc)
                if su > now:
                    snoozed[thread_id] = su
        return archived, snoozed

    async def _labels_by_thread(
        self, db: AsyncSession, user_id: uuid.UUID, thread_ids: list[str]
    ) -> dict[str, list[dict]]:
        """Map each thread id to the user's labels attached to it.

        A single grouped query joins the join-table to the label rows so the
        inbox can render label chips without an N+1.
        """
        if not thread_ids:
            return {}
        rows = (
            await db.execute(
                select(
                    InboxThreadLabel.gmail_thread_id,
                    InboxLabel.id,
                    InboxLabel.name,
                    InboxLabel.color,
                )
                .join(InboxLabel, InboxLabel.id == InboxThreadLabel.label_id)
                .where(
                    InboxThreadLabel.user_id == user_id,
                    InboxThreadLabel.gmail_thread_id.in_(thread_ids),
                )
                .order_by(InboxLabel.name)
            )
        ).all()
        by_thread: dict[str, list[dict]] = {}
        for thread_id, label_id, name, color in rows:
            by_thread.setdefault(thread_id, []).append(
                {"id": label_id, "name": name, "color": color}
            )
        return by_thread

    async def set_threads_snoozed(
        self,
        db: AsyncSession,
        thread_ids: list[str],
        snoozed_until: Optional[datetime],
        user_id: uuid.UUID,
    ) -> int:
        """Upsert the per-user snooze timestamp for each thread.

        ``snoozed_until=None`` un-snoozes (the thread returns to the default
        view immediately). Reuses the same ``inbox_thread_state`` row as archive
        so a thread carries one state row per user. Returns the count affected.
        """
        if not thread_ids:
            return 0
        existing = {
            s.gmail_thread_id: s
            for s in (
                await db.execute(
                    select(InboxThreadState).where(
                        InboxThreadState.user_id == user_id,
                        InboxThreadState.gmail_thread_id.in_(thread_ids),
                    )
                )
            ).scalars().all()
        }
        for thread_id in thread_ids:
            state = existing.get(thread_id)
            if state is None:
                state = InboxThreadState(
                    gmail_thread_id=thread_id,
                    user_id=user_id,
                    snoozed_until=snoozed_until,
                )
                db.add(state)
            else:
                state.snoozed_until = snoozed_until
        await db.commit()
        return len(thread_ids)

    # ------------------------------------------------------------------
    # Labels (per-user thread tags)
    # ------------------------------------------------------------------

    @staticmethod
    def _label_dict(label: InboxLabel) -> dict:
        return {
            "id": label.id,
            "name": label.name,
            "color": label.color,
            "created_at": label.created_at,
        }

    async def list_labels(self, db: AsyncSession, user_id: uuid.UUID) -> list[dict]:
        """All of a user's labels, alphabetical, each with its thread count."""
        labels = (
            await db.execute(
                select(InboxLabel)
                .where(InboxLabel.user_id == user_id)
                .order_by(InboxLabel.name)
            )
        ).scalars().all()
        counts = dict(
            (
                await db.execute(
                    select(
                        InboxThreadLabel.label_id, func.count()
                    )
                    .where(InboxThreadLabel.user_id == user_id)
                    .group_by(InboxThreadLabel.label_id)
                )
            ).all()
        )
        out = []
        for label in labels:
            d = self._label_dict(label)
            d["thread_count"] = int(counts.get(label.id, 0))
            out.append(d)
        return out

    async def create_label(
        self, db: AsyncSession, user_id: uuid.UUID, name: str, color: str = "gray"
    ) -> Optional[dict]:
        """Create a label for the user. Returns None on a duplicate name."""
        name = (name or "").strip()
        if not name:
            return None
        # Existence check. Nothing constrains (user_id, name) in the schema —
        # this function *is* the enforcement — so a duplicate that slipped past
        # it (a race between two creates) must still be reported as a duplicate
        # rather than raising.
        existing = (
            await db.execute(
                select(InboxLabel.id)
                .where(
                    InboxLabel.user_id == user_id,
                    func.lower(InboxLabel.name) == name.lower(),
                )
                .limit(1)
            )
        ).scalars().first()
        if existing is not None:
            return None
        label = InboxLabel(user_id=user_id, name=name, color=color or "gray")
        db.add(label)
        await db.commit()
        await db.refresh(label)
        d = self._label_dict(label)
        d["thread_count"] = 0
        return d

    async def update_label(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        label_id: uuid.UUID,
        name: Optional[str] = None,
        color: Optional[str] = None,
    ) -> Optional[dict]:
        """Rename / recolor a label the user owns. None if not found."""
        label = (
            await db.execute(
                select(InboxLabel).where(
                    InboxLabel.id == label_id, InboxLabel.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if label is None:
            return None
        if name is not None and name.strip():
            label.name = name.strip()
        if color is not None and color.strip():
            label.color = color.strip()
        await db.commit()
        await db.refresh(label)
        return self._label_dict(label)

    async def delete_label(
        self, db: AsyncSession, user_id: uuid.UUID, label_id: uuid.UUID
    ) -> bool:
        """Delete a user's label (its thread attachments cascade). False if
        the label doesn't belong to the user / doesn't exist."""
        label = (
            await db.execute(
                select(InboxLabel).where(
                    InboxLabel.id == label_id, InboxLabel.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if label is None:
            return False
        # Explicitly clear attachments rather than leaning on the DB-level
        # ON DELETE CASCADE — that FK only fires under Postgres with FK
        # enforcement on, so doing it here keeps behaviour identical everywhere.
        from sqlalchemy import delete as sa_delete

        await db.execute(
            sa_delete(InboxThreadLabel).where(InboxThreadLabel.label_id == label_id)
        )
        await db.delete(label)
        await db.commit()
        return True

    async def set_thread_label(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        gmail_thread_id: str,
        label_id: uuid.UUID,
        attached: bool,
    ) -> Optional[bool]:
        """Attach (or detach) a label to a thread for the user.

        Returns the resulting attached-state, or ``None`` if the label doesn't
        belong to the user (so the caller can 404). Idempotent both ways.
        """
        label = (
            await db.execute(
                select(InboxLabel.id).where(
                    InboxLabel.id == label_id, InboxLabel.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        if label is None:
            return None
        existing = (
            await db.execute(
                select(InboxThreadLabel).where(
                    InboxThreadLabel.user_id == user_id,
                    InboxThreadLabel.gmail_thread_id == gmail_thread_id,
                    InboxThreadLabel.label_id == label_id,
                )
            )
        ).scalar_one_or_none()
        if attached and existing is None:
            db.add(
                InboxThreadLabel(
                    gmail_thread_id=gmail_thread_id,
                    label_id=label_id,
                    user_id=user_id,
                )
            )
        elif not attached and existing is not None:
            await db.delete(existing)
        await db.commit()
        return attached

    async def set_threads_read(
        self,
        db: AsyncSession,
        thread_ids: list[str],
        is_read: bool,
        user_gmail_emails: Optional[set[str]] = None,
    ) -> int:
        """Flip ``is_read`` on every reply in the given threads.

        Scoped to the caller's own mailbox when ``user_gmail_emails`` is provided
        — only replies *received on* one of the user's addresses are touched, so
        a user can never mark another user's thread read/unread. Returns the
        number of reply rows updated. Idempotent (setting read→read is a no-op
        that still reports the row count in the thread).
        """
        if not thread_ids:
            return 0
        query = select(EmailReply).where(
            EmailReply.gmail_thread_id.in_(thread_ids)
        )
        if user_gmail_emails is not None:
            if not user_gmail_emails:
                return 0
            query = query.where(
                func.lower(EmailReply.to_email).in_(user_gmail_emails)
            )
        replies = (await db.execute(query)).scalars().all()
        for reply in replies:
            reply.is_read = is_read
        await db.commit()
        return len(replies)

    async def set_threads_archived(
        self,
        db: AsyncSession,
        thread_ids: list[str],
        is_archived: bool,
        user_id: uuid.UUID,
    ) -> int:
        """Upsert the per-user archive flag for each thread.

        Creates an ``inbox_thread_state`` row on first archive of a thread, and
        updates it thereafter. Returns the number of threads affected.
        """
        if not thread_ids:
            return 0
        existing = {
            s.gmail_thread_id: s
            for s in (
                await db.execute(
                    select(InboxThreadState).where(
                        InboxThreadState.user_id == user_id,
                        InboxThreadState.gmail_thread_id.in_(thread_ids),
                    )
                )
            ).scalars().all()
        }
        for thread_id in thread_ids:
            state = existing.get(thread_id)
            if state is None:
                state = InboxThreadState(
                    gmail_thread_id=thread_id,
                    user_id=user_id,
                    is_archived=is_archived,
                )
                db.add(state)
            else:
                state.is_archived = is_archived
        await db.commit()
        return len(thread_ids)

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    async def get_inbox_analytics(
        self,
        db: AsyncSession,
        user_gmail_emails: Optional[set[str]] = None,
        days: int = 30,
        now: Optional[datetime] = None,
    ) -> dict:
        """Mailbox analytics over a trailing window.

        Scoped like ``get_inbox`` (the caller's own mailboxes, or everything for
        the unscoped admin). Returns:

        * ``sent_count`` / ``received_count`` — message volume in the window.
        * ``unread_count`` — unread received replies right now (window-bounded).
        * ``needs_reply_count`` — threads whose latest message is inbound.
        * ``avg_response_seconds`` / ``median_response_seconds`` / ``responded_count``
          — how fast we replied to inbound messages that got a reply.
        * ``classification_breakdown`` — reply-intent category counts.
        * ``daily_volume`` — per-day sent/received counts for a sparkline.
        """
        now = now or datetime.now(timezone.utc)
        since = now - timedelta(days=max(1, days))

        # Resolve the account-id set for the user scope (mirrors get_inbox).
        account_email_map = await self._load_account_email_map(db)
        ids_by_email: dict[str, list] = {}
        for acc_id, acc_email in account_email_map.items():
            if acc_email:
                ids_by_email.setdefault(acc_email.lower(), []).append(acc_id)

        sends_query = select(EmailSend).where(
            EmailSend.status.notin_(NON_WIRE_STATUS_VALUES), EmailSend.sent_at >= since
        )
        replies_query = select(EmailReply).where(EmailReply.received_at >= since)

        # The same scope, in the shape _fetch_sequence_sends takes: a list of
        # (account_ids, from_addresses) pairs, all of which must hold. None means
        # the unscoped admin view, matching the queries above.
        seq_scopes: Optional[list[tuple[set, set]]] = None

        if user_gmail_emails is not None:
            if not user_gmail_emails:
                return self._empty_analytics(days)
            user_account_ids: list = []
            for a in user_gmail_emails:
                user_account_ids.extend(ids_by_email.get(a, ()))
            sends_query = sends_query.where(
                or_(
                    EmailSend.sender_account_id.in_(user_account_ids),
                    func.lower(EmailSend.from_email).in_(user_gmail_emails),
                )
            )
            replies_query = replies_query.where(
                or_(
                    EmailReply.gmail_account_id.in_(user_account_ids),
                    func.lower(EmailReply.to_email).in_(user_gmail_emails),
                )
            )
            seq_scopes = [(set(user_account_ids), set(user_gmail_emails))]

        sends = (await db.execute(sends_query)).scalars().all()
        replies = (await db.execute(replies_query)).scalars().all()

        # The other half of "what did we send?". The sequence engine records a
        # send as OutreachEvent(event_type='sent') and writes no EmailSend row,
        # and that path carries the bulk of Dialtone's outbound volume — so
        # counting EmailSend alone reported a fraction of the real number and
        # called it the total. Every other read surface in this service already
        # unions the two; this one did not, which is why the inbox tile and the
        # sparkline disagreed with the campaign dashboards.
        #
        # Same scope the send query above applies, expressed the way
        # _fetch_sequence_sends takes it: the sending account id, or the from
        # address, has to be one of the caller's.
        sequence_rows = await self._fetch_sequence_sends(
            db, scopes=seq_scopes, date_from=since, date_to=now
        )

        connected_emails = user_gmail_emails or {
            e.lower() for e in account_email_map.values() if e
        }

        # Build unified messages + group into threads (reuse existing helpers).
        messages = []
        for s in sends:
            messages.append(
                self._send_to_message(s, None, None, None, account_email_map)
            )
        for event, enrollment in sequence_rows:
            messages.append(
                self._sequence_send_to_message(
                    event, enrollment, None, None, None, account_email_map
                )
            )
        for r in replies:
            messages.append(
                self._reply_to_message(
                    r, None, None, None, connected_emails, account_email_map
                )
            )
        threads = self._group_into_threads(messages)

        sent_count = sum(1 for m in messages if m["direction"] == "sent")
        received_count = sum(1 for m in messages if m["direction"] == "received")
        unread_count = sum(
            1 for m in messages
            if m["direction"] == "received" and not m["is_read"]
        )

        needs_reply_count = sum(
            1 for t in threads
            if t["messages"] and t["messages"][-1]["direction"] == "received"
        )

        # Response times: for each inbound message that is followed later in its
        # thread by an outbound, the gap to that first outbound is a response.
        response_deltas: list[float] = []
        for t in threads:
            msgs = t["messages"]
            for i, m in enumerate(msgs):
                if m["direction"] != "received":
                    continue
                for later in msgs[i + 1:]:
                    if later["direction"] == "sent":
                        d = (later["timestamp"] - m["timestamp"]).total_seconds()
                        if d >= 0:
                            response_deltas.append(d)
                        break
        responded_count = len(response_deltas)
        avg_response_seconds = (
            sum(response_deltas) / responded_count if responded_count else None
        )
        median_response_seconds = None
        if responded_count:
            ordered = sorted(response_deltas)
            mid = responded_count // 2
            median_response_seconds = (
                ordered[mid]
                if responded_count % 2
                else (ordered[mid - 1] + ordered[mid]) / 2
            )

        classification_breakdown: dict[str, int] = {}
        for r in replies:
            cat = r.reply_classification
            if cat:
                classification_breakdown[cat] = classification_breakdown.get(cat, 0) + 1

        # Per-day sent/received for the sparkline (UTC calendar days).
        daily: dict[str, dict[str, int]] = {}
        for m in messages:
            day = m["timestamp"].astimezone(timezone.utc).date().isoformat()
            bucket = daily.setdefault(day, {"sent": 0, "received": 0})
            bucket[m["direction"]] = bucket.get(m["direction"], 0) + 1
        daily_volume = [
            {"date": day, "sent": v["sent"], "received": v["received"]}
            for day, v in sorted(daily.items())
        ]

        return {
            "window_days": days,
            "sent_count": sent_count,
            "received_count": received_count,
            "unread_count": unread_count,
            "needs_reply_count": needs_reply_count,
            "responded_count": responded_count,
            "avg_response_seconds": avg_response_seconds,
            "median_response_seconds": median_response_seconds,
            "classification_breakdown": classification_breakdown,
            "daily_volume": daily_volume,
        }

    @staticmethod
    def _empty_analytics(days: int) -> dict:
        return {
            "window_days": days,
            "sent_count": 0,
            "received_count": 0,
            "unread_count": 0,
            "needs_reply_count": 0,
            "responded_count": 0,
            "avg_response_seconds": None,
            "median_response_seconds": None,
            "classification_breakdown": {},
            "daily_volume": [],
        }

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

        # Sequence sends in this thread. Without these a thread opened from the
        # inbox showed only the prospect's reply — the outreach it replied to
        # was missing, because the sequence path records no EmailSend row.
        sequence_rows = await self._fetch_sequence_sends(
            db, gmail_thread_id=gmail_thread_id
        )
        engagement = await self._sequence_engagement(
            db, {enr.id for _, enr in sequence_rows}
        )
        for event, enrollment in sequence_rows:
            contact = await self._load_contact(db, enrollment.contact_id)
            company_name, cid = await self._load_company_for_contact(db, contact)
            if company_name and not lead_name:
                lead_name = company_name
                lead_id = cid
            messages.append(
                self._sequence_send_to_message(
                    event,
                    enrollment,
                    contact,
                    company_name,
                    cid,
                    account_email_map,
                    engagement.get((enrollment.id, event.step_number)),
                )
            )

        # Drop any sequence send already represented by an EmailSend / EmailReply
        # row for the same Gmail message (mirrors ``_group_into_threads``, which
        # this single-thread path bypasses).
        deduped: list[dict] = []
        seen_message_ids: set[str] = set()
        for message in messages:
            message_id = message.get("gmail_message_id")
            if message_id:
                if message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
            deduped.append(message)
        messages = deduped

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

        # The do-not-contact list applies to a human hitting Reply too. This
        # composer holds a GmailSender directly, so it does not inherit the gate
        # inside send_outreach_email — and "a person typed it" is not one of the
        # exemptions, because it is precisely how a suppressed address gets
        # mailed in practice. Reported rather than swallowed: the operator sees
        # why, and removing the suppression entry is the way to override it.
        from app.services import email_dispatch

        blocked = await email_dispatch.check_send_allowed(
            db, to_email, check_kill_switch=False
        )
        if blocked is not None:
            return {"status": "error", "detail": blocked["reason"]}

        # The volume floors count this send but never refuse it. A person
        # hitting Reply to someone who wrote to them is the mail most worth
        # sending, and holding it because the *cold outreach* cap is spent would
        # be a worse failure than the cap prevents. It still leaves the same
        # mailbox, so it depletes the day and the mailbox's warm-up allowance —
        # cold volume yields to a live conversation, which is the priority the
        # cap would want if it could express one.
        slot = await email_dispatch.claim_send_volume(db, account, refusable=False)

        # Send via Gmail
        sender = GmailSender(account)
        try:
            send_result = sender.send_email(
                to_email=to_email,
                subject=reply_subject,
                body_html=body_html,
                body_text=body_text,
                thread_id=effective_thread_id,
                in_reply_to=in_reply_to,
                references=references,
            )
        except Exception:
            await email_dispatch.settle_send_volume(db, slot, sent=False)
            raise

        landed = send_result.get("status") == "sent"
        await email_dispatch.settle_send_volume(db, slot, sent=landed)
        await email_dispatch.record_account_send(db, account, sent=landed)

        if not landed:
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
            service = await asyncio.to_thread(self._build_gmail_service, account)
            thread = await self._gexec(
                service.users()
                .threads()
                .get(userId="me", id=gmail_thread_id, format="full")
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
            # Classification/priority live only on incoming replies.
            "reply_classification": None,
            "priority_flag": False,
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
            # Intent classification + hot-lead marker (surfaced as chips / the
            # Priority tab). Only meaningful on the "received" direction.
            "reply_classification": reply.reply_classification if direction == "received" else None,
            "priority_flag": bool(reply.priority_flag) if direction == "received" else False,
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

            # Thread-level intent surface: hot if any message is flagged; the
            # thread's classification is the latest received message's category
            # (that's the reply a rep is triaging against).
            priority = any(m.get("priority_flag") for m in thread_messages)
            classification = None
            for m in reversed(thread_messages):
                if m.get("direction") == "received" and m.get("reply_classification"):
                    classification = m["reply_classification"]
                    break

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
                "priority": priority,
                "classification": classification,
                # Overwritten with the caller's per-user state in get_inbox; the
                # default (no state row) is not-archived, not-snoozed, unlabeled.
                "is_archived": False,
                "snoozed_until": None,
                "labels": [],
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
