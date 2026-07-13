"""Conversation Intelligence — email thread analysis (feature 1.5).

Where ``sentiment_service`` scores individual replies in isolation, this service
analyzes the *whole arc* of an email thread. It pulls every message in a Gmail
thread (via ``EmailHistoryService.get_thread``), hands the full chronological
conversation to Claude, and produces:

- **momentum**: is the deal accelerating, stable, decelerating, or stalled;
- **stakeholder map**: who's in the thread and their inferred role/engagement;
- **unresolved objections**: objections raised but never addressed;
- **recommended action** + **next-best-message**: the strategically ideal reply.

Results are persisted as ``ConversationAnalysis`` rows (one per analysis run)
and served through ``GET /emails/thread/{id}/intelligence``.

Never raises into callers — analysis failures return ``None``/``{"error": ...}``.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation_analysis import ConversationAnalysis
from app.services.ai_service import get_ai_service
from app.services.email_history import EmailHistoryService

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: Optional[str]) -> str:
    """Cheap HTML→text so the model reads prose, not markup."""
    if not text:
        return ""
    return _TAG_RE.sub(" ", text).replace("&nbsp;", " ").strip()


def _momentum_label(score: float) -> str:
    if score >= 70:
        return "accelerating"
    if score >= 45:
        return "stable"
    if score >= 25:
        return "decelerating"
    return "stalled"


class ConversationIntelligenceService:
    """Analyzes full email threads for momentum, stakeholders, and next steps."""

    def __init__(self) -> None:
        self.ai = get_ai_service()
        self.history = EmailHistoryService()

    def _render_thread(self, messages: list[dict]) -> tuple[str, dict]:
        """Render a thread's messages into a chronological transcript + signals.

        Returns (transcript, signals) where signals carries computed metadata
        (message counts, participants, reply-latency trend) fed into the prompt.
        """
        lines: list[str] = []
        participants: dict[str, dict] = {}
        sent_count = received_count = 0
        prev_ts = None
        latencies: list[float] = []

        for i, m in enumerate(messages, start=1):
            direction = m.get("direction", "received")
            frm = (m.get("from_email") or "").strip()
            body = _strip_html(m.get("body_text") or m.get("body_html") or m.get("snippet") or "")
            ts = m.get("timestamp")
            subject = m.get("subject") or ""

            if direction == "sent":
                sent_count += 1
                who = "US (Apprend)"
            else:
                received_count += 1
                who = f"THEM ({frm})"
                p = participants.setdefault(
                    frm, {"email": frm, "name": m.get("contact_name"), "messages": 0, "first_step": i}
                )
                p["messages"] += 1

            if prev_ts is not None and ts is not None:
                try:
                    latencies.append((ts - prev_ts).total_seconds() / 3600.0)
                except Exception:  # noqa: BLE001
                    pass
            prev_ts = ts

            ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            lines.append(
                f"[{i}] {ts_str} — {who}\nSubject: {subject}\n{body[:1500]}\n"
            )

        # Reply-latency trend: are gaps between messages widening (cooling)?
        latency_trend = "unknown"
        if len(latencies) >= 2:
            first_half = latencies[: len(latencies) // 2]
            second_half = latencies[len(latencies) // 2 :]
            avg_first = sum(first_half) / len(first_half) if first_half else 0
            avg_second = sum(second_half) / len(second_half) if second_half else 0
            if avg_second > avg_first * 1.5:
                latency_trend = "widening (cooling)"
            elif avg_second < avg_first * 0.67:
                latency_trend = "tightening (warming)"
            else:
                latency_trend = "steady"

        signals = {
            "message_count": len(messages),
            "sent_count": sent_count,
            "received_count": received_count,
            "unique_participants": len(participants),
            "reply_latency_trend": latency_trend,
        }
        return "\n".join(lines), signals

    async def analyze_thread(
        self, db: AsyncSession, gmail_thread_id: str
    ) -> Optional[ConversationAnalysis]:
        """Analyze a single thread and persist a ``ConversationAnalysis`` row.

        Returns the stored analysis, or ``None`` if the thread has no messages
        or the AI analysis fails.
        """
        thread = await self.history.get_thread(db, gmail_thread_id)
        messages = thread.get("messages", [])
        if not messages:
            logger.info("No messages for thread %s — nothing to analyze", gmail_thread_id)
            return None

        transcript, signals = self._render_thread(messages)

        prompt = f"""You are a B2B sales conversation analyst for Apprend Technologies. Analyze this
