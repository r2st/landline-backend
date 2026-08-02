"""A/B test wiring around the statistical engine (Feature 3).

The maths already lives in :mod:`app.services.ab_testing_service` (z-test,
confidence, bandit, ``auto_declare_winner``). This module is the *wiring*:

* :func:`resolve_variant` — called on the send path. If the step has an active
  A/B test with ≥2 active variants, it assigns a variant (even round-robin or
  bandit), persists the assignment **before** the send, and returns the chosen
  subject/body. Otherwise it returns the step's own template unchanged — the
  exact current behaviour (backwards compatible).
* :func:`backfill_assignment_send` — links an assignment to its EmailSend after
  the send (for send paths that produce one; the sequence path attributes via
  ``outreach_events`` instead).
* :func:`compute_variant_stats` — per-variant sends/opens/clicks/replies + rates,
  aggregated on demand from ``outreach_events`` via the assignment join.
* :func:`evaluate_test` — feeds the per-variant stats into the engine's
  ``auto_declare_winner`` (leader-vs-control), honours the test's ``min_sample``
  / ``confidence_target`` gates, applies a hard cap (500 sends/variant or 21
  days → default winner), and on a declared winner promotes it to the step's
  active template and concludes the test.

Assignment attribution note: the sequence send path records ``outreach_events``
(``enrollment_id`` + step number), not ``EmailSend`` rows, so per-variant metrics
join through the assignment's enrollment rather than ``email_send_id`` (which
stays NULL there).
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ab_test import AbTest, AbTestAssignment, AbTestVariant
from app.models.outreach import OutreachEvent
from app.models.sequence import SequenceStep
from app.services.ab_testing_service import (
    auto_declare_winner,
    evaluate_experiment,
    select_variant_bandit,
)

logger = logging.getLogger(__name__)

# Hard cap so a test never runs forever: once either variant reaches this many
# sends, or the test is this old, conclude by declaring the highest-metric
# variant as the default winner ("no significant difference").
HARD_CAP_SENDS = 500
HARD_CAP_DAYS = 21

# metric name → the rate key in a stats dict.
_RATE_KEY = {"replied": "reply_rate", "opened": "open_rate", "clicked": "click_rate"}


@dataclass
class VariantResolution:
    """The copy to send for one step, plus the A/B assignment (if any)."""

    subject_template: str
    body_template: str
    assignment_id: Optional[UUID] = None
    variant_id: Optional[UUID] = None
    variant_label: Optional[str] = None
    is_ab: bool = False


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _resolution_for(
    step: SequenceStep, variant: AbTestVariant, assignment_id: UUID
) -> VariantResolution:
    """Chosen copy for a variant — a NULL variant template inherits the step."""
    return VariantResolution(
        subject_template=variant.subject_template
        if variant.subject_template is not None
        else step.subject_template,
        body_template=variant.body_template
        if variant.body_template is not None
        else step.body_template,
        assignment_id=assignment_id,
        variant_id=variant.id,
        variant_label=variant.label,
        is_ab=True,
    )


async def _active_test(db: AsyncSession, step_id: UUID) -> Optional[AbTest]:
    """The test governing this step, oldest first if somehow there are two.

    ``ab_tests`` carries no unique constraint on ``sequence_step_id``. The
    create endpoint tries to enforce one, which is not the same thing — it
    cannot stop a race, and this runs on the **send** path, where raising means
    the step does not go out at all.
    """
    return (
        await db.execute(
            select(AbTest)
            .where(AbTest.sequence_step_id == step_id)
            .order_by(AbTest.created_at.asc(), AbTest.id.asc())
            .limit(1)
        )
    ).scalars().first()


async def _variants(
    db: AsyncSession, step_id: UUID, *, active_only: bool = False
) -> list[AbTestVariant]:
    q = select(AbTestVariant).where(AbTestVariant.sequence_step_id == step_id)
    if active_only:
        q = q.where(AbTestVariant.is_active.is_(True))
    rows = list((await db.execute(q)).scalars().all())
    rows.sort(key=lambda v: v.label)  # deterministic order (A, B, C, D)
    return rows


async def resolve_variant(
    db: AsyncSession, enrollment, step: SequenceStep
) -> VariantResolution:
    """Resolve which subject/body to send for ``enrollment`` at ``step``.

    Backwards compatible: a step with no A/B test (or a concluded one, or fewer
    than 2 active variants) returns its own template and touches no A/B tables.
    An A/B-enabled step assigns a variant, persists the assignment *before* the
    send, and returns the chosen copy. Idempotent per ``(enrollment, step)``.
    """
    test = await _active_test(db, step.id)
    if test is None or test.status == "completed":
        # No test, or already concluded → the step template (which, post-winner,
        # is the winning copy) is used directly. Current behaviour, no writes.
        return VariantResolution(step.subject_template, step.body_template)

    variants = await _variants(db, step.id, active_only=True)
    if len(variants) < 2:
        # A step is only "A/B-enabled" with ≥2 active variants.
        return VariantResolution(step.subject_template, step.body_template)

    # Idempotent: reuse an existing assignment (retry / double-dispatch safe).
    existing = (
        await db.execute(
            select(AbTestAssignment).where(
                AbTestAssignment.enrollment_id == enrollment.id,
                AbTestAssignment.sequence_step_id == step.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        variant = next((v for v in variants if v.id == existing.variant_id), None)
        if variant is None:  # assigned variant was deactivated — fall back
            return VariantResolution(
                step.subject_template, step.body_template,
                assignment_id=existing.id, variant_id=existing.variant_id, is_ab=True,
            )
        return _resolution_for(step, variant, existing.id)

    variant = await _choose_variant(db, test, step, variants)
    assignment = AbTestAssignment(
        variant_id=variant.id,
        sequence_step_id=step.id,
        enrollment_id=enrollment.id,
        contact_id=getattr(enrollment, "contact_id", None),
    )
    try:
        # SAVEPOINT so a unique-constraint race rolls back only this insert, not
        # the surrounding send transaction.
        async with db.begin_nested():
            db.add(assignment)
            await db.flush()  # persist assignment before the send (crash safety)
    except IntegrityError:
        # Concurrent assignment won the race — reuse theirs.
        existing = (
            await db.execute(
                select(AbTestAssignment).where(
                    AbTestAssignment.enrollment_id == enrollment.id,
                    AbTestAssignment.sequence_step_id == step.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            variant = next((v for v in variants if v.id == existing.variant_id), variant)
            return _resolution_for(step, variant, existing.id)
        raise

    return _resolution_for(step, variant, assignment.id)


async def _choose_variant(
    db: AsyncSession, test: AbTest, step: SequenceStep, variants: list[AbTestVariant]
) -> AbTestVariant:
    """Pick the variant to assign next.

    ``even`` (default): deterministic round-robin by current assignment count —
    guarantees even distribution even at low N (no RNG). ``bandit``: UCB1 over
    the test's success metric, shifting traffic toward the leader as data grows.

    Both strategies key off the assignment count as the arm's "pulls" — the send
    (and thus the engagement signal) happens after assignment, so counting
    assignments is what makes exploration progress at low N.
    """
    rows = (
        await db.execute(
            select(AbTestAssignment.variant_id, func.count())
            .where(AbTestAssignment.sequence_step_id == step.id)
            .group_by(AbTestAssignment.variant_id)
        )
    ).all()
    counts = {vid: c for vid, c in rows}

    if test.strategy == "bandit":
        # UCB1 pulls = assignments so far; reward = the metric observed among them.
        stats = await compute_variant_stats(db, step, variants, test.metric)
        succ = {s["variant_id"]: s[test.metric] for s in stats}
        bandit_input = [
            {"sent": counts.get(v.id, 0), "successes": succ.get(v.id, 0)}
            for v in variants
        ]
        idx = select_variant_bandit(bandit_input)
        return variants[idx]

    # even round-robin: fewest-assigned wins; ties break to the earliest label
    # because ``variants`` is label-sorted and ``min`` returns the first minimum.
    return min(variants, key=lambda v: counts.get(v.id, 0))


async def backfill_assignment_send(
    db: AsyncSession, assignment_id: Optional[UUID], email_send_id: Optional[UUID]
) -> None:
    """Link an assignment to the EmailSend it produced, after the send.

    No-op when either id is missing. The sequence send path records
    ``outreach_events`` rather than ``EmailSend`` rows, so it passes ``None`` and
    attribution flows through the assignment's enrollment instead; this exists
    for send paths that do create an ``EmailSend``.
    """
    if assignment_id is None or email_send_id is None:
        return
    assignment = (
        await db.execute(
            select(AbTestAssignment).where(AbTestAssignment.id == assignment_id)
        )
    ).scalar_one_or_none()
    if assignment is not None:
        assignment.email_send_id = email_send_id


async def compute_variant_stats(
    db: AsyncSession,
    step: SequenceStep,
    variants: list[AbTestVariant],
    metric: str = "replied",  # noqa: ARG001 - kept for call-site symmetry
) -> list[dict]:
    """Per-variant sends/opens/clicks/replies + rates for a step's test.

    Aggregated on demand from ``outreach_events`` joined to the assignment by
    enrollment (the sequence path's send/engagement log). A contact counts once
    per outcome (distinct enrollments), and the rate denominator is *sends*,
    matching the engine's ``variant_x_sent`` semantics.
    """
    arows = (
        await db.execute(
            select(AbTestAssignment.variant_id, AbTestAssignment.enrollment_id).where(
                AbTestAssignment.sequence_step_id == step.id
            )
        )
    ).all()
    by_variant: dict[UUID, set] = defaultdict(set)
    all_enrollments: set = set()
    for vid, eid in arows:
        by_variant[vid].add(eid)
        all_enrollments.add(eid)

    enroll_events: dict[UUID, set] = defaultdict(set)
    if all_enrollments:
        ev_rows = (
            await db.execute(
                select(OutreachEvent.enrollment_id, OutreachEvent.event_type).where(
                    OutreachEvent.enrollment_id.in_(list(all_enrollments)),
                    OutreachEvent.step_number == step.step_number,
                )
            )
        ).all()
        for eid, et in ev_rows:
            enroll_events[eid].add(et)

    stats: list[dict] = []
    for v in sorted(variants, key=lambda x: x.label):
        eids = by_variant.get(v.id, set())

        def _count(outcome: str) -> int:
            return sum(1 for e in eids if outcome in enroll_events.get(e, ()))

        sent = _count("sent")
        opened = _count("opened")
        clicked = _count("clicked")
        replied = _count("replied")
        denom = sent or 0
        stats.append({
            "variant_id": v.id,
            "label": v.label,
            "is_control": bool(v.is_control),
            "is_active": bool(v.is_active),
            "assigned": len(eids),
            "sent": sent,
            "opened": opened,
            "clicked": clicked,
            "replied": replied,
            "open_rate": round(opened / denom, 4) if denom else 0.0,
            "click_rate": round(clicked / denom, 4) if denom else 0.0,
            "reply_rate": round(replied / denom, 4) if denom else 0.0,
        })
    return stats


class _ExperimentShim:
    """Adapts control + leader stats to the duck-typed shape the engine expects.

    The engine (``evaluate_experiment`` / ``auto_declare_winner``) is pairwise
    A-vs-B: ``A`` is the control, ``B`` is the leading challenger. Winner ``"A"``
    means the control won, ``"B"`` the challenger.
    """

    def __init__(self, control: dict, challenger: dict, test_id, name: str):
        self.id = test_id
        self.name = name
        for slot, s in (("a", control), ("b", challenger)):
            setattr(self, f"variant_{slot}_sent", s["sent"])
            setattr(self, f"variant_{slot}_opened", s["opened"])
            setattr(self, f"variant_{slot}_clicked", s["clicked"])
            setattr(self, f"variant_{slot}_replied", s["replied"])
        self.winner: Optional[str] = None
        self.confidence_level: Optional[float] = None
        self.is_significant: bool = False
        self.status: str = "running"
        self.completed_at: Optional[datetime] = None


def _pick_control_and_leader(stats: list[dict], metric: str):
    """Return ``(control, leader)`` — leader is the best challenger by metric.

    Control is the ``is_control`` variant (or the first if none is flagged).
    Leader is the highest-rate non-control variant, tie-broken by sends then
    label. Returns ``(control, None)`` when there is no challenger.
    """
    if not stats:
        return None, None
    rate_key = _RATE_KEY.get(metric, "reply_rate")
    control = next((s for s in stats if s["is_control"]), stats[0])
    challengers = [s for s in stats if s["variant_id"] != control["variant_id"]]
    if not challengers:
        return control, None
    leader = max(challengers, key=lambda s: (s[rate_key], s["sent"], -ord(s["label"])))
    return control, leader


async def evaluate_test(
    db: AsyncSession,
    test: AbTest,
    now: Optional[datetime] = None,
    *,
    persist: bool = True,
) -> dict:
    """Evaluate a test: compute stats, run the engine, and conclude if warranted.

    Returns a results dict (per-variant stats, leader, confidence, significance,
    winner). When ``persist`` and a winner is reached (significant, or forced by
    the hard cap), the winner is promoted to the step's active template and the
    test is marked ``completed``.
    """
    now = now or datetime.now(timezone.utc)
    step = (
        await db.execute(
            select(SequenceStep).where(SequenceStep.id == test.sequence_step_id)
        )
    ).scalar_one_or_none()
    variants = await _variants(db, test.sequence_step_id)
    stats = await compute_variant_stats(db, step, variants, test.metric) if step else []

    result: dict = {
        "test_id": str(test.id),
        "sequence_step_id": str(test.sequence_step_id),
        "metric": test.metric,
        "strategy": test.strategy,
        "min_sample": test.min_sample,
        "confidence_target": test.confidence_target,
        "variants": stats,
        "status": test.status,
        "winner_variant_id": str(test.winner_variant_id) if test.winner_variant_id else None,
        "confidence": None,
        "is_significant": False,
        "default_winner": False,
        "leader_label": None,
    }
    if test.status == "completed" or step is None:
        return result

    control, leader = _pick_control_and_leader(stats, test.metric)
    if control is not None and leader is not None:
        result["leader_label"] = leader["label"]
        shim = _ExperimentShim(control, leader, test.id, f"step {step.step_number}")

        both_ready = (
            control["sent"] >= test.min_sample and leader["sent"] >= test.min_sample
        )
        # Judge on the metric the test was created to optimise. Without this a
        # subject-line test (which exists to move OPEN rate) was decided on reply
        # rate — the body's work, credited to the subject.
        if both_ready:
            evaluation = auto_declare_winner(shim, now, metric=test.metric)
            # Honour a stricter confidence target than the engine's 0.95 gate.
            if shim.winner in ("A", "B") and (shim.confidence_level or 0.0) < test.confidence_target:
                shim.winner = None
                shim.status = "running"
                shim.completed_at = None
            result["confidence"] = evaluation.get("overall_confidence")
        else:
            evaluation = evaluate_experiment(shim, test.metric)
            result["confidence"] = evaluation.get("overall_confidence")

        if shim.winner in ("A", "B"):
            winner_stat = control if shim.winner == "A" else leader
            result["is_significant"] = True
            result["winner_variant_id"] = str(winner_stat["variant_id"])
            result["status"] = "completed"
            if persist:
                await _apply_winner(test, step, variants, winner_stat, now, default=False)
            return result

    # Hard cap: never let a test run forever. Declare the best variant by default.
    max_sent = max((s["sent"] for s in stats), default=0)
    age_days = (now - _aware(test.created_at)).days if test.created_at else 0
    if stats and (max_sent >= HARD_CAP_SENDS or age_days >= HARD_CAP_DAYS):
        # "Best" means best on the metric under test. Picking by reply rate for a
        # subject-line test would promote whichever subject happened to sit above
        # the better-replying body — an outcome the subject didn't cause.
        cap_rate_key = _RATE_KEY.get(test.metric, "reply_rate")
        best = max(stats, key=lambda s: (s[cap_rate_key], s["sent"], -ord(s["label"])))
        result["default_winner"] = True
        result["winner_variant_id"] = str(best["variant_id"])
        result["status"] = "completed"
        result["leader_label"] = best["label"]
        if persist:
            await _apply_winner(test, step, variants, best, now, default=True)
        logger.info(
            "A/B test %s hard-capped (max_sent=%d, age=%dd) → default winner %s",
            test.id, max_sent, age_days, best["label"],
        )
    return result


async def declare_winner(
    db: AsyncSession, test: AbTest, variant_id: UUID, now: Optional[datetime] = None
) -> dict:
    """Manually declare ``variant_id`` the winner (operator override).

    Promotes the variant to the step's active template and concludes the test,
    exactly like an auto-declared winner. Raises ``ValueError`` if the variant
    doesn't belong to this test's step.
    """
    now = now or datetime.now(timezone.utc)
    step = (
        await db.execute(
            select(SequenceStep).where(SequenceStep.id == test.sequence_step_id)
        )
    ).scalar_one_or_none()
    if step is None:
        raise ValueError("test step not found")
    variants = await _variants(db, test.sequence_step_id)
    if not any(v.id == variant_id for v in variants):
        raise ValueError("variant does not belong to this test")
    stats = await compute_variant_stats(db, step, variants, test.metric)
    winner_stat = next((s for s in stats if s["variant_id"] == variant_id), None)
    if winner_stat is None:  # pragma: no cover - guarded above
        raise ValueError("variant not found")
    await _apply_winner(test, step, variants, winner_stat, now, default=False)
    return {
        "test_id": str(test.id),
        "status": "completed",
        "winner_variant_id": str(variant_id),
        "winner_label": winner_stat["label"],
        "manual": True,
    }


async def _apply_winner(
    test: AbTest,
    step: SequenceStep,
    variants: list[AbTestVariant],
    winner_stat: dict,
    now: datetime,
    *,
    default: bool,
) -> None:
    """Conclude a test: promote the winner to the step template, notify, log.

    Sets the winning variant's copy as the step's active template so 100% of
    future sends use it, deactivates all variants (a concluded test falls back to
    the step template in :func:`resolve_variant`), marks the test completed, and
    fires the operator notification + ``ab_test_concluded`` activity event.
    """
    winner = next((v for v in variants if v.id == winner_stat["variant_id"]), None)
    test.status = "completed"
    test.winner_variant_id = winner_stat["variant_id"]
    test.declared_at = now

    if winner is not None:
        if winner.subject_template is not None:
            step.subject_template = winner.subject_template
        if winner.body_template is not None:
            step.body_template = winner.body_template
    for v in variants:
        v.is_active = False

    await _notify_concluded(test, step, variants, winner_stat, default=default)


async def _notify_concluded(
    test: AbTest,
    step: SequenceStep,
    variants: list[AbTestVariant],
    winner_stat: dict,
    *,
    default: bool,
) -> None:
    """Best-effort operator alert that a test concluded (never raises)."""
    try:
        from app.services.notifications import get_notification_service

        label = winner_stat["label"]
        rate = winner_stat.get(_RATE_KEY.get(test.metric, "reply_rate"), 0.0)
        # Count for the metric under test — reporting "4 replies / 300 sends" on a
        # test decided by open rate makes the alert unreadable.
        metric_count = winner_stat.get(test.metric, winner_stat.get("replied", 0))
        metric_noun = {"opened": "opens", "clicked": "clicks"}.get(test.metric, "replies")
        subject_only = all(
            getattr(v, "body_template", None) is None for v in variants
        ) and any(getattr(v, "subject_template", None) for v in variants)
        what = "subject line" if subject_only else "copy"
        if default:
            subject = f"🅰️🅱️ {what.capitalize()} test concluded (no clear winner) — step {step.step_number}"
            lead = (
                "No statistically significant difference within the cap "
                f"({HARD_CAP_SENDS} sends/variant or {HARD_CAP_DAYS} days). "
                f"Defaulting to the best performer, variant {label}."
            )
        else:
            subject = f"🅰️🅱️ {what.capitalize()} test winner: variant {label} — step {step.step_number}"
            lead = f"Variant {label} won on {test.metric} rate at ≥{test.confidence_target:.0%} confidence."

        winning_copy = ""
        winner_variant = next(
            (v for v in variants if v.id == winner_stat["variant_id"]), None
        )
        if subject_only and winner_variant is not None:
            won_subject = winner_variant.subject_template or step.subject_template
            winning_copy = f'Winning subject: "{won_subject}"\n'

        body = (
            f"{lead}\n\n"
            f"Winning {test.metric} rate: {rate:.1%} "
            f"({metric_count} {metric_noun} / {winner_stat['sent']} sends).\n"
            f"{winning_copy}"
            f"Variant {label} is now the step's active {what}; future sends use it.\n\n"
            f"Test id: {test.id}"
        )
        await get_notification_service()._send_notification(subject, body)
    except Exception:  # pragma: no cover - notification is best-effort
        logger.info("ab_test_concluded notification failed for test %s", test.id, exc_info=True)
