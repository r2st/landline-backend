"""AI sales coach — per-rep performance aggregation and coaching analysis.

Aggregates each rep's activity and effectiveness from existing tables
(``email_sends``, ``call_tasks``, ``interactions``, ``deals``), computes derived
rates (reply rate, connect rate, …), benchmarks each rep against the team, and
asks Claude to turn the numbers into specific, ranked coaching recommendations.

Deals carry no direct rep column, so they're attributed to a rep via the
companies that rep has actually touched (through interactions, call tasks, or
emails). This is a heuristic — good enough for coaching signal, not billing.

Results are persisted as ``RepPerformance`` snapshots (one per rep per period),
generated on demand from the ``coaching`` router or weekly via the
``generate_weekly_coaching`` Celery task.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.call import CallTask
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.models.rep_performance import RepPerformance
from app.models.user import User
from app.services.llm_providers import ChainClient, any_llm_available, chain_client

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

# Call outcomes that count as a live connection with a human.
_CONNECT_OUTCOMES = {"connected", "answered", "conversation", "meeting_booked"}


def period_bounds(period_type: str = "weekly", now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Return (start, end) for the most recent complete-ish period ending now."""
    if now is None:
        now = datetime.now(timezone.utc)
    days = 30 if period_type == "monthly" else 7
    return now - timedelta(days=days), now


def _rate(numerator: int, denominator: int) -> float:
    """Percentage, 1 decimal, safe against divide-by-zero."""
    if not denominator:
        return 0.0
    return round(numerator / denominator * 100, 1)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a possibly-naive datetime to UTC-aware.

    Postgres returns tz-aware timestamps, but SQLite (tests) returns naive ones;
    comparing the two raises TypeError. Treat naive values as UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _touched_company_ids(db: AsyncSession, user_id: UUID) -> set[UUID]:
    """Company ids this rep has interacted with (any time) — for deal attribution."""
    ids: set[UUID] = set()

    for stmt in (
        select(Interaction.company_id).where(Interaction.user_id == user_id, Interaction.company_id.isnot(None)),
        select(CallTask.company_id).where(CallTask.user_id == user_id),
    ):
        rows = await db.execute(stmt)
        ids.update(cid for (cid,) in rows.all() if cid is not None)

    # Emails link to a contact, which links to a company.
    email_rows = await db.execute(
        select(Contact.company_id)
        .join(EmailSend, EmailSend.contact_id == Contact.id)
        .where(EmailSend.user_id == user_id, Contact.company_id.isnot(None))
    )
    ids.update(cid for (cid,) in email_rows.all() if cid is not None)
    return ids


async def compute_rep_metrics(
    db: AsyncSession, user_id: UUID, period_start: datetime, period_end: datetime
) -> dict:
    """Compute a rep's raw + derived activity/effectiveness metrics for a period."""
    # --- Email metrics ---
    email_rows = await db.execute(
        select(
            func.count(EmailSend.id),
            func.count(EmailSend.opened_at),
            func.count(EmailSend.clicked_at),
            func.count(EmailSend.replied_at),
        ).where(
            EmailSend.user_id == user_id,
            EmailSend.sent_at >= period_start,
            EmailSend.sent_at <= period_end,
        )
    )
    emails_sent, opens, clicks, replies = email_rows.one()
    emails_sent = int(emails_sent or 0)

    # --- Call metrics ---
    call_rows = await db.execute(
        select(CallTask.status, CallTask.outcome).where(
            CallTask.user_id == user_id,
            CallTask.completed_at >= period_start,
            CallTask.completed_at <= period_end,
        )
    )
    calls_completed = 0
    connects = 0
    for status, outcome in call_rows.all():
        if status == "completed" or outcome:
            calls_completed += 1
            if outcome and outcome.lower() in _CONNECT_OUTCOMES:
                connects += 1

    # --- Interaction metrics (by type) ---
    interaction_rows = await db.execute(
        select(Interaction.interaction_type, func.count(Interaction.id))
        .where(
            Interaction.user_id == user_id,
            Interaction.occurred_at >= period_start,
            Interaction.occurred_at <= period_end,
        )
        .group_by(Interaction.interaction_type)
    )
    interactions_by_type = {t: int(c) for t, c in interaction_rows.all()}
    total_interactions = sum(interactions_by_type.values())

    # --- Deal metrics (attributed via touched companies) ---
    company_ids = await _touched_company_ids(db, user_id)
    deals_created = deals_won = deals_lost = 0
    won_value = 0.0
    if company_ids:
        deal_rows = await db.execute(
            select(Deal.status, Deal.value, Deal.created_at, Deal.updated_at).where(
                Deal.company_id.in_(company_ids)
            )
        )
        for status, value, created_at, updated_at in deal_rows.all():
            created_at = _aware(created_at)
            updated_at = _aware(updated_at)
            if created_at and period_start <= created_at <= period_end:
                deals_created += 1
            closed_at = updated_at or created_at
            in_window = closed_at and period_start <= closed_at <= period_end
            if status == "won" and in_window:
                deals_won += 1
                won_value += float(value or 0)
            elif status == "lost" and in_window:
                deals_lost += 1

    return {
        "emails_sent": emails_sent,
        "emails_opened": int(opens or 0),
        "emails_clicked": int(clicks or 0),
        "emails_replied": int(replies or 0),
        "open_rate": _rate(int(opens or 0), emails_sent),
        "click_rate": _rate(int(clicks or 0), emails_sent),
        "reply_rate": _rate(int(replies or 0), emails_sent),
        "calls_completed": calls_completed,
        "call_connects": connects,
        "connect_rate": _rate(connects, calls_completed),
        "interactions_total": total_interactions,
        "interactions_by_type": interactions_by_type,
        "deals_created": deals_created,
        "deals_won": deals_won,
        "deals_lost": deals_lost,
        "win_rate": _rate(deals_won, deals_won + deals_lost),
        "won_value": round(won_value, 2),
    }


