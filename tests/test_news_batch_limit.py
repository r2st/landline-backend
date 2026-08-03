"""``POST /news/check-all`` bounds its batch, and reports what it actually did.

Two faults in one endpoint, both of which read as harmless from the call site:

* **``limit`` was unbounded** (``limit: int = 10``, no ``Query`` constraints).
  Each company in the batch costs an outbound news fetch and an LLM call, run
  one after another inside the request, so ``?limit=100000`` is one
  authenticated caller holding a worker and a database connection for as long
  as the company table is long and spending the org's model quota to do it.
  ``?limit=0`` returned an empty batch, and ``?limit=-1`` reached ``LIMIT -1``,
  which Postgres refuses outright.
* **``companies_checked`` counted the wrong list.** ``check_all_companies``
  drops the companies that had no news, so on an ordinary day where nothing is
  in the press it returned ``[]`` — and the endpoint reported
  ``companies_checked: 0``, which is indistinguishable from a sweep that never
  ran, on the response an operator reads to confirm that it did.
"""

import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.services import news_monitor as nm


def _company(name: str) -> Company:
    return Company(id=uuid.uuid4(), name=name, source="test", stage="new")


@pytest.mark.asyncio
async def test_check_all_rejects_out_of_range_limits(client: AsyncClient):
    """Bounds are enforced by validation, so nothing reaches the fan-out."""
    for bad in ("0", "-1", "100000"):
        resp = await client.post("/api/v1/news/check-all", params={"limit": bad})
        assert resp.status_code == 422, f"limit={bad} -> {resp.status_code}"


@pytest.mark.asyncio
async def test_check_all_caps_the_number_of_companies_examined(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """``limit`` is a ceiling on the per-company work, not just on the response."""
    for i in range(5):
        db_session.add(_company(f"Batch Co {i}"))
    await db_session.commit()

    calls: list[str] = []

    async def _fake_check(self, db, company):
        calls.append(company.name)
        return []

    monkeypatch.setattr(nm.NewsMonitor, "check_company_news", _fake_check)

    resp = await client.post("/api/v1/news/check-all", params={"limit": 2})
    assert resp.status_code == 200, resp.text
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_check_all_reports_companies_checked_not_companies_with_news(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """A batch that found nothing still reports the companies it looked at.

    ``companies_checked`` is the batch size actually read — not ``limit``, which
    only bounds it and would overstate the work on an org with fewer companies
    than the batch, and not ``len(results)``, which counts only the ones with
    news.
    """
    for i in range(3):
        db_session.add(_company(f"Quiet Co {i}"))
    await db_session.commit()

    async def _no_news(self, db, company):
        return []

    monkeypatch.setattr(nm.NewsMonitor, "check_company_news", _no_news)

    resp = await client.post("/api/v1/news/check-all", params={"limit": 10})
    body = resp.json()
    assert body["companies_checked"] == 3       # what we looked at
    assert body["companies_with_news"] == 0     # what we found
    assert body["total_articles"] == 0
    assert body["results"] == []


@pytest.mark.asyncio
async def test_check_all_counts_articles_from_the_companies_that_had_news(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    db_session.add(_company("Loud Co"))
    db_session.add(_company("Quiet Co"))
    await db_session.commit()

    async def _one_for_loud(self, db, company):
        return [{"headline": "raised a round"}] if company.name == "Loud Co" else []

    monkeypatch.setattr(nm.NewsMonitor, "check_company_news", _one_for_loud)

    body = (await client.post("/api/v1/news/check-all", params={"limit": 10})).json()
    assert body["companies_checked"] == 2
    assert body["companies_with_news"] == 1
    assert body["total_articles"] == 1
    assert body["results"][0]["company"] == "Loud Co"


@pytest.mark.asyncio
async def test_check_all_companies_keeps_its_list_only_contract(
    db_session: AsyncSession, monkeypatch
):
    """The list-returning method still returns a bare list.

    ``app.tasks.news`` calls it and already labels the length
    ``companies_with_news``, which is correct for what it gets; the counted
    variant exists so the router does not have to guess.
    """
    db_session.add(_company("Contract Co"))
    await db_session.commit()

    monkeypatch.setattr(
        nm.NewsMonitor, "check_company_news", AsyncMock(return_value=[])
    )
    monitor = nm.NewsMonitor()

    assert await monitor.check_all_companies(db_session, limit=5) == []
    assert await monitor.check_all_companies_counted(db_session, limit=5) == (1, [])
