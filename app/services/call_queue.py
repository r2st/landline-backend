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

Enhanced with sales-book recommendations:
* **Role-based optimal timing** (cold-calling-strategy.md): plant managers are
  best reached 7:30-9:00 AM, quality managers 10:00-11:30 AM, etc.
* **Cooling period enforcement** (ai-cold-calling-guidelines.md): 3 days after
  a live call, 2 days after voicemail, 1 day after no-answer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------
# Role-Based Optimal Call Timing (from cold-calling-strategy.md)
# ---------------------------------------------------------------------------
# Maps role keywords → list of (hour_start, hour_end) windows in 24h format.
# From the research: "Best times: 6:30-8:00 AM (plant managers), 10:00-11:30 AM,
# 2:00-3:30 PM, 4:00-5:00 PM (47% higher connect rate per Gong)"

_ROLE_TIMING: dict[str, list[tuple[int, int]]] = {
    "operations": [(7, 9), (16, 17)],     # plant/ops managers: 7:30-9 AM, 4-5 PM
    "quality": [(10, 12)],                  # quality managers: 10-11:30 AM
    "executive": [(8, 9), (16, 18)],       # C-suite: 8-9 AM, 4:30-5:30 PM
    "engineering": [(10, 12), (14, 16)],   # engineering: 10-12 PM, 2-4 PM
    "default": [(8, 9), (16, 17)],         # default: golden hours
}

_ROLE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("executive", ("ceo", "owner", "president", "founder", "principal", "md",
                   "managing director", "gm", "general manager", "cfo", "coo")),
    ("quality", ("quality", "qa", "qc", "fsqa", "food safety", "compliance")),
    ("operations", ("plant", "operations", "production", "ops", "manufacturing",
                    "supervisor", "floor", "supply chain")),
    ("engineering", ("engineer", "process", "technical", "r&d", "tooling",
                     "toolroom", "die")),
]


def _detect_role_for_timing(title: str | None) -> str:
    """Map a contact title to a role category for optimal timing."""
    t = (title or "").lower()
    for role, keywords in _ROLE_KEYWORDS:
        if any(kw in t for kw in keywords):
            return role
    return "default"


def optimal_call_windows(title: str | None) -> list[tuple[int, int]]:
    """Return the best call windows (hour_start, hour_end) for a contact's role."""
    role = _detect_role_for_timing(title)
    return _ROLE_TIMING.get(role, _ROLE_TIMING["default"])


def _optimal_window_score(title: str | None, current_hour: int) -> tuple[float, str]:
    """Return (bonus_points, window_label) based on whether now is optimal.

    +20 if within an optimal window, +10 if within 1 hour, +0 otherwise.
    """
    windows = optimal_call_windows(title)
    for start, end in windows:
        if start <= current_hour < end:
            return 20.0, f"{start}:00-{end}:00"
        if start - 1 <= current_hour < start or end <= current_hour < end + 1:
            return 10.0, f"near {start}:00-{end}:00"
    return 0.0, ""


# ---------------------------------------------------------------------------
# Cooling Period Enforcement (from ai-cold-calling-guidelines.md)
# ---------------------------------------------------------------------------
# After a call attempt, respect a cooling period before re-queuing.
_COOLING_PERIODS: dict[str, int] = {
    "connected": 3,           # 3 days after a live conversation
    "voicemail": 2,           # 2 days after leaving a voicemail
    "no_answer": 1,           # 1 day after no answer
    "gatekeeper": 2,          # 2 days after gatekeeper block
    "callback_scheduled": 7,  # wait for the callback window
}


