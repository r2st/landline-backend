"""Suppression-list helpers (Feature 2).

The single source of truth for "is this address on the do-not-contact list?" plus
the bulk CSV import used by the compliance upload. All reads/writes are org-scoped
by the global mixin filter, so callers never pass an org.
"""

import csv
import io
import logging
import re
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.suppression import (
    SUPPRESSION_REASONS,
    SUPPRESSION_SOURCES,
    SuppressionEntry,
)
from app.services.sql_search import LIKE_ESCAPE, contains_pattern

logger = logging.getLogger(__name__)

# Deliberately permissive — the list may hold addresses we never validated. Good
# enough to reject obvious junk (no ``@``, whitespace) without dropping real,
# unusual addresses from a compliance upload.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# The width of ``suppression_entries.email`` (and of every other address column
# in the schema). The regex above matches an address of any length, so without
# this a 400-character line in an upload is "valid" right up until the INSERT,
# where Postgres raises ``StringDataRightTruncation``.
#
# That is the wrong place for this to fail. ``import_csv`` batches the whole
# upload into one transaction and commits once, so the error does not cost the
# operator the bad row — it costs them **every** row: 150k addresses parsed,
# inserted and rolled back, and a 500 in place of the per-row ``invalid`` count
# the endpoint exists to return. A do-not-contact list silently not applying is
# the one outcome this module must not have, and a single junk line in a
# third-party compliance export is an ordinary way to get one.
#
# 320 rather than the RFC 5321 maximum of 254: the column is what actually
# constrains the write, and narrowing the rule here would start rejecting
# addresses the database would happily have stored.
MAX_EMAIL_LENGTH = 320


def normalize_email(email: Optional[str]) -> Optional[str]:
    """Trim + lower-case an address, or ``None`` if it isn't email-shaped.

    "Email-shaped" includes fitting in the column: an address longer than
    :data:`MAX_EMAIL_LENGTH` is rejected here, as a row-level ``invalid``,
    rather than at flush time as a failed transaction.
    """
    if not email:
        return None
    cleaned = email.strip().lower()
    if len(cleaned) > MAX_EMAIL_LENGTH:
        return None
    return cleaned if _EMAIL_RE.match(cleaned) else None


async def is_suppressed(db: AsyncSession, email: Optional[str]) -> bool:
    """Whether ``email`` is on the current org's suppression list."""
    normalized = normalize_email(email)
    if normalized is None:
        return False
    hit = (
        await db.execute(
            select(SuppressionEntry.id).where(SuppressionEntry.email == normalized).limit(1)
        )
    ).first()
    return hit is not None


async def suppressed_addresses(db: AsyncSession, emails) -> set[str]:
    """The subset of ``emails`` on the current org's list, normalized.

    The batch form of :func:`is_suppressed`, for callers holding a page of
    contacts. One query rather than one per contact — the bulk-enrol endpoint
    takes up to a full page of ids, and a per-row existence check there is the
    N+1 the rest of that handler goes out of its way to avoid.
    """
    normalized = {n for n in (normalize_email(e) for e in emails) if n}
    if not normalized:
        return set()
    rows = (
        await db.execute(
            select(SuppressionEntry.email).where(SuppressionEntry.email.in_(normalized))
        )
    ).scalars().all()
    return set(rows)


# --- The do-not-contact test -------------------------------------------------

# What the send gate asks, in the words it answers with. See
# ``email_dispatch.check_send_allowed`` and the queue sweep in
# ``tasks/outreach.py``: a contact is mailable unless the per-contact
# ``suppressed`` flag, the legacy ``unsubscribed`` flag, or the org
# do-not-contact LIST says otherwise. Three sources, not two.
NO_EMAIL = "Contact has no email address"
UNSUBSCRIBED = "Contact has unsubscribed"
SUPPRESSED = "Contact is suppressed"
ON_LIST = "Contact's address is on the do-not-contact list"


