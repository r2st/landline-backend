"""Daily autonomous-activity summary.

Aggregates everything the agent did on a given calendar day — leads discovered
and enriched, emails sent (broken out by sequence and by step/stage), bounces,
replies, follow-ups triggered, lead qualification and grade distribution, cold
calls queued, and overall deliverability — into a single metrics blob plus a
human-readable narrative, then upserts it into ``agent_daily_summaries``.

Design notes:
* Windowing is done in a reporting timezone (default IST) so "today" matches the
  6 PM IST beat run. The window is ``[date 00:00, date+1 00:00)`` converted to
  UTC; all timestamp comparisons are made timezone-aware so naive (SQLite, in
  tests) and aware (Postgres) rows compare consistently.
* The metrics computation is pure DB aggregation and never calls an LLM, so it
  is fast and deterministic. The narrative is rule-based by default; an optional
  AI pass rewrites it into warmer prose and silently falls back to the
  rule-based text if the model is unavailable.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date as date_cls, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_daily_summary import AgentDailySummary
from app.models.call import CallTask
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.follow_up import FollowUp
from app.models.lead_score import LeadScore
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.prospecting import ProspectingRun
from app.models.sender_health import SenderHealth
from app.models.sequence import OutreachSequence
from app.services.scoring import grade_for_fit

logger = logging.getLogger(__name__)

# Reporting timezone — the day boundary and the "6 PM" beat run are both IST.
DEFAULT_REPORT_TZ = "Asia/Kolkata"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _day_window_utc(target_date: date_cls, tz_name: str) -> tuple[datetime, datetime]:
    """Return the ``[start, end)`` UTC datetimes for ``target_date`` in ``tz_name``."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    start_local = datetime.combine(target_date, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _in(ts: Optional[datetime], start: datetime, end: datetime) -> bool:
    ts = _aware(ts)
    return ts is not None and start <= ts < end