ENTIRE email thread (chronological) between our rep and a prospect. Judge the
conversation as an arc, not message-by-message.

THREAD METADATA:
  Total messages: {signals['message_count']} (we sent {signals['sent_count']}, they sent {signals['received_count']})
  Unique people from their side: {signals['unique_participants']}
  Reply-latency trend: {signals['reply_latency_trend']}

FULL THREAD:
{transcript[:9000]}

Analyze and return ONLY valid JSON:
{{
  "momentum_score": 0-100,   // 0 = dead, 50 = neutral, 100 = actively advancing to a deal
  "momentum_reasoning": "one sentence on why",
  "stakeholder_map": [
    {{"name": "...", "email": "...", "role": "decision_maker|champion|coach|blocker|end_user|unknown", "joined_step": 1, "signal": "what tells you their role/stance"}}
  ],
  "unresolved_objections": [
    {{"objection": "...", "raised_by": "...", "addressed": false, "notes": "why it's still open"}}
  ],
  "recommended_action": "specific strategic advice for the rep's next move",
  "next_message": {{"subject": "...", "body": "the ideal next email to send — Apprend voice: short, concrete, one ask, no buzzwords", "rationale": "why this advances the deal"}}
}}

Rules: only list objections actually raised in the thread. Only list stakeholders
who actually appear. If the prospect never replied, momentum is low and the
next_message should re-engage."""

        result = await self.ai._complete_json(prompt, max_tokens=2000)
        if not isinstance(result, dict) or result.get("error"):
            logger.error(
                "Conversation analysis failed for thread %s: %s",
                gmail_thread_id, (result or {}).get("error"),
            )
            return None

        score = _clamp_score(result.get("momentum_score", 50))
        signals["momentum_reasoning"] = result.get("momentum_reasoning")

        # Resolve company/contact from the thread when available.
        company_id = thread.get("lead_id")
        contact_id = None
        for m in messages:
            if m.get("contact_id"):
                contact_id = m["contact_id"]
                if not company_id and m.get("company_id"):
                    company_id = m["company_id"]
                break

        analysis = ConversationAnalysis(
            gmail_thread_id=gmail_thread_id,
            company_id=company_id,
            contact_id=contact_id,
            momentum_score=score,
            momentum_label=_momentum_label(score),
            stakeholder_map=_as_list(result.get("stakeholder_map")),
            unresolved_objections=_as_list(result.get("unresolved_objections")),
            recommended_action=result.get("recommended_action"),
            next_message=result.get("next_message") if isinstance(result.get("next_message"), dict) else {},
            signals=signals,
            message_count=signals["message_count"],
            model_used=self.ai.model,
            analyzed_at=datetime.now(timezone.utc),
        )
        db.add(analysis)
        await db.commit()
        await db.refresh(analysis)
        logger.info(
            "Analyzed thread %s — momentum=%.0f (%s), %d objections",
            gmail_thread_id, score, analysis.momentum_label,
            len(analysis.unresolved_objections or []),
        )
        return analysis

    async def get_latest(
        self, db: AsyncSession, gmail_thread_id: str
    ) -> Optional[ConversationAnalysis]:
        """Return the most recent stored analysis for a thread, or None."""
        return (
            await db.execute(
                select(ConversationAnalysis)
                .where(ConversationAnalysis.gmail_thread_id == gmail_thread_id)
                .order_by(ConversationAnalysis.analyzed_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


def _clamp_score(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 50.0
    return max(0.0, min(100.0, f))


def _as_list(v) -> list:
    return v if isinstance(v, list) else []


def get_conversation_intelligence_service() -> ConversationIntelligenceService:
    return ConversationIntelligenceService()