def blocking_flag(contact) -> Optional[str]:
    """The contact-row half of the do-not-contact test, or ``None`` if clear.

    Split out from :func:`blocking_reason` for callers holding a batch: the
    flags are already loaded, so filtering on them costs nothing and shrinks the
    set of addresses the list query has to ask about.
    """
    if not (getattr(contact, "email", None) or "").strip():
        return NO_EMAIL
    if getattr(contact, "unsubscribed", False):
        return UNSUBSCRIBED
    if getattr(contact, "suppressed", False):
        return SUPPRESSED
    return None


async def blocking_reason(db: AsyncSession, contact) -> Optional[str]:
    """Why ``contact`` must not be mailed, or ``None`` if they may be.

    **The enrollment side of the compliance gate, and the reason it is one
    function.** ``check_send_allowed`` is the floor and stops the message; what
    it cannot stop is the *enrollment*, and six paths created those with six
    different ideas of who is mailable. Only ``autonomous_prospector`` asked all
    three questions. ``signal_enrollment`` and the bulk-enrol endpoint read the
    two flags and not the list; the two single-enrol endpoints and
    ``social_listening_agent`` asked nothing at all.

    The list is the clause that gets dropped, and it is the one that cannot be
    reconstructed from the contact row. ``add_entry`` does not touch
    ``Contact.suppressed`` — deliberately, because an address outlives every row
    that carries it — so a compliance CSV upload, a bounce recorded against a
    duplicate row, or an erasure whose subject the scraper later rediscovered
    all leave a contact whose flags are clean and whose address must never be
    mailed. That last one is the case ``gdpr.erase_subject`` writes the entry
    *for*: erasure anonymises the contact row, so the list is the only surviving
    link between the person and the decision.

    Nothing leaked, because the send gate holds. What it cost is everything up
    to it: a live enrollment for someone who opted out, dispatched and paid for,
    counted as outreach on a campaign that can never mail it, and — in
    ``social_listening_agent`` — a company advanced to ``pipeline_stage=
    'outreach'`` on the strength of it.

    Returns the operator-facing reason so the bulk endpoint can report which
    clause refused, rather than a bare False.
    """
    reason = blocking_flag(contact)
    if reason is not None:
        return reason
    if await is_suppressed(db, contact.email):
        return ON_LIST
    return None


async def is_mailable(db: AsyncSession, contact) -> bool:
    """Whether ``contact`` may be enrolled/mailed. See :func:`blocking_reason`."""
    return await blocking_reason(db, contact) is None


async def address_blocking_reason(db: AsyncSession, email: Optional[str]) -> Optional[str]:
    """The do-not-contact test for a bare **address**, or ``None`` if clear.

    :func:`blocking_reason` is the version for a caller holding a ``Contact``.
    This is the version for a caller holding only the string it is about to put
    in the ``To:`` header — which is what every *send* path has, since the send
    gate takes an address and not a row.

    **It asks all three questions, and the send gate asked one.**
    ``email_dispatch.check_send_allowed`` calls itself "the two compliance
    floors, in one place" and consulted the kill switch and the LIST. The two
    contact flags it did not read, while the sequence path
    (``send_sequence_step``) reads all three and says in its own comment why:
    *"three writers have set this column alone at various times"*. Rows carrying
    ``unsubscribed=True`` with no list entry are in the database now — that is
    the whole reason the sequence path keeps the redundant read — and every path
    that is not the sequence engine is gated by ``check_send_allowed`` alone.

    So the opt-out that stopped a cadence did not stop the smart-reply
    auto-send, a meeting proposal, a reminder, an inbox reply or a test send to
    the same person. Same shape as the gate's own origin story, one level down:
    the floor was consolidated and one of its clauses was left behind in the
    caller it was consolidated out of.

    Matching is by **address**, case-insensitively, across every row carrying
    it, because ``contacts.email`` has no unique constraint and the scraper
    re-creates rows. One flagged duplicate is enough: the flag is a fact about
    the person, and which row the send path happened to load is not.
    """
    normalized = normalize_email(email)
    if normalized is None:
        # Not email-shaped. Nothing to look up, and the transport will refuse it
        # anyway — this gate is not the place to decide an address is malformed.
        return None

    from app.models.contact import Contact

    flagged = (
        await db.execute(
            select(Contact.unsubscribed, Contact.suppressed)
            .where(func.lower(Contact.email) == normalized)
            .where(
                (Contact.unsubscribed.is_(True)) | (Contact.suppressed.is_(True))
            )
            .limit(1)
        )
    ).first()
    if flagged is not None:
        return UNSUBSCRIBED if flagged.unsubscribed else SUPPRESSED

    if await is_suppressed(db, normalized):
        return ON_LIST
    return None


