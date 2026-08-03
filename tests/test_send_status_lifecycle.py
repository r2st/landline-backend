"""``EmailSend.status`` is a lifecycle column, and every counter has to know it.

A row starts ``pending``, becomes ``sent`` when the transport accepts it, and
then **keeps moving**: ``app.routers.tracking`` advances it up a rank ladder on
every engagement signal — ``sent → opened → clicked → replied``. So the moment a
recipient opens a message, that send's status is no longer ``"sent"``.

Six counters were written as ``status == "sent"``. Each therefore answered "how
many emails did we send that nobody engaged with?" while reading as "how many
emails did we send". The two questions agree exactly when the product isn't
working, which is why the mistake survives review — **and why no existing test
caught it: a fixture that sends and never opens cannot distinguish them.** Every
test below opens or replies to some of its own sends for that reason.

The direction is what makes this expensive. It is a count that *shrinks as
engagement rises*, so it degrades quietly and in proportion to success, and the
worst-affected counter is not a dashboard tile — ``send_limiter._db_sent_since``
is the seed for the daily cap's Redis counter, whose own docstring says an
undercounted seed "hands back allowance the mailbox already spent, which is the
cap failing in the one direction that matters."
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_template import (
    DELIVERED_STATUSES,
    EmailSend,
    NON_WIRE_STATUSES,
    WIRE_STATUSES,
)


async def _send(db: AsyncSession, *, status="sent", when=None, **kw):
    row = EmailSend(
        to_email=kw.pop("to_email", f"{uuid.uuid4().hex[:8]}@acme.com"),
        subject="Quick question",
        status=status,
        sent_at=when or datetime.now(timezone.utc),
        **kw,
    )
    db.add(row)
    return row


# ---------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------


def test_the_tracking_ladder_is_entirely_inside_the_wire_statuses():
    """Whatever ``tracking`` can advance a landed send *to* still counts as sent.

    This is the coupling the bug lived in: the ladder is declared in
    ``app.routers.tracking`` and the counters are declared elsewhere, so the two
    drifted with nothing to notice. Assert against the real ladder rather than a
    copied-out list, so adding a rung there fails here instead of silently
    deleting sends from the daily cap.
    """
    from app.routers.tracking import _STATUS_RANK

    assert set(_STATUS_RANK) <= set(WIRE_STATUSES)


def test_no_status_is_both_a_send_and_a_non_send():
    assert not (set(WIRE_STATUSES) & NON_WIRE_STATUSES)


def test_bounced_reached_the_wire_but_was_never_delivered():
    """The one status the two lists deliberately disagree about.

    A bounce spent the mailbox's allowance — it went out and was rejected
    afterwards — so it belongs in a send count and in a cap. It was never
    delivered, so putting it in the denominator of an *open rate* reports a list
    problem as a copy problem.
    """
    assert "bounced" in WIRE_STATUSES
    assert "bounced" not in DELIVERED_STATUSES
    assert set(DELIVERED_STATUSES) == set(WIRE_STATUSES) - {"bounced"}


def test_the_statuses_that_never_left_are_not_sends():
    """Each of these is written by a path that explicitly did not send."""
    for status in ("skipped", "suppressed", "halted", "error", "failed", "scheduled", "pending"):
        assert status in NON_WIRE_STATUSES
        assert status not in WIRE_STATUSES


# ---------------------------------------------------------------------------
# The daily cap seed — the counter where being wrong costs most
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cap_seed_does_not_shrink_when_recipients_engage(
    db_session: AsyncSession,
):
    """Five sends stay five sends after three of them are opened.

    Before this, the seed fell to two. A Redis restart or eviction mid-day
    re-seeds the daily counter from this number, so the mailbox was handed back
    one slot per engaged recipient — and handed back *more* of them the better
    the campaign was landing. On a cold domain under a warm-up ramp that is the
    exact overshoot the cap exists to prevent.
    """
    from app.services.send_limiter import SendLimiter

    now = datetime.now(timezone.utc)
    today = now.replace(hour=9, minute=0, second=0, microsecond=0)
    for _ in range(5):
        await _send(db_session, when=today)
    await db_session.commit()

    limiter = SendLimiter()
    assert await limiter._db_sent_today(db_session, now) == 5

    rows = (await db_session.execute(select(EmailSend))).scalars().all()
    rows[0].status = "opened"
    rows[1].status = "clicked"
    rows[2].status = "replied"
    await db_session.commit()

    assert await limiter._db_sent_today(db_session, now) == 5


@pytest.mark.asyncio
async def test_the_cap_seed_still_ignores_mail_that_never_left(
    db_session: AsyncSession,
):
    """The other half. A seed that counts held mail is a cap that refuses early.

    Widening the vocabulary is only correct if it stayed narrow where it was
    right to be: a warm-up hold, a suppressed recipient and a spam-gate block
    all write ``EmailSend`` rows, and counting them would spend a day's
    allowance on mail nobody received.
    """
    from app.services.send_limiter import SendLimiter

    now = datetime.now(timezone.utc)
    today = now.replace(hour=9, minute=0, second=0, microsecond=0)
    await _send(db_session, when=today)
    for status in ("skipped", "suppressed", "halted", "error", "failed", "pending"):
        await _send(db_session, status=status, when=today)
    await db_session.commit()

    assert await SendLimiter()._db_sent_today(db_session, now) == 1


@pytest.mark.asyncio
async def test_a_bounce_still_spent_the_days_allowance(db_session: AsyncSession):
    """A bounce is mail that went out. The mailbox does not get that slot back."""
    from app.services.send_limiter import SendLimiter

    now = datetime.now(timezone.utc)
    today = now.replace(hour=9, minute=0, second=0, microsecond=0)
    await _send(db_session, when=today)
    await _send(db_session, status="bounced", when=today)
    await db_session.commit()

    assert await SendLimiter()._db_sent_today(db_session, now) == 2


@pytest.mark.asyncio
async def test_the_company_pacing_gap_sees_an_opened_send(
    db_session: AsyncSession, sample_company, sample_contact,
):
    """Emailing a colleague is refused for N hours — including after an open.

    ``can_send_to_company`` stops a burst to four addresses at one firm, which
    is the canonical spam pattern. It read ``status == "sent"``, so the gap
    silently lifted the moment the colleague we already emailed opened it —
    turning an engaged account into the one we were most likely to blast.
    """
    from app.models.contact import Contact
    from app.services.send_limiter import SendLimiter

    now = datetime.now(timezone.utc)
    colleague = Contact(
        company_id=sample_company.id, email="cfo@acme.com", first_name="Dana",
    )
    db_session.add(colleague)
    await db_session.flush()

    await _send(
        db_session, contact_id=colleague.id, when=now - timedelta(minutes=30),
        status="opened", to_email="cfo@acme.com",
    )
    await db_session.commit()

    # We are about to email ``sample_contact`` at the same firm; the colleague
    # is one of the peers the gap is measured against.
    allowed, reason, wait = await SendLimiter().can_send_to_company(
        db_session, sample_company.id, exclude_contact_id=sample_contact.id, now=now,
    )
    assert allowed is False, reason
    assert wait > timedelta(0)


# ---------------------------------------------------------------------------
# The reporting surfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_dashboard_send_count_does_not_fall_as_opens_arrive(
    db_session: AsyncSession,
):
    """The tile beside this one counts opens. This one used to lose them.

    That is the >100% open rate the module docstring warns about, reached from
    the other side: opens counted on ``OutreachEvent``, sends dropped from
    ``EmailSend`` by the very act of being opened.
    """
    from app.routers.dashboard import _count_emails_sent

    for status in ("sent", "opened", "clicked", "replied"):
        await _send(db_session, status=status)
    for status in ("skipped", "scheduled", "error"):
        await _send(db_session, status=status)
    await db_session.commit()

    assert await _count_emails_sent(db_session) == 4


@pytest.mark.asyncio
async def test_ab_stats_divide_by_delivered_mail_only(db_session: AsyncSession):
    """A subject line must not win on how many of its recipients unsubscribed.

    The variant is stamped before the send is attempted, so a suppressed
    recipient, a warm-up hold or a spent cap still leaves an "A" or "B" row.
    Assignment is ``i % 2`` over an arbitrary contact list, so which arm absorbs
    them has nothing to do with the copy — it is noise landing directly in the
    denominator of the comparison the page exists to make.
    """
    from app.models.email_template import EmailTemplate
    from app.routers.email_templates import get_ab_stats

    template = EmailTemplate(name="Subject test", subject="Hi", body="<p>Hi</p>")
    db_session.add(template)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    # Arm A: 10 delivered, 5 opened.
    for i in range(10):
        await _send(
            db_session, template_id=template.id, ab_variant="A",
            status="opened" if i < 5 else "sent",
            opened_at=now if i < 5 else None,
        )
    # Arm B: 10 delivered, 5 opened — identical copy performance — plus 10
    # recipients who were suppressed or held and never received anything.
    for i in range(10):
        await _send(
            db_session, template_id=template.id, ab_variant="B",
            status="opened" if i < 5 else "sent",
            opened_at=now if i < 5 else None,
        )
    for _ in range(10):
        await _send(
            db_session, template_id=template.id, ab_variant="B", status="skipped",
        )
    await db_session.commit()

    stats = await get_ab_stats(template_id=template.id, db=db_session)
    by_variant = {s.variant: s for s in stats.stats}

    assert by_variant["A"].total == 10
    assert by_variant["B"].total == 10
    # The two arms performed identically, so the page must say so. It used to
    # report B at 25% against A's 50% and declare A the winner.
    assert by_variant["A"].open_rate == by_variant["B"].open_rate == 50.0


@pytest.mark.asyncio
async def test_campaign_analytics_rates_do_not_collapse_when_sending_is_blocked(
    db_session: AsyncSession,
):
    """Every rate on the page used to drop when mail was being *prevented*.

    The filter was ``status != "scheduled"`` — the widest possible reading of "a
    send". It admitted skipped, suppressed, halted, failed and the transient
    ``pending`` row the compose loop writes before it tries. Those are the
    denominator four percentages divide by, so the page looked worst exactly
    when someone opened it to find out why nothing was going out.
    """
    from app.routers.email_templates import get_campaign_analytics

    now = datetime.now(timezone.utc)
    for _ in range(10):
        await _send(db_session, status="opened", opened_at=now)
    # A kill switch, a suppression list and a warm-up ramp hold ninety more.
    for _ in range(30):
        await _send(db_session, status="halted")
    for _ in range(30):
        await _send(db_session, status="suppressed")
    for _ in range(30):
        await _send(db_session, status="skipped")
    await db_session.commit()

    analytics = await get_campaign_analytics(db=db_session)
    assert analytics.total_sent_all == 10
    assert analytics.open_rate == 100.0


@pytest.mark.asyncio
async def test_campaign_analytics_bounce_rate_counts_itself(
    db_session: AsyncSession,
):
    """A bounce is in its own numerator, so it must be in its own denominator.

    Engagement rates divide by delivered mail; the bounce rate cannot, or a list
    that bounces every address reports a bounce rate of infinity or of zero
    depending on which way the division falls.
    """
    from app.routers.email_templates import get_campaign_analytics

    for _ in range(9):
        await _send(db_session, status="sent")
    await _send(db_session, status="bounced")
    await db_session.commit()

    analytics = await get_campaign_analytics(db=db_session)
    assert analytics.total_sent_all == 10
    assert analytics.bounce_rate == 10.0
