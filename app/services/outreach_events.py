"""Shared helpers for writing ``OutreachEvent`` rows correctly.

Exists because of one subtle, load-bearing detail: **which step number a reply
belongs to.**

``send_sequence_step`` stamps its ``sent`` event with ``enrollment.current_step``
and *then* increments the counter. So a step-1 send logs ``step_number=1`` and
leaves ``current_step`` at 2. A reply arriving afterwards is a reply *to step 1*,
but the naive ``step_number=enrollment.current_step`` stamps it as step 2.

That mis-attribution is invisible in a timeline view and fatal to anything that
joins sends to outcomes on the step number. ``ab_test_resolver.compute_variant_stats``
does exactly that (``OutreachEvent.step_number == step.step_number``), so with
replies landing one step late every variant's ``replied`` count was 0 — while
``AbTest.metric`` defaults to ``"replied"``. The bandit and the significance test
were running on an all-zero column.

``last_sent_step`` resolves the step a reply is actually answering: the most
recent step this enrollment genuinely sent.

:class:`AttemptTally` is the counting half of the same problem: **how many times
did we put a step on the wire?** ``sent`` and ``bounced`` events do not answer
that on their own, because the two shapes a ``bounced`` event arrives in are
mutually exclusive in one case and additive in the other — see the class
docstring.
"""

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment, OutreachEvent

logger = logging.getLogger(__name__)

#: The event types that describe an attempt to put a sequence step on the wire.
SEND_ATTEMPT_TYPES = ("sent", "bounced")


class AttemptTally:
    """Counts sequence send *attempts* and *bounces* without double-counting.

    A ``bounced`` OutreachEvent is written by two different paths that mean two
    different things, and no counter that treats them alike can be right:

    * **synchronously**, by ``send_sequence_step``, which writes
      ``"sent" if status == "sent" else "bounced"`` — one event per attempt,
      never both. The message never reached anyone;
    * **asynchronously**, by ``bounce_handler._suppress_contact``, when the DSN
      arrives minutes or hours after a send that already wrote its ``sent``
      event. Here the ``bounced`` event is a *second* row about the *same*
      attempt.

    Counting rows therefore over- or under-states the denominator depending on
    which shape the data happens to have, and every consumer picked a different
    wrong answer. ``danger_monitor`` counted ``sent + bounced`` rows, so the
    common async case inflated attempts by the bounce count and the rail that
    pauses a burning domain read 9.1% where the truth was 10% — the one
    direction a brake must not err in. ``lead_metrics.funnel_rates`` divided by
    ``sent + bounces`` for the same reason and reported 50% for a domain where
    every single address was dead. ``funnel_analytics`` divided by ``sent``
    alone, which is right for the async shape and leaves a run where *every*
    attempt failed synchronously with a denominator of zero — a bounce rate of
    0.0, read as healthy.

    Both shapes agree on one thing: the attempt is identified by
    ``(enrollment_id, step_number)``. So::

        attempts = <sent events> + <bounced attempts with no sent event>
        bounces  = <distinct bounced attempts>

    A ``bounced`` event pairs with a ``sent`` event on the same key and adds
    nothing (the async DSN); one with no such partner is a synchronous failure
    and is an attempt in its own right; a re-delivered DSN is free rather than a
    second bounce.

    ``sent`` events are counted as **rows**, not distinct keys. The send-claim
    interlock makes one ``sent`` per ``(enrollment, step)`` the invariant, so the
    two agree — and where they don't, counting rows is the conservative choice:
    collapsing them would shrink the denominator and *inflate* the rates the
    danger rail reads.

    This depends on the bounce being filed against the step that actually sent.
    ``bounce_handler`` uses :func:`last_sent_step` for exactly that; rows written
    before that fix carry ``current_step`` and will not pair up.
    """

    __slots__ = ("_sent_rows", "_sent_keys", "_bounces")

    def __init__(self) -> None:
        self._sent_rows: int = 0
        self._sent_keys: set[tuple] = set()
        self._bounces: set[tuple] = set()

    def add(self, event_type: str, enrollment_id, step_number) -> None:
        """Record one ``sent``/``bounced`` event. Other event types are ignored."""
        if event_type not in SEND_ATTEMPT_TYPES:
            return
        key = (enrollment_id, step_number)
        if event_type == "bounced":
            self._bounces.add(key)
        else:
            self._sent_rows += 1
            self._sent_keys.add(key)

    @property
    def attempts(self) -> int:
        """Steps put on the wire (an async bounce is not a second attempt)."""
        return self._sent_rows + len(self._bounces - self._sent_keys)

    @property
    def bounces(self) -> int:
        """Distinct attempts that bounced (a repeated DSN is not a second bounce)."""
        return len(self._bounces)