# Metrics used for team benchmarking (higher is better for all of these).
_BENCHMARK_KEYS = ("open_rate", "reply_rate", "connect_rate", "win_rate", "emails_sent", "calls_completed")


async def compute_team_benchmarks(
    db: AsyncSession, period_start: datetime, period_end: datetime
) -> dict:
    """Compute team averages and per-metric best-in-team for the period.

    Returns ``{"team_averages": {...}, "top_performer": {...}, "rep_count": n}``
    where ``top_performer`` maps each benchmark metric to the best value any rep
    achieved (anonymized — a target to beat, not a name to shame).
    """
    users = (await db.execute(select(User).where(User.is_active.is_(True)))).scalars().all()
    per_rep = []
    for u in users:
        m = await compute_rep_metrics(db, u.id, period_start, period_end)
        # Only count reps with any activity so idle accounts don't drag averages.
        if m["emails_sent"] or m["calls_completed"] or m["interactions_total"]:
            per_rep.append(m)

    if not per_rep:
        return {"team_averages": {}, "top_performer": {}, "rep_count": 0}

    averages = {}
    top = {}
    for key in _BENCHMARK_KEYS:
        values = [r.get(key, 0) for r in per_rep]
        averages[key] = round(sum(values) / len(values), 1)
        top[key] = max(values)
    return {"team_averages": averages, "top_performer": top, "rep_count": len(per_rep)}


