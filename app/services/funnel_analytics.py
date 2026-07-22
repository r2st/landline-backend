"""Unified reply-rate funnel analytics (P1 fix).

Before this, ``reply_rate`` / ``open_rate`` were recomputed ad hoc in
``dashboard.py``, ``ab_tests.py``, ``coaching.py`` and ``deliverability.py``,
each with its own denominator and none giving a per-sequence / per-step funnel.
This is the single source of truth: one funnel

    sent → delivered → opened → replied → positive

computed straight off ``OutreachEvent`` (the primary source), broken down per
sequence and per step within each sequence, plus an org-wide total.

``positive`` counts replies the SmartReplyService classified as real interest —
it stamps ``metadata.positive`` on the ``replied`` event, so positivity is read
back off the same OutreachEvent rows rather than re-classified here. Older
replied events without the flag simply don't count as positive (safe under-count,
never an over-count).

Rates are expressed against ``sent`` (the top of the funnel) so every stage is
comparable, with ``positive_rate_of_replies`` added because "how many replies
were good" is the question the funnel exists to answer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence

logger = logging.getLogger(__name__)

# Funnel progression stages counted straight off OutreachEvent rows. NOTE:
# "delivered" is intentionally NOT here — Gmail emits no delivered signal, so no
# "delivered" event is ever written; a real delivered figure is derived as
# sent − bounced in ``_with_rates`` instead of always reading 0. "bounced" is a
# real event the send path writes, tracked separately (it's a negative outcome,
# not a forward stage) so the funnel can show per-sequence deliverability.
_STAGES = ("sent", "opened", "replied")


def _rate(numerator: int, denominator: int) -> float:
    """Percentage numerator/denominator, rounded to 2dp; 0.0 when denominator is 0."""
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100, 2)


def _blank_counts() -> dict:
    return {stage: 0 for stage in _STAGES} | {"bounced": 0, "positive": 0}


def _with_rates(counts: dict) -> dict:
    """Attach funnel rates to a counts dict (counts left intact).

    ``delivered`` is derived as ``sent − bounced`` (clamped at 0) rather than read
    from a non-existent "delivered" event, giving a real delivered figure and rate.
    ``bounced`` / ``bounce_rate`` expose per-sequence deliverability in the funnel.
    """
    sent = counts.get("sent", 0)
    replied = counts.get("replied", 0)
    bounced = counts.get("bounced", 0)
    delivered = max(0, sent - bounced)
    return {
        **counts,
        "delivered": delivered,
        "delivered_rate": _rate(delivered, sent),
        "bounce_rate": _rate(bounced, sent),
        "open_rate": _rate(counts.get("opened", 0), sent),
        "reply_rate": _rate(replied, sent),
        "positive_rate": _rate(counts.get("positive", 0), sent),
        "positive_rate_of_replies": _rate(counts.get("positive", 0), replied),
    }


async def compute_funnel(
    db: AsyncSession,
    *,
    sequence_id: Optional[UUID] = None,
    days: Optional[int] = None,
) -> dict:
    """Build the per-sequence / per-step funnel plus an org-wide total.

    ``sequence_id`` restricts to one sequence; ``days`` restricts to events in
    the trailing window (by ``occurred_at``). All reads are org-scoped by the
    global mixin filter.
    """
    since: Optional[datetime] = None
    if days and days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=days)

    # Base join: every event → its enrollment's sequence. step_number lives on
    # the event, so per-step is a group key with no extra join.
    def _base():
        q = (
            select(
                OutreachEnrollment.sequence_id.label("sequence_id"),
                OutreachEvent.step_number.label("step_number"),
                OutreachEvent.event_type.label("event_type"),
                func.count(OutreachEvent.id).label("n"),
            )
            .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            .group_by(
                OutreachEnrollment.sequence_id,
                OutreachEvent.step_number,
                OutreachEvent.event_type,
            )
        )
        if sequence_id is not None:
            q = q.where(OutreachEnrollment.sequence_id == sequence_id)
        if since is not None:
            q = q.where(OutreachEvent.occurred_at >= since)
        return q

    rows = (await db.execute(_base())).all()

    # (sequence_id, step_number) → counts dict.
    per_step: dict[tuple, dict] = {}
    for seq_id, step_number, event_type, n in rows:
        key = (seq_id, int(step_number))
        bucket = per_step.setdefault(key, _blank_counts())
        if event_type in _STAGES:
            bucket[event_type] += int(n)
        elif event_type == "bounced":
            bucket["bounced"] += int(n)

    # Positive replies: replied events whose metadata flags interest. Portable
    # across Postgres/SQLite by reading the JSON in Python (the volume here is
    # replied events only, which is small relative to sends/opens).
    pos_q = (
        select(
            OutreachEnrollment.sequence_id,
            OutreachEvent.step_number,
            OutreachEvent.event_metadata,
        )
        .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
        .where(OutreachEvent.event_type == "replied")
    )
    if sequence_id is not None:
        pos_q = pos_q.where(OutreachEnrollment.sequence_id == sequence_id)
    if since is not None:
        pos_q = pos_q.where(OutreachEvent.occurred_at >= since)

    for seq_id, step_number, metadata in (await db.execute(pos_q)).all():
        if isinstance(metadata, dict) and metadata.get("positive"):
            key = (seq_id, int(step_number))
            per_step.setdefault(key, _blank_counts())["positive"] += 1

    # Sequence names for labelling (only those that appeared).
    seq_ids = {seq_id for (seq_id, _step) in per_step}
    names: dict = {}
    if seq_ids:
        name_rows = (
            await db.execute(
                select(OutreachSequence.id, OutreachSequence.name).where(
                    OutreachSequence.id.in_(seq_ids)
                )
            )
        ).all()
        names = {sid: nm for sid, nm in name_rows}

    # Assemble per-sequence (with nested per-step) and roll up totals.
    sequences: dict = {}
    for (seq_id, step_number), counts in per_step.items():
        seq = sequences.setdefault(
            seq_id,
            {
                "sequence_id": str(seq_id),
                "sequence_name": names.get(seq_id) or "(unknown)",
                "_totals": _blank_counts(),
                "_steps": {},
            },
        )
        seq["_steps"][step_number] = counts
        for k in _blank_counts():
            seq["_totals"][k] += counts[k]

    overall = _blank_counts()
    sequence_list = []
    for seq in sequences.values():
        for k in overall:
            overall[k] += seq["_totals"][k]
        steps = [
            {"step_number": step, **_with_rates(seq["_steps"][step])}
            for step in sorted(seq["_steps"])
        ]
        sequence_list.append(
            {
                "sequence_id": seq["sequence_id"],
                "sequence_name": seq["sequence_name"],
                "totals": _with_rates(seq["_totals"]),
                "steps": steps,
            }
        )

    # Stable, useful ordering: most sends first.
    sequence_list.sort(key=lambda s: s["totals"]["sent"], reverse=True)

    return {
        "window_days": days,
        "totals": _with_rates(overall),
        "sequences": sequence_list,
    }
