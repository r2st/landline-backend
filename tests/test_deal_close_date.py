"""``close_date`` is stamped when a deal is decided.

Nothing ever wrote ``Deal.close_date``. It is optional on both the create and
the update schema, and an operator flipping a deal to won has no reason to also
type a date — so in practice every won deal carried ``close_date = NULL``, and
every report that windows deals by their decision date silently reported zero.

These tests pin the write, and then pin the two reader endpoints that were
structurally broken by its absence: client-retention (won deals in period) and
deal metrics (average days to close).
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.deal import Deal


def _aware(dt):
    """SQLite drops tz info on stored TIMESTAMP columns."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _create(client, company_id, **kw):
    payload = {"company_id": str(company_id), "title": "Annual contract"}
    payload.update(kw)
    return await client.post("/api/v1/deals", json=payload)


@pytest.mark.asyncio
async def test_pending_deal_has_no_close_date(
    client: AsyncClient, sample_company: Company
):
    created = (await _create(client, sample_company.id, value=1000)).json()
    assert created["close_date"] is None


@pytest.mark.asyncio
async def test_winning_a_deal_stamps_close_date(
    client: AsyncClient, sample_company: Company
):
    before = datetime.now(timezone.utc) - timedelta(seconds=5)
    did = (await _create(client, sample_company.id, value=1000)).json()["id"]

    won = (await client.patch(f"/api/v1/deals/{did}", json={"status": "won"})).json()

    assert won["close_date"] is not None
    assert _aware(datetime.fromisoformat(won["close_date"])) >= before


@pytest.mark.asyncio
async def test_losing_a_deal_stamps_close_date(
    client: AsyncClient, sample_company: Company
):
    did = (await _create(client, sample_company.id, value=1000)).json()["id"]
    lost = (
        await client.patch(f"/api/v1/deals/{did}", json={"status": "lost"})
    ).json()
    assert lost["close_date"] is not None


@pytest.mark.asyncio
async def test_deal_created_already_won_is_stamped(
    client: AsyncClient, sample_company: Company
):
    created = (
        await _create(client, sample_company.id, value=1000, status="won")
    ).json()
    assert created["close_date"] is not None


@pytest.mark.asyncio
async def test_explicit_close_date_wins(client: AsyncClient, sample_company: Company):
    """Backfilling a historical close must stay possible."""
    backdated = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    created = (
        await _create(
            client, sample_company.id, value=1000, status="won", close_date=backdated
        )
    ).json()
    assert _aware(datetime.fromisoformat(created["close_date"])).date() == (
        datetime.now(timezone.utc) - timedelta(days=200)
    ).date()


@pytest.mark.asyncio
async def test_resaving_a_won_deal_does_not_walk_the_close_date_forward(
    client: AsyncClient, sample_company: Company, db_session: AsyncSession
):
    backdated = datetime.now(timezone.utc) - timedelta(days=90)
    did = (
        await _create(
            client,
            sample_company.id,
            value=1000,
            status="won",
            close_date=backdated.isoformat(),
        )
    ).json()["id"]

    # An unrelated edit, and a redundant re-save of the same status.
    await client.patch(f"/api/v1/deals/{did}", json={"value": 1200})
    after = (
        await client.patch(f"/api/v1/deals/{did}", json={"status": "won"})
    ).json()

    assert _aware(datetime.fromisoformat(after["close_date"])).date() == backdated.date()


@pytest.mark.asyncio
async def test_reopening_a_deal_clears_the_close_date(
    client: AsyncClient, sample_company: Company
):
    """A decision that was undone must not leave a decision date behind."""
    did = (await _create(client, sample_company.id, value=1000, status="won")).json()[
        "id"
    ]
    reopened = (
        await client.patch(f"/api/v1/deals/{did}", json={"status": "pending"})
    ).json()
    assert reopened["close_date"] is None

    # And re-winning it stamps *today*, not the original decision.
    rewon = (await client.patch(f"/api/v1/deals/{did}", json={"status": "won"})).json()
    assert rewon["close_date"] is not None


@pytest.mark.asyncio
async def test_retention_report_sees_a_deal_won_through_the_api(
    client: AsyncClient, db_session: AsyncSession
):
    """The regression this fix exists for.

    Without a stamped ``close_date`` the report's ``_in_window(None, ...)``
    returns False for every deal, so a month of real closes reported $0.
    """
    company = Company(
        id=_uuid.uuid4(),
        name="RetentionCo",
        country="US",
        source="test",
        pipeline_stage="client",
    )
    db_session.add(company)
    await db_session.commit()

    did = (await _create(client, company.id, value=25000)).json()["id"]
    await client.patch(f"/api/v1/deals/{did}", json={"status": "won"})

    data = (await client.get("/api/v1/reports/client-retention")).json()
    assert data["won_deals_in_period"] == 1
    assert data["won_revenue_in_period"] == 25000


@pytest.mark.asyncio
async def test_avg_days_to_close_is_computed_for_api_won_deals(
    client: AsyncClient, sample_company: Company, db_session: AsyncSession
):
    """``avg_days_to_close`` averaged over ``close_date IS NOT NULL`` — an empty
    set, so it reported 0.0 for every pipeline that had ever closed anything."""
    did = (await _create(client, sample_company.id, value=1000)).json()["id"]
    await client.patch(f"/api/v1/deals/{did}", json={"status": "won"})

    # Backdate creation so the cycle is measurable rather than sub-second.
    deal = (
        await db_session.execute(select(Deal).where(Deal.id == _uuid.UUID(did)))
    ).scalar_one()
    deal.created_at = datetime.now(timezone.utc) - timedelta(days=30)
    await db_session.commit()

    metrics = (await client.get("/api/v1/deals/metrics")).json()
    assert metrics["avg_days_to_close"] == pytest.approx(30.0, abs=1.0)
