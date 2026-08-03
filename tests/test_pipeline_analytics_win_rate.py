"""``/reports/pipeline-analytics`` win/loss rates exclude undecided deals.

The denominator used to be ``won + lost + pending`` under the name
``total_decided``. A pending deal is not a loss — it hasn't been anything yet —
so every open opportunity dragged the reported win rate down, and the two rates
never summed to 100%.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.deal import Deal

NOW = datetime.now(timezone.utc)


async def _company(db_session: AsyncSession, name: str) -> Company:
    c = Company(
        id=_uuid.uuid4(), name=name, country="US", source="test", stage="prospect"
    )
    db_session.add(c)
    await db_session.flush()
    return c


@pytest.mark.asyncio
async def test_win_rate_ignores_pending_deals(
    client: AsyncClient, db_session: AsyncSession
):
    c = await _company(db_session, "WinCo")
    # 3 won, 1 lost → 75% win / 25% loss, regardless of how much is still open.
    for i in range(3):
        db_session.add(
            Deal(
                id=_uuid.uuid4(),
                company_id=c.id,
                title=f"W{i}",
                value=1000,
                status="won",
                close_date=NOW - timedelta(days=1),
            )
        )
    db_session.add(
        Deal(
            id=_uuid.uuid4(),
            company_id=c.id,
            title="L",
            value=1000,
            status="lost",
            close_date=NOW - timedelta(days=1),
        )
    )
    # A fat open pipeline: under the old denominator this alone dropped the
    # reported win rate from 75% to 23%.
    for i in range(9):
        db_session.add(
            Deal(
                id=_uuid.uuid4(),
                company_id=c.id,
                title=f"P{i}",
                value=5000,
                status="pending",
            )
        )
    await db_session.commit()

    data = (await client.get("/api/v1/reports/pipeline-analytics")).json()
    assert data["win_rate"] == pytest.approx(75.0, abs=0.1)
    assert data["loss_rate"] == pytest.approx(25.0, abs=0.1)
    # The rates describe the same population, so they account for all of it.
    assert data["win_rate"] + data["loss_rate"] == pytest.approx(100.0, abs=0.1)
    # Open value is still reported — it just isn't a loss.
    assert data["total_pipeline_value"] == pytest.approx(45000.0, abs=0.01)


@pytest.mark.asyncio
async def test_creating_an_open_deal_does_not_move_the_win_rate(
    client: AsyncClient, db_session: AsyncSession
):
    """Filling the pipeline used to *lower* the win rate."""
    c = await _company(db_session, "StableCo")
    db_session.add(
        Deal(
            id=_uuid.uuid4(),
            company_id=c.id,
            title="W",
            value=1000,
            status="won",
            close_date=NOW - timedelta(days=1),
        )
    )
    await db_session.commit()

    before = (await client.get("/api/v1/reports/pipeline-analytics")).json()["win_rate"]

    db_session.add(
        Deal(id=_uuid.uuid4(), company_id=c.id, title="P", value=1, status="pending")
    )
    await db_session.commit()

    after = (await client.get("/api/v1/reports/pipeline-analytics")).json()["win_rate"]
    assert before == after == pytest.approx(100.0, abs=0.1)


@pytest.mark.asyncio
async def test_win_rate_is_zero_when_nothing_is_decided(
    client: AsyncClient, db_session: AsyncSession
):
    c = await _company(db_session, "OpenCo")
    db_session.add(
        Deal(id=_uuid.uuid4(), company_id=c.id, title="P", value=1000, status="pending")
    )
    await db_session.commit()

    data = (await client.get("/api/v1/reports/pipeline-analytics")).json()
    assert data["win_rate"] == 0.0
    assert data["loss_rate"] == 0.0


@pytest.mark.asyncio
async def test_pipeline_analytics_agrees_with_deal_metrics(
    client: AsyncClient, db_session: AsyncSession
):
    """Two pages quoting the same statistic must quote the same number."""
    c = await _company(db_session, "AgreeCo")
    for status, n in (("won", 2), ("lost", 3), ("pending", 4)):
        for i in range(n):
            db_session.add(
                Deal(
                    id=_uuid.uuid4(),
                    company_id=c.id,
                    title=f"{status}{i}",
                    value=1000,
                    status=status,
                    close_date=NOW - timedelta(days=1) if status != "pending" else None,
                )
            )
    await db_session.commit()

    analytics = (await client.get("/api/v1/reports/pipeline-analytics")).json()
    metrics = (await client.get("/api/v1/deals/metrics")).json()
    assert analytics["win_rate"] == pytest.approx(metrics["win_rate"], abs=0.1)
    assert analytics["win_rate"] == pytest.approx(40.0, abs=0.1)
