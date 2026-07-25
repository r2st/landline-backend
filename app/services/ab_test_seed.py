"""Seed subject-line A/B tests from the static variant bank (Feature 4).

:data:`app.services.outreach_templates.SUBJECT_AB_VARIANTS` already holds 2-3
hand-written subject alternatives per sequence step — the cheapest, highest-
signal thing to A/B in cold email — but nothing ever turned them into live
tests, so every send used the one static subject and the variants were dead
copy. :func:`seed_subject_ab_tests` walks a sequence's steps and, for each step
that has variants defined, creates one :class:`AbTest` plus its
:class:`AbTestVariant` rows so :func:`app.services.ab_test_resolver.resolve_variant`
starts rotating subjects on the next cold send.

Design choices:

* **A is the control, and it inherits the step's own subject** (``NULL``
  template) rather than duplicating it — a step whose subject is later edited
  keeps its control in sync automatically. The listed variant that equals the
  step's current subject is treated as that control and not duplicated as a
  challenger.
* **Idempotent.** A step that already has an ``AbTest`` is skipped, so seeding
  can run repeatedly (on every sequence seed, or as a manual backfill) without
  minting duplicates.
* **Body inherits the step** for every variant — this is a *subject-line* test,
  so only the subject varies; the body is held constant to keep the comparison
  clean.
"""

import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ab_test import AB_METRICS, AB_STRATEGIES, VARIANT_LABELS, AbTest, AbTestVariant
from app.models.sequence import SequenceStep
from app.services.outreach_templates import SUBJECT_AB_VARIANTS

logger = logging.getLogger(__name__)


async def seed_subject_ab_tests(
    db: AsyncSession,
    sequence_id: UUID,
    *,
    variants_by_step: Optional[dict[int, list[str]]] = None,
    strategy: str = "even",
    # Subject-line tests are scored on OPENS by default. The subject is the whole
    # lever on whether a message gets opened and has close to no bearing on what
    # happens after it; scoring one on reply rate measures the body's work and
    # credits it to the subject, and needs an order of magnitude more volume to
    # reach significance (cold reply rates run ~2-5%, open rates ~30-50%).
    metric: str = "opened",
    min_sample: int = 50,
    confidence_target: float = 0.95,
) -> dict:
    """Create subject-line A/B tests for a sequence's steps (idempotent).

    For each step whose ``step_number`` appears in ``variants_by_step`` with at
    least one subject that differs from the step's current subject, create an
    ``AbTest`` (control A + up to three challengers B-D). Steps that already have
    a test, or have no distinct alternatives, are skipped.

    Returns ``{"created": [step_numbers...], "skipped": [step_numbers...]}``.
    """
    variants_by_step = variants_by_step if variants_by_step is not None else SUBJECT_AB_VARIANTS
    if strategy not in AB_STRATEGIES:
        raise ValueError(f"strategy must be one of {AB_STRATEGIES}")
    if metric not in AB_METRICS:
        raise ValueError(f"metric must be one of {AB_METRICS}")

    steps = list(
        (
            await db.execute(
                select(SequenceStep).where(SequenceStep.sequence_id == sequence_id)
            )
        ).scalars().all()
    )
    steps_by_number = {s.step_number: s for s in steps}

    created: list[int] = []
    skipped: list[int] = []

    for step_number, subjects in variants_by_step.items():
        step = steps_by_number.get(step_number)
        if step is None:
            continue

        # Already under test? Leave it — re-seeding must never duplicate.
        existing = (
            await db.execute(
                select(func.count())
                .select_from(AbTest)
                .where(AbTest.sequence_step_id == step.id)
            )
        ).scalar_one()
        if existing:
            skipped.append(step_number)
            continue

        control_subject = (step.subject_template or "").strip()
        # Challengers = the listed subjects that actually differ from the control
        # (so the control isn't duplicated), de-duplicated, capped to fill B-D.
        challengers: list[str] = []
        for subj in subjects:
            s = (subj or "").strip()
            if not s or s == control_subject or s in challengers:
                continue
            challengers.append(s)
        challengers = challengers[: len(VARIANT_LABELS) - 1]  # leave room for A

        if not challengers:
            # Nothing to test against the control.
            skipped.append(step_number)
            continue

        test = AbTest(
            sequence_step_id=step.id,
            status="running",
            strategy=strategy,
            metric=metric,
            min_sample=min_sample,
            confidence_target=confidence_target,
        )
        db.add(test)

        # A = control, inherits the step subject (NULL template).
        db.add(
            AbTestVariant(
                sequence_step_id=step.id,
                label=VARIANT_LABELS[0],
                subject_template=None,
                body_template=None,
                is_control=True,
                is_active=True,
            )
        )
        for idx, subj in enumerate(challengers, start=1):
            db.add(
                AbTestVariant(
                    sequence_step_id=step.id,
                    label=VARIANT_LABELS[idx],
                    subject_template=subj,
                    body_template=None,  # subject-only test
                    is_control=False,
                    is_active=True,
                )
            )
        created.append(step_number)
        logger.info(
            "Seeded subject A/B test for step %d (1 control + %d challengers)",
            step_number, len(challengers),
        )

    await db.flush()
    return {"created": created, "skipped": skipped}


