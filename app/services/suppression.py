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


def normalize_email(email: Optional[str]) -> Optional[str]:
    """Trim + lower-case an address, or ``None`` if it isn't email-shaped."""
    if not email:
        return None
    cleaned = email.strip().lower()
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
