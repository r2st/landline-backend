"""Email deliverability optimizer (feature 3.3).

Two responsibilities:

1. **Sender reputation** — aggregate the last 7 days of ``EmailSend`` rows per
   sending account into bounce / complaint / open rates, a 0-100 reputation
   score, and a ``throttle_factor`` (0..1) the send limiter multiplies into that
   account's daily cap. A struggling account is throttled so volume shifts to
   healthier accounts automatically.

2. **Content scanning** — a pre-send spam-risk check (deterministic heuristics,
   optionally sharpened by Claude) that flags spammy patterns before an email
   goes out.

Metrics are attributed to an account by ``EmailSend.sender_account_id`` (Gmail)
or, as a fallback, by matching ``EmailSend.from_email`` to the account's address
(covers SMTP sends that don't set the Gmail-only FK).
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.bounce_record import BounceRecord
from app.models.email_template import EmailSend
from app.models.sender_health import SenderHealth, SenderHealthSnapshot
from app.services.email_dispatch import get_sending_accounts, _transport_for

logger = logging.getLogger(__name__)

# Common spam-trigger phrases (lowercased). Not exhaustive — a signal, not a verdict.
_SPAM_PHRASES = (
    "act now", "buy now", "click here", "limited time", "risk free", "risk-free",
    "100% free", "free money", "guarantee", "no obligation", "cash bonus",
    "earn $", "make money", "double your", "winner", "congratulations you",
    "this is not spam", "increase sales", "order now", "special promotion",
    "urgent", "wire transfer", "viagra", "crypto",
)


class DeliverabilityOptimizer:
    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # Sender reputation
    # ------------------------------------------------------------------
    async def _account_metrics(self, db: AsyncSession, account) -> dict:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        match = or_(
            EmailSend.sender_account_id == account.id,
            EmailSend.from_email == account.email,
        )
        base = select(func.count(EmailSend.id)).where(EmailSend.sent_at >= cutoff, match)

        sent = (await db.execute(base.where(EmailSend.status != "skipped"))).scalar() or 0
        send_bounces = (await db.execute(base.where(EmailSend.status == "bounced"))).scalar() or 0
        complaints = (
            await db.execute(base.where(EmailSend.status.in_(("complained", "complaint", "spam"))))
        ).scalar() or 0
        opens = (
            await db.execute(base.where(EmailSend.opened_at.isnot(None)))
        ).scalar() or 0

        # Real bounces mostly arrive as mailer-daemon DSNs and are ledgered in
        # ``bounce_records`` by the ingestion sweep — which does NOT flip
        # ``EmailSend.status`` to "bounced". Counting only the send status would
        # therefore report ~0 bounces for an account that is actually bouncing,
        # so fold in the ingested bounce ledger. Gmail DSNs land in the sending
        # mailbox, so they attribute cleanly by ``gmail_account_id``; SMTP DSNs
        # carry no account link, so those accounts fall back to send status only.
        ingested_bounces = 0
        if _transport_for(account) == "gmail":
            ingested_bounces = (
                await db.execute(
                    select(func.count(BounceRecord.id)).where(
                        BounceRecord.gmail_account_id == account.id,
                        BounceRecord.processed_at >= cutoff,
                    )
                )
            ).scalar() or 0
        bounces = send_bounces + ingested_bounces

        bounce_rate = round(bounces / sent, 4) if sent else 0.0
        complaint_rate = round(complaints / sent, 4) if sent else 0.0
        open_rate = round(opens / sent, 4) if sent else 0.0
        return {
            "sent_7d": sent,
            "bounces_7d": bounces,
            "complaints_7d": complaints,
            "opens_7d": opens,
            "bounce_rate_7d": bounce_rate,
            "complaint_rate_7d": complaint_rate,
            "open_rate_7d": open_rate,
        }

    def _score(self, m: dict) -> tuple[Optional[int], float, str]:
        """Return (reputation 0-100 or None, throttle_factor 0..1, status).

        A ``None`` reputation with status ``"pending"`` means "not enough data to
        rate this account yet" — reported honestly instead of the old fabricated
        ``100 / healthy``, which made an account that had never sent a single
        email look like a proven-clean sender.
        """
        s = self.settings
        sent = m["sent_7d"]
        bounces = m["bounces_7d"]
        complaints = m["complaints_7d"]

        # No activity in the window at all → nothing measured yet. Don't claim a
        # perfect reputation; report 'pending' and don't throttle (there is
        # nothing to throttle).
        if sent == 0 and bounces == 0 and complaints == 0:
            return None, 1.0, "pending"

        # Some signal, but too little send volume for stable *rates* (one bounce
        # in three sends is not a 33% bounce rate). Score on absolute bad-event
        # counts so a clean low-volume account isn't thrown to 'critical', while
        # real damage (a hard bounce, a complaint) still shows.
        if sent < 10:
            rep = 100.0
            rep -= min(70.0, bounces * 20.0)
            rep -= min(60.0, complaints * 35.0)
            rep = int(max(0, min(100, round(rep))))
            if rep < s.sender_min_reputation:
                return rep, 0.4, "throttled"
            if rep < 80:
                return rep, 0.75, "watch"
            return rep, 1.0, "healthy"

        rep = 100.0
        rep -= min(60.0, m["bounce_rate_7d"] * 400)      # 5% bounce → -20, 15% → -60
        rep -= min(40.0, m["complaint_rate_7d"] * 3000)  # 0.3% complaint → -9
        # Very low open rate is a soft negative signal (possible inboxing issue).
        if m["open_rate_7d"] < 0.05:
            rep -= 10
        rep = int(max(0, min(100, round(rep))))

        throttled = (
            m["bounce_rate_7d"] > s.sender_bounce_rate_throttle
            or m["complaint_rate_7d"] > s.sender_complaint_rate_throttle
        )
        if rep < s.sender_min_reputation:
            return rep, 0.1, "critical"
        if throttled or rep < 60:
            return rep, 0.4, "throttled"
        if rep < 80:
            return rep, 0.75, "watch"
        return rep, 1.0, "healthy"

    async def assess_account(self, db: AsyncSession, account) -> SenderHealth:
        """Compute and upsert the SenderHealth row for one account."""
        account_type = _transport_for(account)
        metrics = await self._account_metrics(db, account)
        rep, throttle, status = self._score(metrics)

        row = (
            await db.execute(
                select(SenderHealth).where(
                    SenderHealth.account_type == account_type,
                    SenderHealth.account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        now = datetime.now(timezone.utc)
        # Capture the prior status before we overwrite it, so we can alert only on
        # a *degrading transition* (not on every hourly re-assessment of an
        # already-throttled account). None on the very first assessment.
        prior_status = row.status if row is not None else None
        if row is None:
            row = SenderHealth(account_type=account_type, account_id=account.id)
            db.add(row)
        row.account_email = account.email
        row.sent_7d = metrics["sent_7d"]
        row.bounces_7d = metrics["bounces_7d"]
        row.complaints_7d = metrics["complaints_7d"]
        row.opens_7d = metrics["opens_7d"]
        row.bounce_rate_7d = metrics["bounce_rate_7d"]
        row.complaint_rate_7d = metrics["complaint_rate_7d"]
        row.open_rate_7d = metrics["open_rate_7d"]
        row.reputation_score = rep
        row.throttle_factor = throttle
        row.status = status
        row.details = metrics
        row.last_assessed = now
        await db.flush()

        # Append an immutable history point so the dashboard can trend reputation
        # over time. Best-effort: a snapshot failure must never sink the
        # assessment itself (which throttling depends on).
        try:
            db.add(SenderHealthSnapshot(
                account_id=account.id,
                account_type=account_type,
                account_email=account.email,
                sent_7d=metrics["sent_7d"],
                bounce_rate_7d=metrics["bounce_rate_7d"],
                complaint_rate_7d=metrics["complaint_rate_7d"],
                open_rate_7d=metrics["open_rate_7d"],
                reputation_score=rep,
                throttle_factor=throttle,
                status=status,
                captured_at=now,
            ))
            await db.flush()
        except Exception:
            logger.exception("Sender-health snapshot failed for %s", account.email)

        # Alert on a degrading transition into throttled/critical (change-only, so
        # an already-degraded account doesn't re-alert every hour; recovery and the
        # first-ever assessment are intentionally silent). Best-effort — an alert
        # failure must never sink the assessment that throttling depends on.
        _DEGRADED = ("throttled", "critical")
        if status in _DEGRADED and prior_status is not None and prior_status not in _DEGRADED:
            try:
                from app.services.notifications import get_notification_service

                await get_notification_service().notify_sender_health_change(
                    account.email, prior_status, status, metrics, db=db
                )
            except Exception:  # pragma: no cover - notification must never break the run
                logger.exception("Sender-health alert failed for %s", account.email)

        return row

    async def assess_all(self, db: AsyncSession) -> list[dict]:
        """Assess every sending-enabled account. Returns per-account summaries."""
        accounts = await get_sending_accounts(db)
        out = []
        for account in accounts:
            try:
                row = await self.assess_account(db, account)
                out.append({
                    "account_id": str(account.id),
                    "email": account.email,
                    "reputation_score": row.reputation_score,
                    "throttle_factor": row.throttle_factor,
                    "status": row.status,
                    "bounce_rate_7d": row.bounce_rate_7d,
                })
            except Exception:
                logger.exception("Sender-health assessment failed for %s", account.email)
        return out

    async def throttle_factor_for(self, db: AsyncSession, account) -> float:
        """The current throttle factor for an account (1.0 if unknown)."""
        account_type = _transport_for(account)
        row = (
            await db.execute(
                select(SenderHealth).where(
                    SenderHealth.account_type == account_type,
                    SenderHealth.account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        return row.throttle_factor if row is not None else 1.0

    # ------------------------------------------------------------------
    # Content scanning
    # ------------------------------------------------------------------
    def scan_content_heuristic(self, subject: str, body: str, has_unsubscribe: bool = True) -> dict:
        """Deterministic spam-risk scan. Returns {spam_score 0-100, risk, issues, suggestions}."""
        issues: list[str] = []
        suggestions: list[str] = []
        text = f"{subject}\n{body}"
        lower = text.lower()
        score = 0

        # Spam phrases.
        hits = sorted({p for p in _SPAM_PHRASES if p in lower})
        if hits:
            score += min(30, 8 * len(hits))
            issues.append(f"spam-trigger phrases: {', '.join(hits[:6])}")
            suggestions.append("Rephrase or remove salesy trigger phrases.")

        # Excessive capitalization (of alpha characters in the body).
        letters = [c for c in body if c.isalpha()]
        if letters:
            caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
            if caps_ratio > 0.35 and len(letters) > 20:
                score += 20
                issues.append(f"excessive capitalization ({round(caps_ratio * 100)}% of letters)")
                suggestions.append("Use normal sentence case, not ALL CAPS.")

        # ALL-CAPS words in the subject.
        subj_caps = re.findall(r"\b[A-Z]{3,}\b", subject)
        if subj_caps:
            score += 10
            issues.append(f"all-caps words in subject: {', '.join(subj_caps[:4])}")

        # Exclamation marks.
        excl = text.count("!")
        if excl >= 3:
            score += 10
            issues.append(f"{excl} exclamation marks")
            suggestions.append("Cut exclamation marks — one at most.")

        # Too many links.
        links = len(re.findall(r"https?://", body))
        if links > 3:
            score += 15
            issues.append(f"{links} links (too many)")
            suggestions.append("Reduce to one primary link.")

        # Missing unsubscribe.
        if not has_unsubscribe:
            score += 15
            issues.append("no unsubscribe mechanism")
            suggestions.append("Include a one-click unsubscribe (CAN-SPAM / bulk-sender rules).")

        score = max(0, min(100, score))
        risk = "high" if score >= 50 else "medium" if score >= 25 else "low"
        return {"spam_score": score, "risk": risk, "issues": issues, "suggestions": suggestions}

    async def scan_content(
        self, subject: str, body: str, has_unsubscribe: bool = True, use_ai: bool = False
    ) -> dict:
        """Content scan; optionally blends a Claude spam assessment on top of heuristics."""
        result = self.scan_content_heuristic(subject, body, has_unsubscribe)
        if use_ai:
            ai = await self._ai_scan(subject, body)
            if ai:
                result["ai_assessment"] = ai
                # Blend: take the higher of the two risk scores.
                if isinstance(ai.get("spam_score"), (int, float)):
                    result["spam_score"] = max(result["spam_score"], int(ai["spam_score"]))
                    result["risk"] = (
                        "high" if result["spam_score"] >= 50
                        else "medium" if result["spam_score"] >= 25 else "low"
                    )
        return result

    async def _ai_scan(self, subject: str, body: str) -> Optional[dict]:
        try:
            from app.services.ai_service import get_ai_service

            prompt = (
                "Assess this cold outreach email for spam-filter risk. Return ONLY JSON: "
                '{"spam_score": 0-100, "issues": ["..."], "fixes": ["..."]}\n\n'
                f"Subject: {subject}\n\nBody:\n{body[:2000]}"
            )
            data = await get_ai_service().complete_json(prompt, max_tokens=500)
            return data if isinstance(data, dict) and "error" not in data else None
        except Exception as e:  # pragma: no cover
            logger.warning("AI content scan failed: %s", e)
            return None


_optimizer: Optional[DeliverabilityOptimizer] = None


def get_deliverability_optimizer() -> DeliverabilityOptimizer:
    global _optimizer
    if _optimizer is None:
        _optimizer = DeliverabilityOptimizer()
    return _optimizer