async def count_attempts(
    db: AsyncSession, *, where=None, join_enrollment: bool = False
) -> tuple[int, int]:
    """``(attempts, bounces)`` over ``OutreachEvent``, deduplicated per attempt.

    The SQL counterpart of :class:`AttemptTally` — same arithmetic, same
    reasoning — for callers that aggregate in the database rather than scanning
    rows. ``where`` is an optional predicate (already combined);
    ``join_enrollment`` joins ``OutreachEnrollment`` so the predicate may
    reference it.

    Distinct counts are expressed as ``COUNT(*)`` over a ``DISTINCT`` subquery
    rather than ``COUNT(DISTINCT (a, b))``, which Postgres accepts and SQLite
    does not.
    """
    from sqlalchemy import func as _func

    def _events():
        q = select(OutreachEvent.enrollment_id, OutreachEvent.step_number)
        if join_enrollment:
            q = q.join(
                OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id
            )
        if where is not None:
            q = q.where(where)
        return q

    sent_rows = (
        await db.execute(
            select(_func.count()).select_from(
                _events().where(OutreachEvent.event_type == "sent").subquery()
            )
        )
    ).scalar() or 0

    bounce_keys = _events().where(OutreachEvent.event_type == "bounced").distinct().subquery()
    bounces = (
        await db.execute(select(_func.count()).select_from(bounce_keys))
    ).scalar() or 0

    # Bounced attempts with no ``sent`` event of their own: the synchronous
    # failures, which never wrote one. Anything else is the async DSN sitting on
    # top of a send that is already in ``sent_rows``.
    sent_keys = _events().where(OutreachEvent.event_type == "sent").subquery()
    orphan_bounces = (
        await db.execute(
            select(_func.count()).select_from(bounce_keys).outerjoin(
                sent_keys,
                (bounce_keys.c.enrollment_id == sent_keys.c.enrollment_id)
                & (bounce_keys.c.step_number == sent_keys.c.step_number),
            ).where(sent_keys.c.enrollment_id.is_(None))
        )
    ).scalar() or 0

    return sent_rows + orphan_bounces, bounces


async def count_attempts_grouped(
    db: AsyncSession, *, group_by, where=None, join_contact: bool = False
) -> dict:
    """``{key: (attempts, bounces)}`` — :func:`count_attempts`, per group.

    Same arithmetic and the same reasoning as :class:`AttemptTally`; what
    changes is that every group gets its own tally in one pass rather than one
    query per group. ``group_by`` is a list of selectable columns; the key is a
    tuple when there is more than one, and the bare value when there is one.

    It exists because the *per-something* surfaces — per campaign, per campaign
    step, per receiving domain — were the ones still dividing raw
    ``count(sent)`` into raw ``count(bounced)``. Both halves of that are wrong
    and they fail in opposite directions, so neither cancels the other:

    * the numerator counts *rows*, so a mailer-daemon that retries books a
      second bounce against one send;
    * the denominator counts ``sent`` rows only, and a **synchronous** failure
      writes ``bounced`` with no ``sent`` event of its own — so an attempt that
      never reached anyone is missing from the denominator entirely.

    The second is the one that matters, because at the limit it inverts. A
    campaign or domain whose every send failed synchronously has no ``sent``
    rows at all, so its bounce rate divides by zero, comes back ``0.0``, and
    reads as the healthiest thing on the board — while the sample-size gate
    beside it then drops the row for having sent nothing.

    ``OutreachEnrollment`` is always joined, so ``group_by`` and ``where`` may
    reference it. ``join_contact`` adds the contact behind the enrollment, for
    callers grouping by the address that was mailed.
    """
    from sqlalchemy import func as _func

    def _events():
        q = select(
            *group_by, OutreachEvent.enrollment_id, OutreachEvent.step_number
        ).join(
            OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id
        )
        if join_contact:
            from app.models.contact import Contact

            q = q.join(Contact, OutreachEnrollment.contact_id == Contact.id)
        if where is not None:
            q = q.where(where)
        return q

    width = len(group_by)

    def _key(row):
        return tuple(row[:width]) if width > 1 else row[0]

    tallies: dict = {}

    def _fold(rows, index, *, add=False):
        for row in rows:
            key, n = _key(row), int(row[-1])
            slot = tallies.setdefault(key, [0, 0])
            slot[index] = slot[index] + n if add else n

    sent_q = _events().where(OutreachEvent.event_type == "sent").subquery()
    sent_cols = list(sent_q.c)[:width]
    _fold(
        (await db.execute(
            select(*sent_cols, _func.count()).group_by(*sent_cols)
        )).all(),
        0,
    )

    # Distinct ``(enrollment, step)``, so a redelivered DSN is free rather than
    # a second bounce — the whole point of the dedup.
    bounce_q = (
        _events().where(OutreachEvent.event_type == "bounced").distinct().subquery()
    )
    bounce_cols = list(bounce_q.c)[:width]
    _fold(
        (await db.execute(
            select(*bounce_cols, _func.count()).group_by(*bounce_cols)
        )).all(),
        1,
    )

    # A bounced attempt with no ``sent`` partner is a synchronous failure and an
    # attempt in its own right. Anything else is the async DSN sitting on top of
    # a send already counted above.
    sent_keys = _events().where(OutreachEvent.event_type == "sent").subquery()
    _fold(
        (await db.execute(
            select(*bounce_cols, _func.count())
            .select_from(bounce_q)
            .outerjoin(
                sent_keys,
                (bounce_q.c.enrollment_id == sent_keys.c.enrollment_id)
                & (bounce_q.c.step_number == sent_keys.c.step_number),
            )
            .where(sent_keys.c.enrollment_id.is_(None))
            .group_by(*bounce_cols)
        )).all(),
        0,
        add=True,
    )

    return {k: (v[0], v[1]) for k, v in tallies.items()}


