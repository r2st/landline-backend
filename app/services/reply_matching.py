"""Shared enrollment-matching for inbound replies (reply-detection breadth).

Both reply paths (:mod:`app.services.reply_tracker` and
:mod:`app.services.smart_reply`) need to answer the same question when a prospect
writes back: *which enrollment does this stop?* Two failure modes lost replies:

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


def _message_id_tokens(*header_values: Optional[str]) -> list[str]:
    """Extract candidate id strings from one or more reply headers.

    For each ``<...>`` token we keep both the full inner string and its local
    part (before ``@``), because the send path stores the bare Gmail id while the
    RFC message-id it threads under is ``<{gmail_id}@mail.gmail.com>``.
    """
    tokens: list[str] = []
    for value in header_values:
        if not value:
            continue
        for match in _MSGID_RE.findall(value):
            inner = match.strip()
            if not inner:
                continue
            tokens.append(inner)
            local = inner.split("@", 1)[0]
            if local and local != inner:
                tokens.append(local)
    # De-dup, preserve order.
    seen: set = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


async def find_stoppable_enrollment(
    db: AsyncSession, contact_id: UUID
) -> Optional[OutreachEnrollment]:
    """The enrollment a reply from this contact should stop, or None.

    Broadened beyond ``active``: prefers a still-running enrollment (active, then
    pending_review) and otherwise takes the most recent worked one, so a reply is
    never dropped just because the sequence had moved on.
    """
    priority = case(
        (OutreachEnrollment.status == "active", 0),
        (OutreachEnrollment.status == "pending_review", 1),
        else_=2,
    )
    return (
        await db.execute(
            select(OutreachEnrollment)
            .where(
                OutreachEnrollment.contact_id == contact_id,
                OutreachEnrollment.status.in_(REPLY_STOPPABLE_STATUSES),
            )
            .order_by(priority, OutreachEnrollment.enrolled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def match_enrollment_by_headers(
    db: AsyncSession,
    in_reply_to: Optional[str],
    references: Optional[str],
) -> Optional[OutreachEnrollment]:
    """Fallback: match a reply to an enrollment by its threading headers.

    Matches the reply's ``In-Reply-To`` / ``References`` message-ids against the
    Gmail ``gmail_message_id`` and ``gmail_thread_id`` the send path recorded on
    the enrollment. Returns the matched enrollment (only if it is still
    reply-stoppable) or None. Used when address-based matching finds nothing.
    """
    tokens = _message_id_tokens(in_reply_to, references)
    if not tokens:
        return None

    enrollment = (
        await db.execute(
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
    ).scalar_one_or_none()
    if enrollment is not None:
        logger.info(
            "Reply matched enrollment %s by threading headers (%d token(s))",
            enrollment.id, len(tokens),
        )
    return enrollment
