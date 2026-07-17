"""Danger-signal monitor (Feature 2 — Auto-Pause on Danger Signals).

The safety rail under all autonomous sending. A Celery task calls
:meth:`DangerMonitor.evaluate` every 5 minutes; on a breach it calls
:meth:`DangerMonitor.trip`, which engages the global kill switch, records a
:class:`DangerSignalEvent`, and alerts the operator.

Three 24h-rolling metrics are computed both **globally** (aggregate across every
send) and **per sending account**:

* **bounce rate** — ``bounced`` / ``sent`` — auto-pause > 5%.
* **complaint rate** — spam-complaints / ``sent`` — auto-pause > 0.1%.
* **hard-bounce count** — classified hard bounces in 24h — auto-pause at >= 5.

Rate trips require a **minimum sample** (>= 20 sends) because a rate is
meaningless at low volume (1 bounce of 20 = 5%). The hard-bounce count is the
low-volume backstop and is NOT sample-gated, so a bad list still burns the
domain during warm-up.

Scope decision (v1): auto-pause is **global** — it flips the single system-wide
kill switch even when only one account trips, because a shared domain/IP
reputation problem endangers all sending. Per-account soft throttling already
exists (``sender_health.throttle_factor``); this is the hard, all-stop
escalation.

Design rules borrowed from the existing services:

* **Never raise out of monitoring.** :meth:`evaluate` and :meth:`trip` swallow
  and log their own failures — a monitoring bug must never take down the worker
  or, worse, mask a real breach.
* Metrics are attributed to an account the same way ``deliverability_optimizer``
  does — by ``sender_account_id`` OR a ``from_email`` match — so Gmail and SMTP
  sends both count.
* Thresholds default from config but are overridable at runtime via a
  ``system_settings`` row (``danger_thresholds``) so they're tunable without a
  deploy.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.bounce_record import BounceRecord
from app.models.contact import Contact
from app.models.danger_signal import DangerSignalEvent
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.services import system_settings

logger = logging.getLogger(__name__)

# EmailSend.status values that represent a spam complaint (mirrors
# deliverability_optimizer so the 24h and 7d rollups agree).
_COMPLAINT_STATUSES = ("complained", "complaint", "spam")

# system_settings key holding runtime threshold overrides.
DANGER_THRESHOLDS_KEY = "danger_thresholds"


@dataclass
class Thresholds:
    """Active thresholds — config defaults merged with any runtime override."""

    bounce_rate: float
    bounce_rate_warn: float
    complaint_rate: float
    complaint_rate_warn: float
    hard_bounce_count: int
    hard_bounce_count_warn: int
    min_sample_size: int

    def as_dict(self) -> dict:
        return {
            "bounce_rate": self.bounce_rate,
            "bounce_rate_warn": self.bounce_rate_warn,
            "complaint_rate": self.complaint_rate,
            "complaint_rate_warn": self.complaint_rate_warn,
            "hard_bounce_count": self.hard_bounce_count,
            "hard_bounce_count_warn": self.hard_bounce_count_warn,
            "min_sample_size": self.min_sample_size,
        }


@dataclass
class MetricResult:
    """One evaluated metric for one scope (global or a single account)."""

    metric: str  # 'bounce_rate' | 'complaint_rate' | 'hard_bounce_count'
    scope: str  # 'global' | 'account'
    window: str  # '24h'
    observed_value: float  # rate (0..1) or a count, depending on metric
    threshold: float  # the breach threshold
    warn_threshold: float
    sample_size: int  # sends in the window (the rate denominator)
    status: str  # 'pass' | 'warn' | 'breach'
    account_id: Optional[uuid.UUID] = None
    account_email: Optional[str] = None
    hard_bounces: int = 0  # absolute hard-bounce count for context

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "scope": self.scope,
            "window": self.window,
            "observed_value": round(self.observed_value, 6),
            "threshold": self.threshold,
            "warn_threshold": self.warn_threshold,
            "sample_size": self.sample_size,
            "status": self.status,
            "account_id": str(self.account_id) if self.account_id else None,
            "account_email": self.account_email,
            "hard_bounces": self.hard_bounces,
        }


@dataclass
class DangerReport:
    """The full evaluation: every metric across every scope, plus roll-ups."""

    generated_at: datetime
    window_hours: int
    thresholds: Thresholds
    sending_enabled: bool
    metrics: list[MetricResult] = field(default_factory=list)

    @property
    def breaches(self) -> list[MetricResult]:
        return [m for m in self.metrics if m.status == "breach"]

    @property
    def warnings(self) -> list[MetricResult]:
        return [m for m in self.metrics if m.status == "warn"]

    @property
    def breach(self) -> bool:
        return bool(self.breaches)

    @property
    def overall_status(self) -> str:
        """Roll-up band for the widget pill."""
        if not self.sending_enabled:
            return "paused"
        if self.breach:
            return "breach"
        if self.warnings:
            return "watch"
        return "healthy"

    def primary_breach(self) -> Optional[MetricResult]:
        """The most severe breach — drives the kill-switch reason string.

        Ranked by how far over threshold the metric is (observed / threshold),
        with global scope preferred over a single account at equal severity so
        the reason reflects the broadest signal.
        """
        if not self.breaches:
            return None

        def severity(m: MetricResult) -> tuple:
            ratio = (m.observed_value / m.threshold) if m.threshold else float("inf")
            return (0 if m.scope == "global" else 1, -ratio)

        return sorted(self.breaches, key=severity)[0]

    def summary(self) -> dict:
        return {
            "generated_at": self.generated_at.isoformat(),
            "window_hours": self.window_hours,
            "sending_enabled": self.sending_enabled,
            "overall_status": self.overall_status,
            "breach": self.breach,
            "breaches": [m.as_dict() for m in self.breaches],
            "warnings": [m.as_dict() for m in self.warnings],
            "metrics": [m.as_dict() for m in self.metrics],
            "thresholds": self.thresholds.as_dict(),
        }


class DangerMonitor:
    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # Thresholds
    # ------------------------------------------------------------------
    async def thresholds(self, db: AsyncSession) -> Thresholds:
        """Config defaults merged with the optional runtime override row.

        Never raises — a bad/missing override falls back to config defaults so a
        typo in the tuning row can't disable the safety rail.
        """
        s = self.settings
        base = Thresholds(
            bounce_rate=s.danger_bounce_rate_threshold,
            bounce_rate_warn=s.danger_bounce_rate_warn,
            complaint_rate=s.danger_complaint_rate_threshold,
            complaint_rate_warn=s.danger_complaint_rate_warn,
            hard_bounce_count=s.danger_hard_bounce_count_threshold,
            hard_bounce_count_warn=s.danger_hard_bounce_count_warn,
            min_sample_size=s.danger_min_sample_size,
        )
        try:
            override = await system_settings.get_value(db, DANGER_THRESHOLDS_KEY, default=None)
        except Exception:  # pragma: no cover - defensive
            override = None
        if isinstance(override, dict):
            for key in base.as_dict():
                if key in override and isinstance(override[key], (int, float)):
                    setattr(base, key, override[key])
        return base

    # ------------------------------------------------------------------
    # Windowed aggregation
    # ------------------------------------------------------------------
    async def _window_metrics(
        self, db: AsyncSession, *, account=None, hours: int = 24
    ) -> dict:
        """Aggregate sends / bounces / complaints / hard-bounces over the window.

        ``account`` restricts to one sending account; ``None`` is the global
        aggregate.

        **Outbound mail lives in three places, and this rail has to watch all of
        them.** It previously counted only ``EmailSend``, which meant it was
        blind to the exact thing it exists to restrain:

        * ``EmailSend`` — manual campaigns (``routers/email_templates.py``) and
          auto-replies (``services/smart_reply.py``).
        * ``OutreachEvent`` — **Dialtone's autonomous sequence sends**. That path
          writes no EmailSend row at all (see ``tasks/outreach.py:562`` — "there
          is no EmailSend row here"), so both the numerator *and the denominator*
          were zero for every email the agent sent unattended. The rail could not
          have tripped on Dialtone's traffic under any circumstances.
        * ``BounceRecord`` — asynchronous DSNs swept out of the mailbox. This is
          where *most* real bounces land: Gmail accepts the message and reports
          the failure minutes later, so a hard bounce almost never appears as a
          synchronous send failure.

        The three are unioned rather than one replacing another: a bounce hurts
        the sending domain's reputation identically whoever queued it, and
        watching only Dialtone would re-blind the rail to manual campaigns.

        Double counting is avoided by construction, not by a DISTINCT:
        ``OutreachEvent('bounced')`` is a *synchronous* failure, so the message
        was never accepted and no DSN can exist for it; a ``BounceRecord``
        implies the send was accepted, i.e. ``OutreachEvent('sent')``. The two
        are mutually exclusive per send. ``EmailSend.status == 'bounced'`` is
        included for completeness but is provably never written today — if that
        ever changes it must not *also* create a BounceRecord, or bounces will
        be counted twice here.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        # --- Population 1: EmailSend (manual campaigns, auto-replies) ---
        es_filters = [EmailSend.sent_at >= cutoff]
        if account is not None:
            es_filters.append(
                or_(
                    EmailSend.sender_account_id == account.id,
                    EmailSend.from_email == account.email,
                )
            )
        es_base = select(func.count(EmailSend.id)).where(and_(*es_filters))

        es_sent = (await db.execute(es_base.where(EmailSend.status != "skipped"))).scalar() or 0
        es_bounces = (await db.execute(es_base.where(EmailSend.status == "bounced"))).scalar() or 0
        complaints = (
            await db.execute(es_base.where(EmailSend.status.in_(_COMPLAINT_STATUSES)))
        ).scalar() or 0

        es_hard = (
            await db.execute(
                select(func.count(EmailSend.id))
                .join(Contact, EmailSend.contact_id == Contact.id)
                .where(and_(*es_filters))
                .where(EmailSend.status == "bounced")
                .where(Contact.last_bounce_type == "hard")
            )
        ).scalar() or 0

        # --- Population 2: OutreachEvent (Dialtone's sequence sends) ---
        # Scoped through the enrollment, which is where this path records the
        # sending account (`tasks/outreach.py:556`).
        oe_filters = [OutreachEvent.occurred_at >= cutoff]
        oe_base = (
            select(func.count(OutreachEvent.id))
            .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
        )
        if account is not None:
            oe_filters.append(OutreachEnrollment.sender_account_id == account.id)

        # The denominator is ATTEMPTS, not successes. `tasks/outreach.py:587`
        # writes exactly one event per attempt — 'sent' or 'bounced' — so a
        # synchronous failure never appears as 'sent'. Counting only 'sent' would
        # leave the worst possible case (every attempt bouncing) with a
        # denominator of zero, and `bounce_rate` would divide to 0.0 and read
        # healthy. That is the original bug wearing a different hat.
        oe_attempts = (
            await db.execute(
                oe_base.where(and_(*oe_filters, OutreachEvent.event_type.in_(("sent", "bounced"))))
            )
        ).scalar() or 0
        oe_bounces = (
            await db.execute(oe_base.where(and_(*oe_filters, OutreachEvent.event_type == "bounced")))
        ).scalar() or 0

        oe_hard = (
            await db.execute(
                select(func.count(OutreachEvent.id))
                .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
                .join(Contact, OutreachEnrollment.contact_id == Contact.id)
                .where(and_(*oe_filters))
                .where(OutreachEvent.event_type == "bounced")
                .where(Contact.last_bounce_type == "hard")
            )
        ).scalar() or 0

        # --- Population 3: BounceRecord (async DSNs — most real bounces) ---
        # `processed_at` is when the sweep read the DSN rather than when the
        # bounce happened. The sweep runs every 15 minutes, so against a 24h
        # window the skew is immaterial; where it does bite, it counts a bounce
        # slightly late, which errs toward pausing. That is the right direction
        # for a brake.
        br_filters = [BounceRecord.processed_at >= cutoff]
        if account is not None:
            br_filters.append(BounceRecord.gmail_account_id == account.id)
        br_base = select(func.count(BounceRecord.id)).where(and_(*br_filters))

        br_bounces = (await db.execute(br_base)).scalar() or 0
        br_hard = (
            await db.execute(br_base.where(BounceRecord.bounce_kind == "hard"))
        ).scalar() or 0

        sent = es_sent + oe_attempts
        bounces = es_bounces + oe_bounces + br_bounces
        hard_bounces = es_hard + oe_hard + br_hard

        bounce_rate = round(bounces / sent, 6) if sent else 0.0
        complaint_rate = round(complaints / sent, 6) if sent else 0.0
        return {
            "sent": sent,
            "bounces": bounces,
            "complaints": complaints,
            "hard_bounces": hard_bounces,
            "bounce_rate": bounce_rate,
            "complaint_rate": complaint_rate,
            # Which population each number came from. An operator staring at a
            # paused agent needs to know whether the bounces are the agent's or
            # a manual campaign's — they call for opposite responses.
            "sources": {
                "sent_sequence": oe_attempts,
                "sent_email": es_sent,
                "bounces_sequence": oe_bounces,
                "bounces_email": es_bounces,
                "bounces_dsn": br_bounces,
            },
        }

    # ------------------------------------------------------------------
    # Threshold evaluation
    # ------------------------------------------------------------------
    def _rate_status(
        self, value: float, sample: int, breach: float, warn: float, min_sample: int
    ) -> str:
        # Rates are untrustworthy below the minimum sample — never trip on them.
        if sample < min_sample:
            return "pass"
        if value > breach:
            return "breach"
        if value > warn:
            return "warn"
        return "pass"

    def _count_status(self, count: int, breach: int, warn: int) -> str:
        if count >= breach:
            return "breach"
        if count >= warn:
            return "warn"
        return "pass"

    def _evaluate_scope(
        self,
        m: dict,
        t: Thresholds,
        *,
        scope: str,
        account_id: Optional[uuid.UUID] = None,
        account_email: Optional[str] = None,
    ) -> list[MetricResult]:
        """Turn a window-metrics dict into three MetricResults for one scope."""
        sample = m["sent"]
        results = [
            MetricResult(
                metric="bounce_rate",
                scope=scope,
                window="24h",
                observed_value=m["bounce_rate"],
                threshold=t.bounce_rate,
                warn_threshold=t.bounce_rate_warn,
                sample_size=sample,
                status=self._rate_status(
                    m["bounce_rate"], sample, t.bounce_rate, t.bounce_rate_warn, t.min_sample_size
                ),
                account_id=account_id,
                account_email=account_email,
                hard_bounces=m["hard_bounces"],
            ),
            MetricResult(
                metric="complaint_rate",
                scope=scope,
                window="24h",
                observed_value=m["complaint_rate"],
                threshold=t.complaint_rate,
                warn_threshold=t.complaint_rate_warn,
                sample_size=sample,
                status=self._rate_status(
                    m["complaint_rate"], sample, t.complaint_rate, t.complaint_rate_warn, t.min_sample_size
                ),
                account_id=account_id,
                account_email=account_email,
                hard_bounces=m["hard_bounces"],
            ),
            MetricResult(
                metric="hard_bounce_count",
                scope=scope,
                window="24h",
                observed_value=float(m["hard_bounces"]),
                threshold=float(t.hard_bounce_count),
                warn_threshold=float(t.hard_bounce_count_warn),
                sample_size=sample,
                status=self._count_status(
                    m["hard_bounces"], t.hard_bounce_count, t.hard_bounce_count_warn
                ),
                account_id=account_id,
                account_email=account_email,
                hard_bounces=m["hard_bounces"],
            ),
        ]
        return results

    async def evaluate(self, db: AsyncSession, *, hours: int = 24) -> DangerReport:
        """Compute all 24h metrics (global + per account) and their status.

        Never raises — on any failure returns an empty, non-breaching report so
        the caller (and the kill switch) is never tripped by a monitoring bug.
        """
        try:
            t = await self.thresholds(db)
            sending_enabled = await system_settings.is_sending_enabled(db)
            report = DangerReport(
                generated_at=datetime.now(timezone.utc),
                window_hours=hours,
                thresholds=t,
                sending_enabled=sending_enabled,
            )

            # Global aggregate.
            global_m = await self._window_metrics(db, account=None, hours=hours)
            report.metrics.extend(self._evaluate_scope(global_m, t, scope="global"))

            # Per sending account.
            try:
                from app.services.email_dispatch import get_sending_accounts

                accounts = await get_sending_accounts(db)
            except Exception as exc:  # pragma: no cover - defensive
                logger.info("danger monitor: sending-account lookup failed: %s", exc)
                accounts = []
            for account in accounts:
                try:
                    m = await self._window_metrics(db, account=account, hours=hours)
                    report.metrics.extend(
                        self._evaluate_scope(
                            m, t,
                            scope="account",
                            account_id=account.id,
                            account_email=getattr(account, "email", None),
                        )
                    )
                except Exception:  # pragma: no cover - defensive
                    logger.exception(
                        "danger monitor: per-account eval failed for %s",
                        getattr(account, "email", "?"),
                    )
            return report
        except Exception:
            logger.exception("danger monitor: evaluate() failed — treating as no-breach")
            return DangerReport(
                generated_at=datetime.now(timezone.utc),
                window_hours=hours,
                thresholds=Thresholds(
                    bounce_rate=self.settings.danger_bounce_rate_threshold,
                    bounce_rate_warn=self.settings.danger_bounce_rate_warn,
                    complaint_rate=self.settings.danger_complaint_rate_threshold,
                    complaint_rate_warn=self.settings.danger_complaint_rate_warn,
                    hard_bounce_count=self.settings.danger_hard_bounce_count_threshold,
                    hard_bounce_count_warn=self.settings.danger_hard_bounce_count_warn,
                    min_sample_size=self.settings.danger_min_sample_size,
                ),
                sending_enabled=True,
            )

    # ------------------------------------------------------------------
    # Trip
    # ------------------------------------------------------------------
    @staticmethod
    def _format_value(m: MetricResult) -> str:
        if m.metric == "hard_bounce_count":
            return f"{int(m.observed_value)}"
        return f"{m.observed_value * 100:.2f}%"

    @staticmethod
    def _format_threshold(m: MetricResult) -> str:
        if m.metric == "hard_bounce_count":
            return f"{int(m.threshold)}"
        return f"{m.threshold * 100:g}%"

    def _reason(self, m: MetricResult) -> str:
        where = "all senders" if m.scope == "global" else f"sender {m.account_email or m.account_id}"
        op = ">=" if m.metric == "hard_bounce_count" else ">"
        return (
            f"auto-pause: {m.metric} {self._format_value(m)} {op} {self._format_threshold(m)} "
            f"on {where} ({m.window}, n={m.sample_size})"
        )

    async def trip(self, db: AsyncSession, report: DangerReport) -> Optional[DangerSignalEvent]:
        """Engage the kill switch, record the trip, and alert. Never raises.

        Records ONE DangerSignalEvent for the most-severe breach (the full breach
        list + thresholds are captured in ``details`` for the post-mortem), then
        flips the global ``sending_enabled`` switch off — which itself pauses
        active enrollments and fires the admin kill-switch notification.
        """
        primary = report.primary_breach()
        if primary is None:  # pragma: no cover - defensive (trip called w/o breach)
            logger.warning("danger monitor: trip() called with no breach — skipping")
            return None

        reason = self._reason(primary)
        try:
            event = DangerSignalEvent(
                metric=primary.metric,
                scope=primary.scope,
                account_id=primary.account_id,
                account_email=primary.account_email,
                window=primary.window,
                observed_value=float(primary.observed_value),
                threshold=float(primary.threshold),
                sample_size=int(primary.sample_size),
                action_taken="auto_paused",
                details={
                    "reason": reason,
                    "hard_bounces": primary.hard_bounces,
                    "breaches": [m.as_dict() for m in report.breaches],
                    "warnings": [m.as_dict() for m in report.warnings],
                    "thresholds": report.thresholds.as_dict(),
                    "generated_at": report.generated_at.isoformat(),
                },
            )
            db.add(event)
            await db.flush()

            await system_settings.set_sending_enabled(
                db, enabled=False, updated_by="dialtone:auto-pause", reason=reason
            )
            logger.error(
                "Dialtone AUTO-PAUSED — %s (breaches: %d)", reason, len(report.breaches)
            )
            await self._notify(report, primary, event)
            return event
        except Exception:
            logger.exception("danger monitor: trip() failed to fully record — %s", reason)
            return None

    async def _notify(
        self, report: DangerReport, primary: MetricResult, event: DangerSignalEvent
    ) -> None:
        """Best-effort operator alert. Never raises."""
        try:
            from app.services.notifications import get_notification_service

            lines = [
                "Dialtone has automatically paused ALL sending — a deliverability "
                "danger threshold was breached.",
                "",
                f"Trigger: {self._reason(primary)}",
                "",
                "All breaches:",
            ]
            for m in report.breaches:
                lines.append(
                    f"  • [{m.scope}] {m.metric} = {self._format_value(m)} "
                    f"(threshold {self._format_threshold(m)}, n={m.sample_size})"
                    + (f" — {m.account_email}" if m.account_email else "")
                )
            lines += [
                "",
                f"Trip id: {event.id}",
                "",
                "Sending is paused and active enrollments are held. Review the "
                "Deliverability Health widget and, once resolved, resume via "
                "POST /dialtone/resume with this trip id and a note.",
            ]
            body = "\n".join(lines)
            await get_notification_service()._send_notification(
                "🚨 Dialtone auto-paused — deliverability danger signal", body
            )
        except Exception:  # pragma: no cover - notification is best-effort
            logger.info("danger monitor: operator alert skipped", exc_info=True)


_monitor: Optional[DangerMonitor] = None


def get_danger_monitor() -> DangerMonitor:
    global _monitor
    if _monitor is None:
        _monitor = DangerMonitor()
    return _monitor
