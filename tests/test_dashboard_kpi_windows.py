"""The dashboard KPIs are windowed to the period their labels claim.

Two of them weren't:

* ``replies_this_week`` counted every reply ever recorded. The comment beside it
  claimed ``OutreachEvent`` had no timestamp; it has ``occurred_at``, non-null,
  and the activity feed sorts on it.
* ``deals_won_this_month`` windowed on ``Deal.created_at`` — when the record was
  opened, not when it was won. A deal with a sales cycle longer than a month
  never appeared in the month its revenue landed.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence

NOW = datetime.now(timezone.utc)


async def _enrollment(db_session: AsyncSession) -> OutreachEnrollment:
    company = Company(
        id=_uuid.uuid4(), name="KpiCo", country="US", source="test"
    )
    db_session.add(company)
    await db_session.flush()
    contact = Contact(
        id=_uuid.uuid4(),
        company_id=company.id,
        first_name="Pat",
        last_name="Kpi",
        email="pat@kpi.test",
    )
    seq = OutreachSequence(id=_uuid.uuid4(), name="KPI seq")
    db_session.add_all([contact, seq])
    await db_session.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id
    )
    db_session.add(enr)
    await db_session.flush()
    return enr


def _reply(enr, *, days_ago: int) -> OutreachEvent:
    return OutreachEvent(
        id=_uuid.uuid4(),
        enrollment_id=enr.id,
        step_number=1,
        event_type="replied",
        occurred_at=NOW - timedelta(days=days_ago),
    )


@pytest.mark.asyncio
async def test_replies_this_week_excludes_older_replies(
    client: AsyncClient, db_session: AsyncSession
):
    enr = await _enrollment(db_session)
    db_session.add_all(
        [
            _reply(enr, days_ago=1),
            _reply(enr, days_ago=3),
            # Outside the 7-day window — these were the whole KPI before.
            _reply(enr, days_ago=30),
            _reply(enr, days_ago=200),
        ]
    )
    await db_session.commit()

    data = (await client.get("/api/v1/dashboard/kpis")).json()
    assert data["replies_this_week"] == 2


@pytest.mark.asyncio
async def test_replies_this_week_is_zero_after_a_quiet_week(
    client: AsyncClient, db_session: AsyncSession
):
    """The old count only ever went up; it never dropped on a quiet week."""
    enr = await _enrollment(db_session)
    db_session.add_all([_reply(enr, days_ago=40), _reply(enr, days_ago=90)])
    await db_session.commit()

    data = (await client.get("/api/v1/dashboard/kpis")).json()
    assert data["replies_this_week"] == 0


@pytest.mark.asyncio
async def test_deals_won_this_month_uses_the_close_date(
    client: AsyncClient, db_session: AsyncSession
):
    company = Company(id=_uuid.uuid4(), name="SlowCycleCo", country="US", source="test")
    db_session.add(company)
    await db_session.flush()

    first_of_month = NOW.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    # A long sales cycle: opened months ago, won today. Windowing on
    # ``created_at`` dropped this deal — and its revenue — entirely.
    long_cycle = Deal(
        id=_uuid.uuid4(),
        company_id=company.id,
        title="Long cycle",
        value=80000,
        status="won",
        close_date=max(first_of_month, NOW - timedelta(hours=1)),
    )
    long_cycle.created_at = first_of_month - timedelta(days=120)
    # Opened this month but won before it — belongs to a previous month's number.
    prior = Deal(
        id=_uuid.uuid4(),
        company_id=company.id,
        title="Prior month close",
        value=5000,
        status="won",
        close_date=first_of_month - timedelta(days=2),
    )
    db_session.add_all([long_cycle, prior])
    await db_session.commit()

    data = (await client.get("/api/v1/dashboard/kpis")).json()
    assert data["deals_won_this_month"] == 1
    assert data["deals_won_value"] == pytest.approx(80000.0, abs=0.01)


@pytest.mark.asyncio
async def test_overview_deals_won_this_month_uses_the_close_date(
    client: AsyncClient, db_session: AsyncSession
):
    """``/dashboard/overview`` carried the same window bug."""
    company = Company(id=_uuid.uuid4(), name="OverviewCo", country="US", source="test")
    db_session.add(company)
    await db_session.flush()
    first_of_month = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    won_now = Deal(
        id=_uuid.uuid4(),
        company_id=company.id,
        title="Won now",
        value=12000,
        status="won",
        close_date=max(first_of_month, NOW - timedelta(hours=1)),
    )
    won_now.created_at = first_of_month - timedelta(days=200)
    db_session.add(won_now)
    await db_session.commit()

    data = (await client.get("/api/v1/dashboard/overview")).json()
    assert data["deals_won_this_month"] == 1
    assert data["deals_won_value_this_month"] == pytest.approx(12000.0, abs=0.01)


@pytest.mark.asyncio
async def test_won_deal_without_a_close_date_is_not_counted(
    client: AsyncClient, db_session: AsyncSession
):
    """A legacy row with no decision date can't be attributed to a month."""
    company = Company(id=_uuid.uuid4(), name="LegacyCo", country="US", source="test")
    db_session.add(company)
    await db_session.flush()
    db_session.add(
        Deal(
            id=_uuid.uuid4(),
            company_id=company.id,
            title="Legacy",
            value=999,
            status="won",
            close_date=None,
        )
    )
    await db_session.commit()

    data = (await client.get("/api/v1/dashboard/kpis")).json()
    assert data["deals_won_this_month"] == 0
