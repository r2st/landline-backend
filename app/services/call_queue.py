"""Prioritised cold-call queue generation.

Turns the lead database into a ranked list of call tasks for a sales rep. The
ranking blends the signals that actually predict a productive call:

* **email engagement** — replies, link clicks and repeat opens (from
  ``EmailSend``) are the strongest "they're interested, call now" signals,
* **recency** — leads that have gone quiet since the last touch bubble up so
  they don't go cold; never-contacted active leads are worth a first call,
* **lead score** — the company's latest fit score,
* **industry priority** — the flagship vertical (injection molding) gets a
  small boost.

Each generated ``CallTask`` is stamped with a ``priority`` (hot/warm/cold) and a
human-readable reason in ``notes`` so the rep knows why the lead surfaced. The
logic lives in :func:`generate_call_queue_for_session` so it can be driven by
both the API endpoint and the morning Celery beat task, and tested directly
against any AsyncSession.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import CallTask
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.lead_score import LeadScore
from app.services import industry_verticals as iv

logger = logging.getLogger(__name__)

# Kanban pipeline stages worth cold-calling by default (companies being actively
# worked). Overridable per request.
DEFAULT_CALL_STAGES = ("researching", "outreach", "engaged", "negotiating")

# Open call-task statuses — we don't queue a contact that already has one.
_OPEN_CALL_STATUSES = ("pending", "rescheduled")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class _Engagement:
    __slots__ = ("emails_sent", "opens", "clicks", "replies", "last_contact_at")

    def __init__(self):
        self.emails_sent = 0
        self.opens = 0
        self.clicks = 0
        self.replies = 0
        self.last_contact_at: datetime | None = None


async def engagement_by_contact(
    db: AsyncSession, contact_ids: list
) -> dict:
    """Aggregate per-contact email engagement from ``EmailSend``.

    Returns ``{contact_id: _Engagement}`` for the given contacts (missing
    contacts simply have no entry / zero engagement).
    """
    if not contact_ids:
        return {}
    rows = await db.execute(
        select(
            EmailSend.contact_id,
            func.count(EmailSend.id),
            func.count(EmailSend.opened_at),
            func.count(EmailSend.clicked_at),
            func.count(EmailSend.replied_at),
            func.max(EmailSend.sent_at),
        )
        .where(EmailSend.contact_id.in_(contact_ids))
        .group_by(EmailSend.contact_id)
    )
    out: dict = {}
    for contact_id, sent, opens, clicks, replies, last_sent in rows.all():
        e = _Engagement()
        e.emails_sent = sent or 0
        e.opens = opens or 0
        e.clicks = clicks or 0
        e.replies = replies or 0
        e.last_contact_at = _aware(last_sent)
        out[contact_id] = e
    return out


async def _latest_fit_score_by_company(db: AsyncSession, company_ids: list) -> dict:
    """Map company_id -> latest fit_score (0 if none)."""
    if not company_ids:
        return {}
    rows = await db.execute(
        select(LeadScore.company_id, LeadScore.fit_score, LeadScore.scored_at)
        .where(LeadScore.company_id.in_(company_ids))
        .order_by(LeadScore.scored_at.asc())
    )
    out: dict = {}
    # ascending order means the last write per company wins (the latest).
    for company_id, fit_score, _scored_at in rows.all():
        out[company_id] = fit_score or 0
    return out


def score_lead(
    engagement: _Engagement | None,
    fit_score: int,
    industry_key: str,
    now: datetime,
) -> tuple[str, float, str]:
    """Return ``(priority, score, reason)`` for one lead.

    Priority drives the hot/warm/cold label; score drives ordering within the
    queue; reason is a short human explanation stored on the task.
    """
    e = engagement or _Engagement()
    reasons: list[str] = []

    # Recency.
    days_since: float | None = None
    if e.last_contact_at is not None:
        days_since = (now - e.last_contact_at).total_seconds() / 86400.0

    # --- priority bucket ---
    if e.replies > 0:
        priority = "hot"
        reasons.append("replied to an email")
    elif e.clicks > 0:
        priority = "hot"
        reasons.append(f"clicked a link ({e.clicks})")
    elif e.opens >= 3:
        priority = "hot"
        reasons.append(f"opened email {e.opens} times")
    elif e.opens >= 1:
        priority = "warm"
        reasons.append(f"opened email {e.opens} time(s)")
    elif days_since is not None and days_since <= 14:
        priority = "warm"
        reasons.append("recently contacted, no engagement yet")
    else:
        priority = "cold"
        if e.last_contact_at is None:
            reasons.append("never contacted")
        else:
            reasons.append(f"gone quiet for {int(days_since or 0)} days")

    # --- ordering score ---
    score = (
        e.replies * 100.0
        + e.clicks * 40.0
        + e.opens * 15.0
        + (fit_score or 0) * 0.3
    )
    # Flagship vertical gets a small edge.
    score += 10.0 if industry_key == iv.DEFAULT_VERTICAL.key else 5.0

    # Overdue boost: quiet-but-known leads should resurface; capped so engagement
    # always outranks pure staleness.
    if days_since is not None:
        score += min(days_since * 0.5, 20.0)
        if days_since > 21:
            reasons.append("overdue for a touch")
    else:
        score += 15.0  # never contacted — worth a first call

    if fit_score:
        reasons.append(f"fit score {fit_score}")

    return priority, score, "; ".join(reasons)


async def generate_call_queue_for_session(
    db: AsyncSession,
    assigned_to: str | None = None,
    limit: int = 20,
    stages: list[str] | None = None,
) -> dict:
    """Build and persist a prioritised call queue. Returns a summary dict.

    Idempotent for the day: a contact that already has an open (pending/
    rescheduled) call task is skipped rather than duplicated.
    """
    target_stages = tuple(stages) if stages else DEFAULT_CALL_STAGES
    now = datetime.now(timezone.utc)

    # Candidate contacts at companies in the target pipeline stages.
    rows = await db.execute(
        select(Contact, Company)
        .join(Company, Company.id == Contact.company_id)
        .where(
            Contact.unsubscribed.is_(False),
            Company.pipeline_stage.in_(target_stages),
        )
    )
    candidates = list(rows.all())
    if not candidates:
        return {
            "generated": 0, "hot": 0, "warm": 0, "cold": 0,
            "skipped_existing": 0, "tasks": [],
        }

    contact_ids = [c.id for c, _ in candidates]
    company_ids = list({comp.id for _, comp in candidates})

    engagement = await engagement_by_contact(db, contact_ids)
    fit_scores = await _latest_fit_score_by_company(db, company_ids)

    # Contacts that already have an open call task — don't double-queue.
    open_rows = await db.execute(
        select(CallTask.contact_id).where(
            CallTask.status.in_(_OPEN_CALL_STATUSES),
            CallTask.contact_id.isnot(None),
        )
    )
    has_open = {r[0] for r in open_rows.all()}

    scored: list[tuple[float, str, str, Contact, Company]] = []
    for contact, company in candidates:
        if contact.id in has_open:
            continue
        vertical = iv.resolve_vertical_or_default(
            company.industry_segment or "", company.name or ""
        )
        priority, score, reason = score_lead(
            engagement.get(contact.id),
            fit_scores.get(company.id, 0),
            vertical.key,
            now,
        )
        scored.append((score, priority, reason, contact, company))

    skipped_existing = len(candidates) - len(scored)
    scored.sort(key=lambda t: t[0], reverse=True)

    created: list[CallTask] = []
    counts = {"hot": 0, "warm": 0, "cold": 0}
    for score, priority, reason, contact, company in scored[:limit]:
        task = CallTask(
            contact_id=contact.id,
            company_id=company.id,
            assigned_to=assigned_to,
            status="pending",
            priority=priority,
            scheduled_at=now,
            notes=f"Auto-queued ({priority}): {reason}.",
        )
        db.add(task)
        created.append(task)
        counts[priority] += 1

    await db.flush()
    for t in created:
        await db.refresh(t)

    logger.info(
        "Generated call queue: %d tasks (%d hot, %d warm, %d cold), skipped %d existing",
        len(created), counts["hot"], counts["warm"], counts["cold"], skipped_existing,
    )
    return {
        "generated": len(created),
        "hot": counts["hot"],
        "warm": counts["warm"],
        "cold": counts["cold"],
        "skipped_existing": skipped_existing,
        "tasks": created,
    }