async def compute_daily_metrics(
    db: AsyncSession, target_date: date_cls, tz_name: str = DEFAULT_REPORT_TZ
) -> dict:
    """Aggregate all autonomous activity for ``target_date`` into a metrics dict.

    Pure DB reads — no side effects, no LLM calls.
    """
    start, end = _day_window_utc(target_date, tz_name)

    # --- Emails sent (by sequence + by stage) ------------------------------
    # Join sent events to their enrollment → sequence so we can attribute volume.
    sent_rows = (
        await db.execute(
            select(
                OutreachEvent.step_number,
                OutreachEvent.occurred_at,
                OutreachSequence.name,
                OutreachEvent.event_type,
            )
            .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            .join(OutreachSequence, OutreachEnrollment.sequence_id == OutreachSequence.id)
        )
    ).all()

    emails_sent = 0
    by_stage: Counter = Counter()
    by_sequence: Counter = Counter()
    replied_events = 0
    bounced_events = 0
    for step_number, occurred_at, seq_name, event_type in sent_rows:
        if not _in(occurred_at, start, end):
            continue
        if event_type == "sent":
            emails_sent += 1
            by_stage[str(step_number)] += 1
            by_sequence[seq_name or "Unknown"] += 1
        elif event_type == "replied":
            replied_events += 1
        elif event_type == "bounced":
            bounced_events += 1

    # --- Replies (dedicated reply store is the source of truth) -------------
    reply_rows = (await db.execute(select(EmailReply.received_at, EmailReply.created_at))).all()
    replies_received = sum(
        1 for received_at, created_at in reply_rows if _in(received_at or created_at, start, end)
    )
    # Fall back to outreach 'replied' events if no dedicated reply rows landed.
    replies_received = max(replies_received, replied_events)

    # --- Bounces (contacts newly bounced today, split hard/soft) ------------
    contact_rows = (
        await db.execute(select(Contact.last_bounced_at, Contact.last_bounce_type))
    ).all()
    bounces = Counter()
    for last_bounced_at, last_bounce_type in contact_rows:
        if _in(last_bounced_at, start, end):
            bounces["total"] += 1
            bounces[(last_bounce_type or "unknown")] += 1
    bounces_total = max(bounces["total"], bounced_events)

    # --- Leads discovered / enriched ---------------------------------------
    company_rows = (
        await db.execute(select(Company.created_at, Company.source))
    ).all()
    leads_discovered = 0
    discovered_by_source: Counter = Counter()
    for created_at, source in company_rows:
        if _in(created_at, start, end):
            leads_discovered += 1
            discovered_by_source[source or "unknown"] += 1

    # --- Lead scores computed today + grade distribution -------------------
    score_rows = (await db.execute(select(LeadScore.scored_at, LeadScore.fit_score))).all()
    leads_scored = 0
    grade_dist: Counter = Counter()
    for scored_at, fit_score in score_rows:
        if _in(scored_at, start, end):
            leads_scored += 1
            grade_dist[grade_for_fit(fit_score)] += 1

    # --- Prospecting runs (autonomous prospecting agent) -------------------
    prospecting_rows = (
        await db.execute(
            select(
                ProspectingRun.started_at,
                ProspectingRun.companies_found,
                ProspectingRun.companies_qualified,
                ProspectingRun.companies_enrolled,
                ProspectingRun.companies_review_queue,
            )
        )
    ).all()
    prospecting = {
        "runs": 0,
        "candidates_found": 0,
        "candidates_qualified": 0,
        "candidates_enrolled": 0,
        "review_queue": 0,
    }
    for started_at, found, qualified, enrolled, review in prospecting_rows:
        if _in(started_at, start, end):
            prospecting["runs"] += 1
            prospecting["candidates_found"] += found or 0
            prospecting["candidates_qualified"] += qualified or 0
            prospecting["candidates_enrolled"] += enrolled or 0
            prospecting["review_queue"] += review or 0

    # --- Follow-ups triggered ----------------------------------------------
    followup_rows = (await db.execute(select(FollowUp.created_at))).all()
    follow_ups_created = sum(1 for (created_at,) in followup_rows if _in(created_at, start, end))

    # --- Cold calls queued today -------------------------------------------
    call_rows = (
        await db.execute(select(CallTask.created_at, CallTask.completed_at, CallTask.priority))
    ).all()
    calls_queued = 0
    calls_completed = 0
    calls_by_priority: Counter = Counter()
    for created_at, completed_at, priority in call_rows:
        if _in(created_at, start, end):
            calls_queued += 1
            calls_by_priority[priority or "warm"] += 1
        if _in(completed_at, start, end):
            calls_completed += 1

    # --- Deliverability snapshot (current sender health) -------------------
    sender_rows = (
        await db.execute(
            select(
                SenderHealth.account_email,
                SenderHealth.status,
                SenderHealth.reputation_score,
                SenderHealth.bounce_rate_7d,
                SenderHealth.complaint_rate_7d,
                SenderHealth.open_rate_7d,
                SenderHealth.sent_7d,
            )
        )
    ).all()
    accounts = [
        {
            "email": email,
            "status": status,
            "reputation_score": reputation,
            "bounce_rate_7d": round(bounce_rate or 0.0, 4),
            "complaint_rate_7d": round(complaint_rate or 0.0, 4),
            "open_rate_7d": round(open_rate or 0.0, 4),
            "sent_7d": sent_7d or 0,
        }
        for email, status, reputation, bounce_rate, complaint_rate, open_rate, sent_7d in sender_rows
    ]
    avg_reputation = (
        round(sum(a["reputation_score"] for a in accounts) / len(accounts), 1)
        if accounts
        else None
    )
    day_bounce_rate = round(bounces_total / emails_sent, 4) if emails_sent else 0.0
    day_reply_rate = round(replies_received / emails_sent, 4) if emails_sent else 0.0

    return {
        "date": target_date.isoformat(),
        "timezone": tz_name,
        "window_utc": {"start": start.isoformat(), "end": end.isoformat()},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "leads": {
            "discovered": leads_discovered,
            "enriched": leads_scored,
            "discovered_by_source": dict(discovered_by_source),
        },
        "prospecting": prospecting,
        "qualification": {
            # Autonomous qualification comes from the prospecting agent.
            "qualified": prospecting["candidates_qualified"],
            "sent_to_review": prospecting["review_queue"],
        },
        "lead_scores": {
            "scored": leads_scored,
            "grade_distribution": dict(grade_dist),
        },
        "emails": {
            "sent": emails_sent,
            "by_stage": dict(by_stage),
            "by_sequence": dict(by_sequence),
        },
        "bounces": {
            "total": bounces_total,
            "hard": bounces.get("hard", 0),
            "soft": bounces.get("soft", 0),
        },
        "replies": {"received": replies_received},
        "follow_ups": {"created": follow_ups_created},
        "calls": {
            "queued": calls_queued,
            "completed": calls_completed,
            "by_priority": dict(calls_by_priority),
        },
        "deliverability": {
            "emails_sent_today": emails_sent,
            "bounces_today": bounces_total,
            "replies_today": replies_received,
            "bounce_rate_today": day_bounce_rate,
            "reply_rate_today": day_reply_rate,
            "avg_reputation": avg_reputation,
            "accounts": accounts,
        },
    }