# Entries an operator's un-suppress may lift. A ``manual`` entry records
# somebody's decision and can be reversed by another decision. The rest record
# something that *happened* to the address — it bounced, its owner reported us
# as spam, its owner asked us to stop, its owner invoked erasure — and none of
# those become untrue because a checkbox was un-ticked on one contact row.
REVERSIBLE_REASONS = ("manual",)


async def remove_entry(db: AsyncSession, email: Optional[str]) -> bool:
    """Lift an operator-placed suppression for ``email``. True if one was lifted.

    Deliberately narrow. Un-suppressing a contact has to actually restore
    mailability — otherwise the flag comes off, the list entry stays, the send
    gate keeps refusing and the operator gets silence with no explanation. But it
    must not become a way to erase a complaint or an erasure record by round-
    tripping a checkbox, so only ``REVERSIBLE_REASONS`` are touched. Removing one
    of the others stays an explicit, per-entry operator action against the
    suppression list itself.
    """
    normalized = normalize_email(email)
    if normalized is None:
        return False
    entry = (
        await db.execute(
            select(SuppressionEntry).where(
                SuppressionEntry.email == normalized,
                SuppressionEntry.reason.in_(REVERSIBLE_REASONS),
            )
        )
    ).scalars().first()
    if entry is None:
        return False
    await db.delete(entry)
    await db.flush()
    return True


