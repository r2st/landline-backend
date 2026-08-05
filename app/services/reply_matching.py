"""Shared enrollment-matching for inbound replies (reply-detection breadth).

The reply path (:mod:`app.services.smart_reply`, and the ingest gate in
:mod:`app.services.email_history`) needs to answer the same question when a
prospect writes back: *which enrollment does this stop?* Two failure modes lost
replies:

1. **Status too narrow.** They only stopped ``status == "active"`` enrollments.
   A reply that lands while a step is parked for review (``pending_review``),
   after the sequence finished (``completed``), or while paused was silently
   ignored — so a "no thanks" arrived, the enrollment never recorded it, and the
   next step (or a re-activation) kept mailing. :data:`REPLY_STOPPABLE_STATUSES`
   widens the net; :func:`find_stoppable_enrollment` prefers the still-running
   one when a contact has several.

2. **Address matching only.** Matching a reply to a send purely by ``From:``
   address misses threaded replies from an alias / a different mailbox on the
   same thread. :func:`match_enrollment_by_headers` recovers those by matching
   the reply's ``In-Reply-To`` / ``References`` message-ids against the Gmail
   thread/message ids the send path stored on the enrollment (the same ids the
   ``outreach_events`` send rows carry) — the strongest possible signal that a
   message belongs to a thread, independent of who sent it.
"""

import logging
import re
from typing import Optional
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment

logger = logging.getLogger(__name__)

# Statuses a reply should stop / annotate. 'active' + 'pending_review' are still
# in-flight; 'completed'/'paused' are worked but a late reply still matters (it
# advances the company stage and must never be re-mailed). 'bounced' /
# 'unsubscribed' / 'replied' are terminal-and-irrelevant and deliberately left
# out — re-marking them adds nothing.
REPLY_STOPPABLE_STATUSES = ("active", "pending_review", "completed", "paused")

# A message-id token inside an In-Reply-To / References header, e.g.
# ``<CAEfid_abc@mail.gmail.com>``.
_MSGID_RE = re.compile(r"<([^>]+)>")

# How many ids one reply may contribute to the lookup.
#
# ``References`` is written by the *sender* of the inbound message — it is
# attacker-controlled input arriving from the open internet, and nothing between
# the mail server and here bounds it. Every token becomes two bind parameters in
# an ``IN`` clause (the full id and its local part, against two columns), and
# psycopg refuses a statement with more than 65535 of them: a single crafted
# reply with a few thousand ids would raise inside the sweep and take down
# ingestion for the *whole batch* it arrived in, which is the failure mode a
# mailbox cannot recover from on its own — no replies at all reads exactly like
# no replies.
#
# The cap costs nothing real. A genuine ``References`` chain is the thread's
# history, RFC 5322 encourages trimming it, and we match against ids *we* wrote:
# the one that threads a reply to our send is the most recent, and the newest
# entries are at the end.
_MAX_MSGID_TOKENS = 64


def _message_id_tokens(*header_values: Optional[str]) -> list[str]:
    """Extract candidate id strings from one or more reply headers.

    For each ``<...>`` token we keep both the full inner string and its local
    part (before ``@``), because the send path stores the bare Gmail id while the
    RFC message-id it threads under is ``<{gmail_id}@mail.gmail.com>``.

    Bounded at :data:`_MAX_MSGID_TOKENS`. **What is dropped matters**: callers
    pass ``In-Reply-To`` first and it is the *direct parent* — the single id most
    likely to be one of ours — so a plain tail-slice of the combined list would
    discard exactly the token worth keeping. Each header is trimmed on its own,
    earlier arguments keep their tokens whole, and only the surplus header is
    cut, from its **head**: a ``References`` chain runs oldest to newest, so its
    newest entries are the ones adjacent to our send.
    """
    per_header: list[list[str]] = []
    seen: set = set()
    for value in header_values:
        found: list[str] = []
        for match in _MSGID_RE.findall(value or ""):
            inner = match.strip()
            if not inner:
                continue
            for token in (inner, inner.split("@", 1)[0]):
                if token and token not in seen:
                    seen.add(token)
                    found.append(token)
        per_header.append(found)

    total = sum(len(f) for f in per_header)
    if total <= _MAX_MSGID_TOKENS:
        return [t for header in per_header for t in header]

    logger.info(
        "Reply carried %d threading ids; matching against at most %d",
        total, _MAX_MSGID_TOKENS,
    )
    out: list[str] = []
    remaining = _MAX_MSGID_TOKENS
    for header in per_header:
        if remaining <= 0:
            break
        keep = min(len(header), remaining)
        out.extend(header[-keep:])
        remaining -= keep
    return out


async def find_stoppable_enrollment(
    db: AsyncSession, contact_id: UUID, org_id: Optional[UUID] = None
) -> Optional[OutreachEnrollment]:
    """The enrollment a reply from this contact should stop, or None.

    Broadened beyond ``active``: prefers a still-running enrollment (active, then
    pending_review) and otherwise takes the most recent worked one, so a reply is
    never dropped just because the sequence had moved on.

    ``org_id`` scopes the match to a single tenant. Pass the owning account's
    org so the matcher can never reach across organizations even when it runs
    without a bound request context (e.g. the manual smart-reply endpoint), in
    addition to the global org read filter.
    """
    priority = case(
        (OutreachEnrollment.status == "active", 0),
        (OutreachEnrollment.status == "pending_review", 1),
        else_=2,
    )
    query = (
        select(OutreachEnrollment)
        .where(
            OutreachEnrollment.contact_id == contact_id,
            OutreachEnrollment.status.in_(REPLY_STOPPABLE_STATUSES),
        )
        .order_by(priority, OutreachEnrollment.enrolled_at.desc())
        .limit(1)
    )
    if org_id is not None:
        query = query.where(OutreachEnrollment.org_id == org_id)
    return (await db.execute(query)).scalar_one_or_none()


async def match_enrollment_by_headers(
    db: AsyncSession,
    in_reply_to: Optional[str],
    references: Optional[str],
    org_id: Optional[UUID] = None,
) -> Optional[OutreachEnrollment]:
    """Fallback: match a reply to an enrollment by its threading headers.

    Matches the reply's ``In-Reply-To`` / ``References`` message-ids against the
    Gmail ``gmail_message_id`` and ``gmail_thread_id`` the send path recorded on
    the enrollment. Returns the matched enrollment (only if it is still
    reply-stoppable) or None. Used when address-based matching finds nothing.

    ``org_id`` scopes the match to a single tenant. Threading-header ids are not
    guaranteed unique across organizations, so passing the owning account's org
    prevents a reply on one tenant from ever matching (and stopping) another
    tenant's enrollment — a cross-tenant leak the global read filter would only
    catch when a request context is bound.
    """
    tokens = _message_id_tokens(in_reply_to, references)
    if not tokens:
        return None

    query = (
        select(OutreachEnrollment)
        .where(
            OutreachEnrollment.status.in_(REPLY_STOPPABLE_STATUSES),
            (
                OutreachEnrollment.gmail_message_id.in_(tokens)
                | OutreachEnrollment.gmail_thread_id.in_(tokens)
            ),
        )
        .order_by(OutreachEnrollment.enrolled_at.desc())
        .limit(1)
    )
    if org_id is not None:
        query = query.where(OutreachEnrollment.org_id == org_id)
    enrollment = (await db.execute(query)).scalar_one_or_none()
    if enrollment is not None:
        logger.info(
            "Reply matched enrollment %s by threading headers (%d token(s))",
            enrollment.id, len(tokens),
        )
    return enrollment