async def count_attempts_by_sequence(db: AsyncSession, *, where=None) -> dict:
    """``{sequence_id: (attempts, bounces)}`` — a campaign's tally.

    A campaign is a sequence here, and enrollments are not shared between them,
    so a portfolio total is the sum of these.
    """
    return await count_attempts_grouped(
        db, group_by=[OutreachEnrollment.sequence_id], where=where
    )


async def count_attempts_by_sequence_step(db: AsyncSession, *, where=None) -> dict:
    """``{(sequence_id, step_number): (attempts, bounces)}`` — one step's tally.

    The funnel buckets per step, and a step is where the off-by-one in bounce
    attribution shows up (see ``last_sent_step``): a bounce filed against the
    wrong step leaves one bucket reporting no bounces and the next dividing
    bounces by a ``sent`` of zero.
    """
    return await count_attempts_grouped(
        db,
        group_by=[OutreachEnrollment.sequence_id, OutreachEvent.step_number],
        where=where,
    )


async def count_attempts_by_contact_email(db: AsyncSession, *, where=None) -> dict:
    """``{email: (attempts, bounces)}`` — keyed by the address we mailed.

    For the receiving-domain report, which folds addresses into domains in
    Python because extracting a domain in SQL needs a dialect branch this
    codebase does not carry.

    ``Contact.email`` is not normalised on write, so addresses are compared
    lower-cased here for the same reason ten other call sites do it: the same
    mailbox stored two ways would otherwise split one domain's traffic across
    two keys and halve its measured bounce rate.
    """
    from sqlalchemy import func as _func

    from app.models.contact import Contact

    return await count_attempts_grouped(
        db,
        group_by=[_func.lower(Contact.email).label("email")],
        where=where,
        join_contact=True,
    )


async def last_sent_step(db: AsyncSession, enrollment: OutreachEnrollment) -> int:
    """The step number of the most recent ``sent`` event for this enrollment.

    This is the step a reply is responding to. Falls back to
    ``max(current_step - 1, 1)`` when no send is logged (legacy enrollments
    predating event logging), which reverses the off-by-one the old code
    introduced rather than repeating it.

    Ordered by ``step_number``, not ``occurred_at``: an enrollment only ever
    advances forward (``current_step += 1``), so the highest step sent is by
    definition the latest one. Timestamps would tie — and resolve arbitrarily —
    whenever two events share a ``server_default`` clock reading.
    """
    row = (
        await db.execute(
            select(OutreachEvent.step_number)
            .where(
                OutreachEvent.enrollment_id == enrollment.id,
                OutreachEvent.event_type == "sent",
            )
            .order_by(OutreachEvent.step_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return int(row)
    return max(int(enrollment.current_step or 1) - 1, 1)


async def sent_step_or_none(
    db: AsyncSession, enrollment: OutreachEnrollment
) -> Optional[int]:
    """:func:`last_sent_step` without the fallback — ``None`` if nothing was sent.

    The fallback is right for a reply, which is proof that *something* reached
    the recipient even if the ``sent`` event predates event logging. It is wrong
    for a bounce, where "this enrollment never sent" is a real and common state
    (the prospector can enroll a contact into a second sequence that hasn't run
    yet, and ``record_bounce`` stops every active enrollment for the address).
    Booking a bounce there invents an attempt that never happened — and one with
    no ``sent`` partner reads to :class:`AttemptTally` as a synchronous failure,
    so it lands in the denominator as well as the numerator.
    """
    row = (
        await db.execute(
            select(OutreachEvent.step_number)
            .where(
                OutreachEvent.enrollment_id == enrollment.id,
                OutreachEvent.event_type == "sent",
            )
            .order_by(OutreachEvent.step_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(row) if row is not None else None


async def build_reply_event(
    db: AsyncSession,
    enrollment: OutreachEnrollment,
    *,
    metadata: Optional[dict] = None,
) -> OutreachEvent:
    """A ``replied`` event attributed to the step that was actually sent.

    Side effect (Feature 3): a reply is the strongest engagement signal, so this
    also shortens the enrollment's adaptive delay factor and reschedules any
    pending step. The caller commits the enrollment along with the event.
    """
    step = await last_sent_step(db, enrollment)
    from datetime import datetime, timezone

    from app.services import adaptive_delay

    await adaptive_delay.register_engagement(db, enrollment)

    return OutreachEvent(
        enrollment_id=enrollment.id,
        step_number=step,
        event_type="replied",
        occurred_at=datetime.now(timezone.utc),
        event_metadata=metadata or {},
    )
