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

Scope decision: the *response* now matches the scope of the problem.

* A **global-scope** breach flips the system-wide kill switch. An aggregate
  bounce rate that high is a shared domain/IP reputation problem, and the
  accounts that look healthy usually just haven't sent yet.
* An **account-scope** breach pauses *that mailbox* (``sending_enabled = False``)
  and leaves every other sender running — see :meth:`DangerMonitor.pause_accounts`.
  One inbox working a bad list slice is a local fault; halting all outreach,
  freezing every enrollment, and requiring a manual resume with a trip id is a
  disproportionate answer that costs a day of pipeline.

Per-account soft throttling (``sender_health.throttle_factor``) remains the
gradual response; this is the hard stop, now aimed at whatever actually broke.

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
from app.models.complaint_record import ComplaintRecord
from app.models.contact import Contact
from app.models.danger_signal import DangerSignalEvent
from app.models.email_template import NON_WIRE_STATUS_VALUES, EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.services import system_settings
from app.services.outreach_events import count_attempts

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
    def global_breaches(self) -> list[MetricResult]:
        """Breaches on the aggregate across every sender.

        These are the ones that justify stopping *everything*: a bounce rate that
        high across all mail is a shared domain/IP reputation problem, and the
        healthy-looking accounts are only healthy because they haven't sent yet.
        """
        return [m for m in self.breaches if m.scope == "global"]

    @property
    def account_breaches(self) -> list[MetricResult]:
        """Breaches attributable to one sending account.

        A single mailbox that got a bad list slice is a *local* problem, and
        halting every other sender to punish it costs a day of pipeline for
        nothing. These are paused individually — see
        :meth:`DangerMonitor.pause_accounts`.
        """
        return [m for m in self.breaches if m.scope == "account" and m.account_id]

    def worst_breach_per_account(self) -> dict:
        """``{account_id: MetricResult}`` — the most-over-threshold breach each.

        One account can trip bounce rate *and* hard-bounce count in the same
        window; it should be paused once, with the reason naming the worst of
        them, not paused three times with three alerts.
        """
        worst: dict = {}
        for m in self.account_breaches:
            ratio = (m.observed_value / m.threshold) if m.threshold else float("inf")
            current = worst.get(m.account_id)
            current_ratio = (
                (current.observed_value / current.threshold)
                if current is not None and current.threshold
                else -1.0
            )
            if current is None or ratio > current_ratio:
                worst[m.account_id] = m
        return worst

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
    # Resolved per access, not captured in ``__init__``. This object is a
    # process-lifetime singleton (``get_danger_monitor``), so a snapshot taken at
    # construction outlives any later change to the settings object — and
    # ``get_settings`` is ``lru_cache``d, so anything that clears that cache
    # leaves the monitor reading a Settings instance nobody else can see. That
    # is not hypothetical: it silently disarmed the auto-pause *disable* switch,
    # because ``danger_account_auto_pause_enabled = False`` landed on the live
    # settings while the rail kept consulting the stale copy and paused anyway.
    # A safety rail must read the config that is actually in force.
    @property
    def settings(self):
        return get_settings()

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
        * ``ComplaintRecord`` — ingested ARF/FBL reports, and the *only* place a
          complaint has ever been recorded. ``_COMPLAINT_STATUSES`` names
          ``EmailSend.status`` values nothing writes, so ``complaint_rate`` was
          a constant 0.0 long after the reports themselves started arriving.

        The four are unioned rather than one replacing another: a bounce hurts
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

        # ``!= "skipped"`` predates ``NON_WIRE_STATUSES`` and lets four other
        # non-sends into the denominator of the brake. Two of them are the
        # send gate *refusing* — ``suppressed`` (do-not-contact) and
        # ``halted`` (the kill switch) — so a rail that had just paused
        # sending watched its own bounce rate fall as the refusals piled up.
        # The queued pair is quieter and no better: ``sent_at`` is a NOT NULL
        # ``server_default``, so a blast scheduled for next Tuesday is stamped
        # now and dilutes today's rate the moment it is composed.
        es_sent = (
            await db.execute(es_base.where(EmailSend.status.notin_(NON_WIRE_STATUS_VALUES)))
        ).scalar() or 0
        es_bounces = (await db.execute(es_base.where(EmailSend.status == "bounced"))).scalar() or 0
        es_complaints = (
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
        if account is not None:
            oe_filters.append(OutreachEnrollment.sender_account_id == account.id)

        # The denominator is ATTEMPTS, not successes, and an attempt is a
        # *step*, not a row. `send_sequence_step` writes one event per attempt —
        # 'sent' or 'bounced' — so counting only 'sent' would leave the worst
        # possible case (every attempt failing synchronously) with a denominator
        # of zero, a bounce rate of 0.0 and a rail reading healthy. But summing
        # the two rows over-counts the *common* case in the other direction:
        # `bounce_handler` writes a 'bounced' event when the DSN arrives hours
        # after a send that already logged its 'sent' one, so ten sends and one
        # async bounce counted eleven attempts and reported 9.1%. Against a 5%
        # or 10% threshold that is a brake applied late, which is the one
        # direction a brake must not err in.
        #
        # `outreach_events.count_attempts` counts distinct
        # `(enrollment_id, step_number)` pairs, which is right under both shapes
        # and makes a re-delivered DSN free rather than a second bounce.
        oe_attempts, oe_bounces = await count_attempts(
            db, where=and_(*oe_filters), join_enrollment=True
        )

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

        # --- Population 3: BounceRecord (async DSNs that reached no sequence) ---
        # `processed_at` is when the sweep read the DSN rather than when the
        # bounce happened. The sweep runs every 15 minutes, so against a 24h
        # window the skew is immaterial; where it does bite, it counts a bounce
        # slightly late, which errs toward pausing. That is the right direction
        # for a brake.
        #
        # **`funnel_events == 0` is what keeps this population disjoint from
        # population 2.** `bounce_ingestion` writes this ledger row and then
        # calls `record_bounce`, which books a `bounced` OutreachEvent against
        # the enrollment that sent. Both were counted, so every ingested DSN
        # that hit a live sequence — the ordinary case for an autonomous
        # outreach agent — was worth two bounces, and the effective auto-pause
        # threshold was half the configured one. A global bounce breach flips
        # the platform-wide kill switch, so that halted every tenant's outreach
        # at 2.5% while the operator's console (correctly) showed 5%.
        #
        # The comment above this block used to read "most real bounces", which
        # was true when it was written and stopped being true the day
        # `bounce_handler` started writing funnel events. Nothing failed; the
        # populations simply began to overlap, and a sum has no way to notice.
        # What remains here is the genuinely un-double-counted half: bounces on
        # manual sends, and on addresses whose enrollments had already finished.
        br_filters = [BounceRecord.processed_at >= cutoff]
        if account is not None:
            br_filters.append(BounceRecord.gmail_account_id == account.id)
        br_base = select(func.count(BounceRecord.id)).where(and_(*br_filters))
        br_uncounted = br_base.where(BounceRecord.funnel_events == 0)

        br_bounces = (await db.execute(br_uncounted)).scalar() or 0
        br_hard = (
            await db.execute(br_uncounted.where(BounceRecord.bounce_kind == "hard"))
        ).scalar() or 0

        # --- Population 4: ComplaintRecord (ingested ARF/FBL reports) ---
        # `complaint_rate` was the last of the three metrics still structurally
        # zero. `_COMPLAINT_STATUSES` describes `EmailSend.status` values that
        # **nothing writes** — verified by exhaustive grep, as with `"bounced"`
        # before it — so the numerator was a constant 0 and the metric passed
        # unconditionally.
        #
        # Since `complaint_ingestion` shipped, the real reports are ledgered in
        # `complaint_records` on a 15-minute sweep. Nothing read that table. So
        # the rail was not merely blind: it was blind *next to* the data, which
        # is the state that survives longest, because the feature looks present
        # from both ends — reports arrive, the dashboard has a complaint row —
        # and only the number joining them is fake.
        #
        # This is the threshold that matters most. It trips at 0.1% where bounce
        # trips at 5%, because a complaint rate is what a mailbox provider
        # blocklists a sending domain over, and a blocklisting is not something
        # you back out of by pausing afterwards.
        cr_filters = [ComplaintRecord.processed_at >= cutoff]
        if account is not None:
            cr_filters.append(ComplaintRecord.gmail_account_id == account.id)
        cr_complaints = (
            await db.execute(select(func.count(ComplaintRecord.id)).where(and_(*cr_filters)))
        ).scalar() or 0

        sent = es_sent + oe_attempts
        bounces = es_bounces + oe_bounces + br_bounces
        complaints = es_complaints + cr_complaints
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
                "complaints_email": es_complaints,
                "complaints_fbl": cr_complaints,
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

    async def pause_accounts(
        self, db: AsyncSession, report: DangerReport
    ) -> list[dict]:
        """Pause each individually-breaching sender instead of halting everything.

        The v1 rule was "any breach flips the global kill switch". That is right
        when the *aggregate* breaches — a shared domain reputation problem — but
        wrong when one mailbox out of five got a bad list slice: it stops four
        healthy senders, freezes every active enrollment, and needs a manual
        resume with a trip id. This is the surgical version: the offending
        account's ``sending_enabled`` goes False, rotation stops picking it, and
        the rest of the system keeps working.

        One pause and one alert per account, keyed on its worst breach. Idempotent
        — an account already disabled is left alone, so a breach that persists
        across 5-minute sweeps doesn't re-alert every 5 minutes. Never raises:
        this is monitoring, and a bug here must not take the worker down.

        Returns one summary dict per account actually paused.
        """
        if not self.settings.danger_account_auto_pause_enabled:
            return []

        paused: list[dict] = []
        for account_id, metric in report.worst_breach_per_account().items():
            try:
                from app.services.email_dispatch import get_account

                account = await get_account(db, account_id)
                if account is None:
                    logger.info(
                        "danger monitor: breaching account %s no longer exists", account_id
                    )
                    continue
                if not getattr(account, "sending_enabled", True):
                    # Already paused — say nothing. Re-alerting every sweep for a
                    # condition the operator has already been told about is how
                    # alerts get muted, and a muted alert is worse than none.
                    continue

                account.sending_enabled = False
                reason = self._reason(metric)
                event = DangerSignalEvent(
                    metric=metric.metric,
                    scope="account",
                    account_id=metric.account_id,
                    account_email=metric.account_email,
                    window=metric.window,
                    observed_value=float(metric.observed_value),
                    threshold=float(metric.threshold),
                    sample_size=int(metric.sample_size),
                    action_taken="account_paused",
                    details={
                        "reason": reason,
                        "hard_bounces": metric.hard_bounces,
                        "account_breaches": [
                            m.as_dict()
                            for m in report.account_breaches
                            if m.account_id == account_id
                        ],
                        "thresholds": report.thresholds.as_dict(),
                        "generated_at": report.generated_at.isoformat(),
                    },
                )
                db.add(event)
                await db.flush()

                logger.error("Sender AUTO-PAUSED — %s", reason)
                await self._notify_account_paused(account, metric, event)
                paused.append({
                    "account_id": str(account_id),
                    "account_email": metric.account_email,
                    "metric": metric.metric,
                    "observed_value": metric.observed_value,
                    "threshold": metric.threshold,
                    "sample_size": metric.sample_size,
                    "reason": reason,
                    "event_id": str(event.id),
                })
            except Exception:
                logger.exception(
                    "danger monitor: could not pause account %s", account_id
                )
        return paused

    async def _notify_account_paused(
        self, account, metric: MetricResult, event: DangerSignalEvent
    ) -> None:
        """Best-effort per-sender alert. Never raises."""
        try:
            from app.services.notifications import get_notification_service

            email = getattr(account, "email", None) or str(metric.account_id)
            body = "\n".join([
                f"Sending from {email} has been paused automatically.",
                "",
                f"Trigger: {self._reason(metric)}",
                "",
                "Other senders are unaffected and outreach continues on them — "
                "this is a per-mailbox pause, not a system halt.",
                "",
                "What to check: the list this mailbox has been working (a bad "
                "import slice is the usual cause), then its authentication "
                "(SPF/DKIM/DMARC) and recent volume ramp.",
                "",
                f"Re-enable it from Sender Health once the cause is fixed. Event id: {event.id}",
            ])
            await get_notification_service()._send_notification(
                f"⚠️ Sender paused — {email} ({metric.metric})", body
            )
        except Exception:  # pragma: no cover - notification is best-effort
            logger.info("danger monitor: per-sender alert skipped", exc_info=True)

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
                "🚨 Autopilot auto-paused — deliverability danger signal", body
            )
        except Exception:  # pragma: no cover - notification is best-effort
            logger.info("danger monitor: operator alert skipped", exc_info=True)


_monitor: Optional[DangerMonitor] = None


def get_danger_monitor() -> DangerMonitor:
    global _monitor
    if _monitor is None:
        _monitor = DangerMonitor()
    return _monitor
