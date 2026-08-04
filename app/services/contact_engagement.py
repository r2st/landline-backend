"""Per-contact engagement: what we sent them and what came back.

**Reads both send tables.** A send lands in ``outreach_events`` (the sequence
engine, which writes no ``EmailSend`` row) or in ``email_sends`` (manual,
template, inbox reply, smart-reply auto-send). Counting one of them is how a
surface ends up telling an operator we have never emailed a contact we have
emailed nine times — the exact failure the unified inbox shipped with. See
CLAUDE.md, "Sent mail lives in two tables".

The roll-up is computed on demand rather than denormalised onto the contact: a
counter maintained by every send path is a counter that drifts, and the volume
per contact is bounded by the per-contact monthly cap anyway.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_template import NON_WIRE_STATUSES, EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence

logger = logging.getLogger(__name__)

# Event types that count toward the roll-up. Anything else an event log grows
# later (channel touches, manual notes) is ignored rather than silently folded
# into "sent".
_COUNTED = ("sent", "opened", "clicked", "replied", "bounced")

# ``EmailSend.status`` values where nothing reached the wire. Counting these as
# sends inflates the denominator every rate is divided by, so a contact whose
# sends were all declined by the warm-up cap would read as 0% open rate rather
# than "we haven't emailed them".
#
# Taken from the model rather than spelled out here. The hand-written copy this
# replaces had drifted: it listed six of the seven and omitted ``pending`` — the
# status every row *starts* in and the one a queued-but-unsent message sits in.
# So a compose that had not yet reached the transport already counted as a send
# on the contact's panel, with a timestamp, and already divided into their open
# and reply rates. ``NON_WIRE_STATUSES`` exists so a new status has to be
# classified by whoever adds it; a local literal is how that guarantee is lost.
_NON_SENDS = NON_WIRE_STATUSES


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; comparisons below need one tz basis."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def contact_engagement(
    db: AsyncSession, contact_id: UUID, *, limit: int = 50
) -> dict:
    """Engagement counters plus the most recent ``limit`` events for a contact.

    Rates are denominated in *sends*, matching how every other surface in the
    codebase computes them, and are 0.0 rather than undefined at zero sends.
    """
    events: list[dict] = []
    counts = dict.fromkeys(_COUNTED, 0)

    # --- Sequence sends + engagement (outreach_events) ---
    rows = (
        await db.execute(
            select(OutreachEvent, OutreachSequence.name)
            .join(
                OutreachEnrollment,
                OutreachEvent.enrollment_id == OutreachEnrollment.id,
            )
            .join(
                OutreachSequence,
                OutreachEnrollment.sequence_id == OutreachSequence.id,
                isouter=True,
            )
            .where(OutreachEnrollment.contact_id == contact_id)
            .order_by(OutreachEvent.occurred_at.desc())
        )
    ).all()
    for event, sequence_name in rows:
        if event.event_type in counts:
            counts[event.event_type] += 1
        meta = event.event_metadata or {}
        events.append({
            "type": event.event_type,
            "occurred_at": _aware(event.occurred_at),
            # The sequence path stores the subject in the event metadata — there
            # is no EmailSend row to read it from.
            "subject": meta.get("subject"),
            "step_number": event.step_number,
            "sequence_name": sequence_name,
            "source": "outreach",
        })

    # --- Manual / template sends (email_sends) ---
    sends = (
        await db.execute(
            select(EmailSend)
            .where(EmailSend.contact_id == contact_id)
            .order_by(EmailSend.sent_at.desc())
        )
    ).scalars().all()
    for send in sends:
        # ``status`` records the send's outcome; the *_at stamps record what the
        # recipient did afterwards. Both matter and they are not the same thing.
        if send.status in _NON_SENDS:
            # Nothing went out — not a send, not a bounce, not history.
            continue
        if send.status == "bounced":
            counts["bounced"] += 1
            events.append({
                "type": "bounced", "occurred_at": _aware(send.sent_at),
                "subject": send.subject, "step_number": None,
                "sequence_name": None, "source": "email_send",
            })
        else:
            counts["sent"] += 1
            events.append({
                "type": "sent", "occurred_at": _aware(send.sent_at),
                "subject": send.subject, "step_number": None,
                "sequence_name": None, "source": "email_send",
            })
        for kind, stamp in (
            ("opened", send.opened_at),
            ("clicked", send.clicked_at),
            ("replied", send.replied_at),
        ):
            if stamp is not None:
                counts[kind] += 1
                events.append({
                    "type": kind, "occurred_at": _aware(stamp),
                    "subject": send.subject, "step_number": None,
                    "sequence_name": None, "source": "email_send",
                })

    events = [e for e in events if e["occurred_at"] is not None]
    events.sort(key=lambda e: e["occurred_at"], reverse=True)

    touches = [e["occurred_at"] for e in events]
    sent = counts["sent"]
    active = (
        await db.execute(
            select(OutreachEnrollment).where(
                OutreachEnrollment.contact_id == contact_id,
                OutreachEnrollment.status == "active",
            )
        )
    ).scalars().all()

    return {
        "contact_id": contact_id,
        **counts,
        "open_rate": round(counts["opened"] / sent, 4) if sent else 0.0,
        "reply_rate": round(counts["replied"] / sent, 4) if sent else 0.0,
        "first_touch_at": min(touches) if touches else None,
        "last_touch_at": max(touches) if touches else None,
        "active_enrollments": len(active),
        "events": events[:limit],
    }