class SalesCoach:
    """Wraps Claude for qualitative rep-performance coaching."""

    def __init__(self):
        settings = get_settings()
        self.model = _MODEL
        self._api_key = settings.anthropic_api_key
        self._client: ChainClient | None = None

    @property
    def available(self) -> bool:
        # Any chain provider counts, not just the Anthropic key: this service is
        # chain-backed now, and gating on one credential would keep it dark on a
        # deployment that carries only a Gemini or Groq key.
        return any_llm_available()

    def _get_client(self) -> ChainClient:
        if self._client is None:
            self._client = chain_client(self._api_key)
        return self._client

    def _parse_json(self, text: str) -> dict:
        import json
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                return json.loads(text[start:end + 1])
            raise

    async def analyze(self, rep_name: str, metrics: dict, benchmarks: dict) -> dict:
        """Ask Claude for strengths/weaknesses + ranked coaching recommendations."""
        if not self.available:
            return self._fallback(metrics, benchmarks)

        import json
        prompt = f"""You are an elite B2B sales coach analyzing one rep's performance for
Apprend Technologies (manufacturing software; cold email + cold calling motion).

REP: {rep_name}

THIS REP'S METRICS (this period):
{json.dumps(metrics, indent=2)}

TEAM BENCHMARKS (anonymized — averages and best-in-team to beat):
{json.dumps(benchmarks, indent=2)}

Give specific, actionable coaching — not generic advice. Compare the rep to team
averages and the top performer. Where they lag, say concretely what to change
(e.g. "reply rate 6% vs team 12% — shorten subject lines and open with a
company-specific pain point instead of a pitch"). Where they lead, reinforce it.

Return ONLY valid JSON:
{{
  "summary": "2-3 sentence overall read of this rep's period",
  "strengths": ["specific strength tied to a metric", "..."],
  "weaknesses": ["specific weakness tied to a metric", "..."],
  "skill_gaps": ["prospecting|discovery|negotiation|closing area needing work with why"],
  "coaching_recommendations": [
    {{"priority": "high|medium|low", "focus": "short label", "recommendation": "specific action to take", "expected_impact": "what improves"}}
  ]
}}
Rank coaching_recommendations most-impactful first. Provide 3-5."""
        try:
            message = await self._get_client().messages.create(
                model=self.model,
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt}],
            )
            result = self._parse_json(message.content[0].text)
            return {
                "insights": {
                    "summary": result.get("summary", ""),
                    "strengths": result.get("strengths", []),
                    "weaknesses": result.get("weaknesses", []),
                    "skill_gaps": result.get("skill_gaps", []),
                },
                "coaching_recommendations": result.get("coaching_recommendations", []),
                "model_used": self.model,
            }
        except Exception as e:
            logger.error("Sales coach analysis failed: %s", str(e))
            return self._fallback(metrics, benchmarks)

    def _fallback(self, metrics: dict, benchmarks: dict) -> dict:
        """Deterministic rule-based coaching when the AI is unavailable."""
        avg = benchmarks.get("team_averages", {})
        strengths, weaknesses, recs = [], [], []

        def _cmp(key, label):
            mine = metrics.get(key, 0)
            team = avg.get(key, 0)
            if team and mine >= team:
                strengths.append(f"{label} {mine} at/above team average {team}")
            elif team and mine < team:
                weaknesses.append(f"{label} {mine} below team average {team}")
                recs.append({
                    "priority": "high" if mine < team * 0.7 else "medium",
                    "focus": label,
                    "recommendation": f"Improve {label.lower()} toward the team average of {team}.",
                    "expected_impact": f"Closing the {label.lower()} gap",
                })

        _cmp("reply_rate", "Reply rate")
        _cmp("open_rate", "Open rate")
        _cmp("connect_rate", "Connect rate")
        _cmp("win_rate", "Win rate")

        return {
            "insights": {
                "summary": "Rule-based coaching (AI unavailable): comparison to team averages.",
                "strengths": strengths,
                "weaknesses": weaknesses,
                "skill_gaps": [],
            },
            "coaching_recommendations": recs,
            "model_used": None,
        }


_coach: SalesCoach | None = None


def get_sales_coach() -> SalesCoach:
    global _coach
    if _coach is None:
        _coach = SalesCoach()
    return _coach


async def generate_rep_performance(
    db: AsyncSession,
    user: User,
    period_type: str = "weekly",
    now: Optional[datetime] = None,
    benchmarks: Optional[dict] = None,
) -> RepPerformance:
    """Compute metrics + AI coaching for a rep and upsert a RepPerformance snapshot.

    ``benchmarks`` can be passed in when generating for the whole team (compute
    once, reuse) — otherwise it's computed here.
    """
    period_start, period_end = period_bounds(period_type, now)
    metrics = await compute_rep_metrics(db, user.id, period_start, period_end)
    if benchmarks is None:
        benchmarks = await compute_team_benchmarks(db, period_start, period_end)

    coach = get_sales_coach()
    analysis = await coach.analyze(user.name, metrics, benchmarks)
    metrics["_benchmarks"] = benchmarks

    # Upsert on (user_id, period_type, period_start-ish). We match the most
    # recent snapshot of this type within a day to avoid churning rows.
    existing = (
        await db.execute(
            select(RepPerformance)
            .where(RepPerformance.user_id == user.id)
            .where(RepPerformance.period_type == period_type)
            .where(RepPerformance.period_start >= period_start - timedelta(hours=12))
            .order_by(RepPerformance.period_start.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.period_start = period_start
        existing.period_end = period_end
        existing.metrics = metrics
        existing.ai_insights = analysis["insights"]
        existing.coaching_recommendations = {"items": analysis["coaching_recommendations"]}
        existing.model_used = analysis.get("model_used")
        snapshot = existing
    else:
        snapshot = RepPerformance(
            user_id=user.id,
            period_type=period_type,
            period_start=period_start,
            period_end=period_end,
            metrics=metrics,
            ai_insights=analysis["insights"],
            coaching_recommendations={"items": analysis["coaching_recommendations"]},
            model_used=analysis.get("model_used"),
        )
        db.add(snapshot)
    await db.flush()
    return snapshot