def build_narrative(metrics: dict) -> str:
    """Deterministic, human-readable "what the agent did today" summary."""
    d = metrics.get("date", "")
    leads = metrics.get("leads", {})
    emails = metrics.get("emails", {})
    bounces = metrics.get("bounces", {})
    replies = metrics.get("replies", {})
    follow_ups = metrics.get("follow_ups", {})
    calls = metrics.get("calls", {})
    deliver = metrics.get("deliverability", {})
    prospecting = metrics.get("prospecting", {})

    sent = emails.get("sent", 0)
    parts: list[str] = []

    if sent == 0 and leads.get("discovered", 0) == 0 and calls.get("queued", 0) == 0:
        return (
            f"Quiet day on {d}: the agent took no outbound actions — no emails "
            f"sent, no new leads discovered, and no calls queued."
        )

    if sent:
        seq = emails.get("by_sequence", {})
        seq_bit = ""
        if seq:
            top = sorted(seq.items(), key=lambda kv: kv[1], reverse=True)[:2]
            seq_bit = " across " + ", ".join(f"{n} via “{name}”" for name, n in top)
        parts.append(f"Sent {sent} outreach email{'s' if sent != 1 else ''}{seq_bit}.")

    if leads.get("discovered"):
        enriched = leads.get("enriched", 0)
        enr = f" and enriched/scored {enriched}" if enriched else ""
        parts.append(f"Discovered {leads['discovered']} new lead(s){enr}.")

    if prospecting.get("runs"):
        parts.append(
            f"Ran {prospecting['runs']} prospecting session(s): "
            f"{prospecting.get('candidates_found', 0)} found, "
            f"{prospecting.get('candidates_qualified', 0)} qualified, "
            f"{prospecting.get('candidates_enrolled', 0)} enrolled."
        )

    if replies.get("received"):
        parts.append(f"Detected {replies['received']} repl{'ies' if replies['received'] != 1 else 'y'} from prospects.")

    if follow_ups.get("created"):
        parts.append(f"Triggered {follow_ups['created']} follow-up(s).")

    if calls.get("queued"):
        parts.append(f"Queued {calls['queued']} cold call(s) for the team.")

    total_bounces = bounces.get("total", 0)
    if total_bounces:
        parts.append(
            f"Handled {total_bounces} bounce(s) ({bounces.get('hard', 0)} hard, "
            f"{bounces.get('soft', 0)} soft) — affected addresses were suppressed."
        )

    # Deliverability health line.
    if sent:
        br = deliver.get("bounce_rate_today", 0.0) * 100
        rr = deliver.get("reply_rate_today", 0.0) * 100
        rep = deliver.get("avg_reputation")
        health = f"Deliverability today: {br:.1f}% bounce, {rr:.1f}% reply"
        if rep is not None:
            health += f"; avg sender reputation {rep}/100"
        parts.append(health + ".")

    return f"On {d}, the autonomous agent was active. " + " ".join(parts)


async def enhance_narrative_with_ai(metrics: dict, base_narrative: str) -> str:
    """Optionally rewrite the narrative into warmer prose via Claude.

    Returns ``base_narrative`` unchanged if the model is unavailable or errors —
    the summary must never depend on an LLM being reachable.
    """
    try:
        from app.services.ai_service import get_ai_service

        prompt = (
            "You are writing a concise daily activity briefing for a sales team "
            "using an autonomous outreach agent. Rewrite the following factual "
            "summary as a warm, professional 2–4 sentence briefing. Do not invent "
            "numbers — only use the facts given. Keep it tight.\n\n"
            f"Facts: {base_narrative}\n\n"
            f"Key metrics JSON: {metrics}"
        )
        text = await get_ai_service().complete_text(prompt, max_tokens=300)
        text = (text or "").strip()
        return text if text else base_narrative
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("AI narrative enhancement failed, using rule-based: %s", exc)
        return base_narrative


async def generate_and_store_summary(
    db: AsyncSession,
    target_date: Optional[date_cls] = None,
    tz_name: str = DEFAULT_REPORT_TZ,
    use_ai: bool = False,
) -> AgentDailySummary:
    """Compute the summary for ``target_date`` (default: today in ``tz_name``) and
    upsert it into ``agent_daily_summaries``. Returns the persisted row.
    """
    if target_date is None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        target_date = datetime.now(tz).date()

    metrics = await compute_daily_metrics(db, target_date, tz_name)
    narrative = build_narrative(metrics)
    if use_ai:
        narrative = await enhance_narrative_with_ai(metrics, narrative)

    existing = (
        await db.execute(
            select(AgentDailySummary).where(AgentDailySummary.summary_date == target_date)
        )
    ).scalar_one_or_none()

    headline = {
        "emails_sent": metrics["emails"]["sent"],
        "leads_discovered": metrics["leads"]["discovered"],
        "replies_received": metrics["replies"]["received"],
        "bounces_received": metrics["bounces"]["total"],
        "follow_ups_created": metrics["follow_ups"]["created"],
        "calls_queued": metrics["calls"]["queued"],
    }

    if existing is not None:
        existing.metrics = metrics
        existing.narrative = narrative
        for k, v in headline.items():
            setattr(existing, k, v)
        existing.updated_at = datetime.now(timezone.utc)
        summary = existing
    else:
        summary = AgentDailySummary(
            summary_date=target_date,
            metrics=metrics,
            narrative=narrative,
            **headline,
        )
        db.add(summary)

    await db.flush()
    return summary
