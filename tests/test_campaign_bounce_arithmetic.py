"""The campaign surfaces were the last place dividing raw counts into a bounce rate.

``outreach_events.AttemptTally`` is the canonical arithmetic and states the rule:
an attempt is ``(enrollment_id, step_number)``, a redelivered DSN is not a second
bounce, and a **synchronous** failure writes a ``bounced`` event with no ``sent``
partner — so it is an attempt that a count of ``sent`` rows cannot see. The
danger rail, ``lead_metrics`` and ``funnel_analytics`` were each corrected to it.
``campaign_analytics`` was not, and computed ``rate(count(bounced), count(sent))``.

Both halves are wrong and they err in opposite directions, so neither cancels the
other. The one that matters is the denominator, because at the limit it inverts:
a campaign whose every send failed synchronously has no ``sent`` rows at all, so
its bounce rate divides by zero, comes back ``0.0``, and reads as the healthiest
campaign on the board — while the sample-size gate then drops it for having sent
nothing, and the ``include_empty`` filter drops it from the report entirely.

That is not only a display problem. ``campaign_dashboard`` raises a per-campaign
bounce alert against ``danger_bounce_rate_threshold``, and its own message says
the campaign is "past the auto-pause line the danger monitor applies to the whole
fleet" — a claim of comparability with a rail that computes the rate the other
way.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence

ANALYTICS = "/api/v1/dialtone/campaigns/analytics"
DASHBOARD = "/api/v1/dialtone/campaigns/dashboard"


async def _campaign(db: AsyncSession, name: str) -> OutreachSequence:
    seq = OutreachSequence(id=_uuid.uuid4(), name=name, is_active=True, status="active")
    db.add(seq)
    await db.flush()
    return seq


async def _attempt(
    db: AsyncSession,
    company,
    sequence,
    *,
    sent: bool,
    bounced: bool = False,
    dsn_copies: int = 1,
    when: datetime | None = None,
):
    """One attempt: an enrollment, and whichever events describe its outcome.

    ``sent=False, bounced=True`` is the **synchronous** failure — the transport
    refused it, so nothing was ever accepted and no ``sent`` event exists.
    ``dsn_copies`` is a mailer-daemon retrying the same notification.
    """
    when = when or datetime.now(timezone.utc)
    contact = Contact(
        id=_uuid.uuid4(), company_id=company.id,
        email=f"a-{_uuid.uuid4().hex[:8]}@acme.com",
        first_name="Sam", source="test",
    )
    db.add(contact)
    enr = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id,
        current_step=1, status="active",
    )
    db.add(enr)
    if sent:
        db.add(OutreachEvent(
            id=_uuid.uuid4(), enrollment_id=enr.id, step_number=1,
            event_type="sent", occurred_at=when, event_metadata={},
        ))
    if bounced:
        for _ in range(dsn_copies):
            db.add(OutreachEvent(
                id=_uuid.uuid4(), enrollment_id=enr.id, step_number=1,
                event_type="bounced", occurred_at=when, event_metadata={},
            ))
    await db.flush()
    return enr


def _row(body, name):
    for r in body["campaigns"]:
        if r["name"] == name:
            return r
    return None


@pytest.mark.asyncio
async def test_a_campaign_that_only_ever_bounced_is_not_reported_healthy(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """The inversion, and the reason this is a correctness fix and not a polish one.

    Every attempt failed synchronously, so there are no ``sent`` rows. Divided by
    ``count(sent)`` the rate is ``0/0 → 0.0`` and the campaign ranks as the
    cleanest in the portfolio; divided by attempts it is 100%, which is what
    happened.
    """
    seq = await _campaign(db_session, "All Dead")
    for _ in range(25):
        await _attempt(db_session, sample_company, seq, sent=False, bounced=True)
    await db_session.commit()

    body = (await client.get(ANALYTICS, params={"days": 30})).json()
    row = _row(body, "All Dead")
    assert row is not None, (
        "a campaign with no successful sends must still be reported — gating the "
        "report on `sent` dropped exactly the campaign doing the damage"
    )
    assert row["attempts"] == 25
    assert row["bounced"] == 25
    assert row["bounce_rate"] == 1.0, (
        f"every attempt bounced; reported {row['bounce_rate']}"
    )


@pytest.mark.asyncio
async def test_that_campaign_now_raises_a_critical_alert(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """The consequence of the above, on the surface that is supposed to act.

    The sample gate read ``sent``, which is zero here, so the one campaign that
    could not possibly be healthy was the one campaign that could never raise a
    bounce alert.
    """
    seq = await _campaign(db_session, "All Dead")
    for _ in range(25):
        await _attempt(db_session, sample_company, seq, sent=False, bounced=True)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()
    bounce_alerts = [a for a in body["alerts"] if a["kind"] == "bounce_rate"]
    assert bounce_alerts, f"no bounce alert raised; got {[a['kind'] for a in body['alerts']]}"
    assert bounce_alerts[0]["severity"] == "critical"
    assert bounce_alerts[0]["value"] == 1.0


@pytest.mark.asyncio
async def test_a_retrying_mailer_daemon_is_one_bounce(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """The numerator half. Duplicate DSNs are ordinary and must stay free.

    This is the same guarantee ``count_attempts`` gives the kill-switch rail;
    the campaign table simply was not asking for it.
    """
    seq = await _campaign(db_session, "Retried")
    for _ in range(39):
        await _attempt(db_session, sample_company, seq, sent=True)
    await _attempt(
        db_session, sample_company, seq, sent=True, bounced=True, dsn_copies=6
    )
    await db_session.commit()

    body = (await client.get(ANALYTICS, params={"days": 30})).json()
    row = _row(body, "Retried")
    assert row["attempts"] == 40
    assert row["bounced"] == 1, "six deliveries of one DSN are one bounce"
    assert row["bounce_rate"] == 0.025


@pytest.mark.asyncio
async def test_an_async_bounce_is_not_a_second_attempt(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """The boundary that keeps the denominator honest in the other direction.

    The ordinary bounce is asynchronous: the message was accepted (``sent``) and
    the DSN arrived hours later (``bounced``). Counting the pair as two attempts
    would understate every bounce rate — the mirror of the bug being fixed.
    """
    seq = await _campaign(db_session, "Async")
    for _ in range(18):
        await _attempt(db_session, sample_company, seq, sent=True)
    for _ in range(2):
        await _attempt(db_session, sample_company, seq, sent=True, bounced=True)
    await db_session.commit()

    body = (await client.get(ANALYTICS, params={"days": 30})).json()
    row = _row(body, "Async")
    assert row["attempts"] == 20, "20 attempts, not 22"
    assert row["sent"] == 20
    assert row["bounced"] == 2
    assert row["bounce_rate"] == 0.1


@pytest.mark.asyncio
async def test_the_mixed_case_counts_each_shape_once(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """Both failure shapes on one campaign, which is what a real bad day is.

    16 clean sends, 2 accepted-then-bounced, 2 refused outright, and one of the
    DSNs redelivered. Attempts = 20, bounces = 4.
    """
    seq = await _campaign(db_session, "Mixed")
    for _ in range(16):
        await _attempt(db_session, sample_company, seq, sent=True)
    await _attempt(db_session, sample_company, seq, sent=True, bounced=True)
    await _attempt(
        db_session, sample_company, seq, sent=True, bounced=True, dsn_copies=3
    )
    for _ in range(2):
        await _attempt(db_session, sample_company, seq, sent=False, bounced=True)
    await db_session.commit()

    body = (await client.get(ANALYTICS, params={"days": 30})).json()
    row = _row(body, "Mixed")
    assert row["sent"] == 18, "the two refusals were never accepted"
    assert row["attempts"] == 20, "…but they were attempted"
    assert row["bounced"] == 4
    assert row["bounce_rate"] == 0.2


@pytest.mark.asyncio
async def test_engagement_rates_still_divide_by_sends(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """The deliberate asymmetry, so it is not "made consistent" later.

    ``attempts`` is the right denominator for a bounce rate and the wrong one
    for an open rate: a message the transport refused was never in anyone's
    inbox, so folding it into the denominator would report an open rate for mail
    that does not exist. ``sent`` keeps meaning "accepted".
    """
    seq = await _campaign(db_session, "Engagement")
    for _ in range(10):
        await _attempt(db_session, sample_company, seq, sent=True)
    for _ in range(10):
        await _attempt(db_session, sample_company, seq, sent=False, bounced=True)
    await db_session.commit()

    # Five of the ten delivered messages were opened.
    enrolls = (
        await db_session.execute(
            OutreachEnrollment.__table__.select().where(
                OutreachEnrollment.sequence_id == seq.id
            )
        )
    ).fetchall()
    now = datetime.now(timezone.utc)
    for row in enrolls[:5]:
        db_session.add(OutreachEvent(
            id=_uuid.uuid4(), enrollment_id=row.id, step_number=1,
            event_type="opened", occurred_at=now, event_metadata={},
        ))
    await db_session.commit()

    body = (await client.get(ANALYTICS, params={"days": 30})).json()
    row = _row(body, "Engagement")
    assert row["sent"] == 10
    assert row["attempts"] == 20
    assert row["bounce_rate"] == 0.5, "half of what was attempted bounced"
    assert row["open_rate"] == 0.5, (
        "…and half of what was *delivered* was opened — the refused half was "
        "never in an inbox to open"
    )


@pytest.mark.asyncio
async def test_the_portfolio_total_uses_the_same_denominator(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """A total computed a different way from its rows is how two screens disagree."""
    healthy = await _campaign(db_session, "Healthy")
    burning = await _campaign(db_session, "Burning")
    for _ in range(60):
        await _attempt(db_session, sample_company, healthy, sent=True)
    for _ in range(20):
        await _attempt(db_session, sample_company, burning, sent=False, bounced=True)
    await db_session.commit()

    body = (await client.get(DASHBOARD, params={"days": 30})).json()
    portfolio = body["portfolio"]
    assert portfolio["attempts"] == 80
    assert portfolio["bounced"] == 20
    assert portfolio["bounce_rate"] == 0.25, (
        "the blend must be bounces over attempts, matching every row it sums"
    )


@pytest.mark.asyncio
async def test_the_previous_period_is_measured_the_same_way(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """A trend differencing a corrected rate against a raw one reports the method.

    Both windows must use attempts, or a campaign whose bounce rate never moved
    shows a delta that is entirely an artefact of how each side was computed.
    """
    seq = await _campaign(db_session, "Steady")
    now = datetime.now(timezone.utc)
    for window in (now - timedelta(days=5), now - timedelta(days=40)):
        for _ in range(38):
            await _attempt(db_session, sample_company, seq, sent=True, when=window)
        for _ in range(2):
            await _attempt(
                db_session, sample_company, seq, sent=False, bounced=True, when=window
            )
    await db_session.commit()

    body = (
        await client.get(ANALYTICS, params={"days": 30, "compare": "true"})
    ).json()
    row = _row(body, "Steady")
    assert row["trend"] is not None
    assert row["bounce_rate"] == 0.05
    assert row["trend"]["previous_bounce_rate"] == 0.05
    assert row["trend"]["bounce_rate_delta"] == 0.0, (
        "an unchanged campaign must show no movement"
    )