def cooling_period_ok(
    last_outcome: str | None,
    last_completed_at: datetime | None,
    now: datetime,
) -> bool:
    """Return True if the cooling period has passed and it's OK to call again."""
    if last_outcome is None or last_completed_at is None:
        return True
    if last_completed_at.tzinfo is None:
        last_completed_at = last_completed_at.replace(tzinfo=timezone.utc)
    days_required = _COOLING_PERIODS.get(last_outcome, 1)
    return (now - last_completed_at) >= timedelta(days=days_required)


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
    *,
    contact_title: str | None = None,
    current_hour: int | None = None,
) -> tuple[str, float, str, str]:
    """Return ``(priority, score, reason, optimal_window)`` for one lead.

    Priority drives the hot/warm/cold label; score drives ordering within the
    queue; reason is a short human explanation stored on the task.

    Enhanced with role-based optimal timing from cold-calling-strategy.md.
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

    # --- NEW: Optimal call timing bonus ---
    hour = current_hour if current_hour is not None else now.hour
    window_bonus, window_label = _optimal_window_score(contact_title, hour)
    if window_bonus > 0:
        score += window_bonus
        reasons.append(f"optimal call window ({window_label})")

    return priority, score, "; ".join(reasons), window_label


async def _last_call_outcome_by_contact(
    db: AsyncSession, contact_ids: list
) -> dict:
    """Map contact_id -> (last_outcome, last_completed_at) from completed calls."""
    if not contact_ids:
        return {}
    rows = await db.execute(
        select(
            CallTask.contact_id,
            CallTask.outcome,
            CallTask.completed_at,
        )
        .where(
            CallTask.contact_id.in_(contact_ids),
            CallTask.status == "completed",
        )
        .order_by(CallTask.completed_at.asc())
    )
    out: dict = {}
    for contact_id, outcome, completed_at in rows.all():
        out[contact_id] = (outcome, _aware(completed_at))
    return out


async def generate_call_queue_for_session(
    db: AsyncSession,
    assigned_to: str | None = None,
    limit: int = 20,
    stages: list[str] | None = None,
    user_id=None,
) -> dict:
    """Build and persist a prioritised call queue. Returns a summary dict.

    Idempotent for the day: a contact that already has an open (pending/
    rescheduled) call task is skipped rather than duplicated.

    Enhanced with cooling period enforcement and role-based optimal timing.
    """
    target_stages = tuple(stages) if stages else DEFAULT_CALL_STAGES
    now = datetime.now(timezone.utc)
    current_hour = now.hour

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
            "skipped_existing": 0, "skipped_cooling": 0, "tasks": [],
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

    # Cooling period check — last completed call outcome per contact.
    last_calls = await _last_call_outcome_by_contact(db, contact_ids)

    scored: list[tuple[float, str, str, str, Contact, Company]] = []
    skipped_cooling = 0
    for contact, company in candidates:
        if contact.id in has_open:
            continue
        # Enforce cooling periods from ai-cold-calling-guidelines.md.
        last = last_calls.get(contact.id)
        if last is not None:
            last_outcome, last_completed = last
            if not cooling_period_ok(last_outcome, last_completed, now):
                skipped_cooling += 1
                continue

        vertical = iv.resolve_vertical_or_default(
            company.industry_segment or "", company.name or ""
        )
        priority, score, reason, window_label = score_lead(
            engagement.get(contact.id),
            fit_scores.get(company.id, 0),
            vertical.key,
            now,
            contact_title=contact.title,
            current_hour=current_hour,
        )
        scored.append((score, priority, reason, window_label, contact, company))

    skipped_existing = len(candidates) - len(scored) - skipped_cooling
    scored.sort(key=lambda t: t[0], reverse=True)

    created: list[CallTask] = []
    counts = {"hot": 0, "warm": 0, "cold": 0}
    for score, priority, reason, window_label, contact, company in scored[:limit]:
        task = CallTask(
            contact_id=contact.id,
            company_id=company.id,
            assigned_to=assigned_to,
            user_id=user_id,
            status="pending",
            priority=priority,
            scheduled_at=now,
            notes=f"Auto-queued ({priority}): {reason}.",
            optimal_call_window=window_label or None,
        )
        db.add(task)
        created.append(task)
        counts[priority] += 1

    await db.flush()
    for t in created:
        await db.refresh(t)

    logger.info(
        "Generated call queue: %d tasks (%d hot, %d warm, %d cold), "
        "skipped %d existing, %d cooling",
        len(created), counts["hot"], counts["warm"], counts["cold"],
        skipped_existing, skipped_cooling,
    )
    return {
        "generated": len(created),
        "hot": counts["hot"],
        "warm": counts["warm"],
        "cold": counts["cold"],
        "skipped_existing": skipped_existing,
        "skipped_cooling": skipped_cooling,
        "tasks": created,
    }