async def add_entry(
    db: AsyncSession,
    email: str,
    *,
    reason: str = "manual",
    source: str = "manual",
) -> tuple[Optional[SuppressionEntry], str]:
    """Add one address to the suppression list (idempotent per org).

    Returns ``(entry, status)`` where status is ``added``, ``duplicate`` (already
    suppressed), or ``invalid`` (not an email). Never raises on a bad row so a
    bulk import can report per-row outcomes.
    """
    normalized = normalize_email(email)
    if normalized is None:
        return None, "invalid"
    if reason not in SUPPRESSION_REASONS:
        reason = "manual"
    if source not in SUPPRESSION_SOURCES:
        source = "manual"

    existing = (
        await db.execute(
            select(SuppressionEntry).where(SuppressionEntry.email == normalized)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, "duplicate"

    entry = SuppressionEntry(email=normalized, reason=reason, source=source)
    db.add(entry)
    await db.flush()
    return entry, "added"


def _extract_emails(content: str) -> list[tuple[str, Optional[str]]]:
    """Parse CSV/plain text into ``(email, reason?)`` rows.

    Handles three shapes without configuration:

    * a header row naming an ``email`` (and optional ``reason``) column;
    * headerless CSV — the first column is the email, an optional second is the
      reason;
    * a plain newline-separated list of addresses.

    Reason values that aren't a known reason are dropped (the caller's default
    applies instead).
    """
    rows: list[tuple[str, Optional[str]]] = []
    reader = csv.reader(io.StringIO(content))
    records = [r for r in reader if any((c or "").strip() for c in r)]
    if not records:
        return rows

    email_idx, reason_idx = 0, None
    header = [c.strip().lower() for c in records[0]]
    if "email" in header:
        email_idx = header.index("email")
        if "reason" in header:
            reason_idx = header.index("reason")
        records = records[1:]  # skip the header

    for record in records:
        if email_idx >= len(record):
            continue
        email = record[email_idx]
        reason = None
        if reason_idx is not None and reason_idx < len(record):
            candidate = (record[reason_idx] or "").strip().lower()
            if candidate in SUPPRESSION_REASONS:
                reason = candidate
        rows.append((email, reason))
    return rows


# Addresses per round trip during a bulk import. Big enough that a full-size
# upload is a few hundred statements instead of a few hundred thousand; small
# enough that the ``IN (...)`` list stays well inside any parameter limit.
_IMPORT_BATCH = 500


async def import_csv(
    db: AsyncSession, content: str, *, default_reason: str = "unsubscribe"
) -> dict:
    """Bulk-import a CSV/text upload into the suppression list.

    Returns a summary: ``{added, duplicates, invalid, total}``. Deduplicates
    within the file too, so a repeated address in the upload counts once.

    Batched rather than row-at-a-time. The upload cap is 5 MB, which is roughly
    150k addresses, and the per-row path was a ``SELECT`` plus a ``flush`` each —
    300k sequential round trips inside one request, holding a worker and a
    database connection for minutes. The failure that produces is worse than
    slow: the proxy times the request out, the transaction that was never
    committed rolls back, and the operator is told nothing while *none* of their
    compliance list landed. A do-not-contact upload silently not applying is the
    one outcome this endpoint must not have.
    """
    if default_reason not in SUPPRESSION_REASONS:
        default_reason = "unsubscribe"

    added = duplicates = invalid = 0
    seen_in_file: set[str] = set()

    # Parse and validate the whole file first; the database work is uniform after
    # this, which is what makes it batchable.
    pending: list[tuple[str, str]] = []
    for raw_email, row_reason in _extract_emails(content):
        normalized = normalize_email(raw_email)
        if normalized is None:
            invalid += 1
            continue
        if normalized in seen_in_file:
            duplicates += 1
            continue
        seen_in_file.add(normalized)
        reason = row_reason or default_reason
        if reason not in SUPPRESSION_REASONS:
            reason = "manual"
        pending.append((normalized, reason))

    for start in range(0, len(pending), _IMPORT_BATCH):
        batch = pending[start : start + _IMPORT_BATCH]
        emails = [email for email, _ in batch]
        # One read per batch. Org-scoped by the global filter, like every other
        # read here — a foreign org's entry for the same address is not a
        # duplicate of ours.
        existing = set(
            (
                await db.execute(
                    select(SuppressionEntry.email).where(
                        SuppressionEntry.email.in_(emails)
                    )
                )
            ).scalars().all()
        )
        fresh = [
            SuppressionEntry(email=email, reason=reason, source="import")
            for email, reason in batch
            if email not in existing
        ]
        duplicates += len(batch) - len(fresh)
        if fresh:
            db.add_all(fresh)
            # One flush per batch, so the ORM emits a single multi-row INSERT
            # rather than one statement per address.
            await db.flush()
            added += len(fresh)

    await db.commit()
    total = added + duplicates + invalid
    logger.info(
        "Suppression import: %d added, %d duplicates, %d invalid (%d rows)",
        added, duplicates, invalid, total,
    )
    return {"added": added, "duplicates": duplicates, "invalid": invalid, "total": total}


async def count_entries(db: AsyncSession, q: Optional[str] = None) -> int:
    """Total suppression entries (optionally filtered by an email substring)."""
    query = select(func.count(SuppressionEntry.id))
    if q:
        query = query.where(
            SuppressionEntry.email.ilike(
                contains_pattern(q.strip().lower()), escape=LIKE_ESCAPE
            )
        )
    return (await db.execute(query)).scalar() or 0
