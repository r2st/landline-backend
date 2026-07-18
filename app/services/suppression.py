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


async def import_csv(
    db: AsyncSession, content: str, *, default_reason: str = "unsubscribe"
) -> dict:
    """Bulk-import a CSV/text upload into the suppression list.

    Returns a summary: ``{added, duplicates, invalid, total}``. Deduplicates
    within the file too, so a repeated address in the upload counts once.
    """
    if default_reason not in SUPPRESSION_REASONS:
        default_reason = "unsubscribe"

    added = duplicates = invalid = 0
    seen_in_file: set[str] = set()

    for raw_email, row_reason in _extract_emails(content):
        normalized = normalize_email(raw_email)
        if normalized is None:
            invalid += 1
            continue
        if normalized in seen_in_file:
            duplicates += 1
            continue
        seen_in_file.add(normalized)

        _, status = await add_entry(
            db, normalized, reason=row_reason or default_reason, source="import"
        )
        if status == "added":
            added += 1
        elif status == "duplicate":
            duplicates += 1
        else:
            invalid += 1

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
        query = query.where(SuppressionEntry.email.ilike(f"%{q.strip().lower()}%"))
    return (await db.execute(query)).scalar() or 0