async def generate_subject_test_for_step(
    db: AsyncSession,
    step: SequenceStep,
    *,
    count: int = 3,
    strategy: str = "even",
    metric: str = "opened",
    min_sample: int = 50,
    confidence_target: float = 0.95,
    industry: Optional[str] = None,
    ai=None,
) -> dict:
    """Write subject-line challengers for one step with AI and start the test.

    The static bank in :data:`SUBJECT_AB_VARIANTS` only covers the steps someone
    hand-wrote alternatives for; every other step — every custom sequence, every
    vertical added since — has exactly one subject and no way to find a better
    one. This generates challengers for whatever step you point it at.

    Same shape as the seeded tests: **A is the control and inherits the step's
    subject** (so editing the step keeps the control in sync), challengers vary
    the subject only, the body is held constant so the comparison is clean, and
    the whole thing is scored on open rate.

    Generation never fails the caller —
    :func:`app.services.ab_testing_service.generate_subject_variants` falls back
    to deterministic transforms when no LLM is reachable. Raises ``ValueError``
    if the step already has a test (one test per step) or if nothing distinct
    could be generated.
    """
    from app.services.ab_testing_service import generate_subject_variants

    if strategy not in AB_STRATEGIES:
        raise ValueError(f"strategy must be one of {AB_STRATEGIES}")
    if metric not in AB_METRICS:
        raise ValueError(f"metric must be one of {AB_METRICS}")

    existing = (
        await db.execute(
            select(func.count()).select_from(AbTest).where(AbTest.sequence_step_id == step.id)
        )
    ).scalar_one()
    if existing:
        raise ValueError("an A/B test already exists for this step")

    control_subject = (step.subject_template or "").strip()
    generated = await generate_subject_variants(
        control_subject,
        industry=industry,
        body_preview=(step.body_template or "")[:200],
        count=count,
        ai=ai,
    )

    challengers: list[str] = []
    for subj in generated:
        s = (subj or "").strip()
        if not s or s == control_subject or s in challengers:
            continue
        challengers.append(s)
    challengers = challengers[: len(VARIANT_LABELS) - 1]
    if not challengers:
        raise ValueError("could not generate any subject distinct from the step's own")

    test = AbTest(
        sequence_step_id=step.id,
        status="running",
        strategy=strategy,
        metric=metric,
        min_sample=min_sample,
        confidence_target=confidence_target,
    )
    db.add(test)
    db.add(
        AbTestVariant(
            sequence_step_id=step.id,
            label=VARIANT_LABELS[0],
            subject_template=None,  # inherit the step subject
            body_template=None,
            is_control=True,
            is_active=True,
        )
    )
    for idx, subj in enumerate(challengers, start=1):
        db.add(
            AbTestVariant(
                sequence_step_id=step.id,
                label=VARIANT_LABELS[idx],
                subject_template=subj,
                body_template=None,  # subject-only test
                is_control=False,
                is_active=True,
            )
        )
    await db.flush()
    logger.info(
        "Generated subject A/B test for step %s (1 control + %d challengers, metric=%s)",
        step.id, len(challengers), metric,
    )
    return {
        "test": test,
        "control_subject": control_subject,
        "challengers": challengers,
    }
