"""Tests for API response consistency: contact list pagination metadata,
filters/sorting, and the news action endpoint."""

import pytest
from httpx import AsyncClient

from app.models.company import Company
from app.models.contact import Contact
from app.models.company_news import CompanyNews


@pytest.mark.asyncio
async def test_contacts_list_returns_pagination_metadata(client: AsyncClient, sample_company):
    resp = await client.get("/api/v1/contacts/", params={"page": 1, "page_size": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 1
    assert body["page_size"] == 10
    assert "total" in body and "items" in body


@pytest.mark.asyncio
async def test_contacts_has_email_filter(client: AsyncClient, db_session, sample_company):
    db_session.add(Contact(company_id=sample_company.id, first_name="With", email="w@a.com", source="t"))
    db_session.add(Contact(company_id=sample_company.id, first_name="Without", email=None, source="t"))
    await db_session.commit()

    with_email = await client.get("/api/v1/contacts/", params={"has_email": True})
    assert with_email.status_code == 200
    assert all(c["email"] for c in with_email.json()["items"])

    without = await client.get("/api/v1/contacts/", params={"has_email": False})
    assert all(c["email"] is None for c in without.json()["items"])


@pytest.mark.asyncio
async def test_contacts_sort_by_email(client: AsyncClient, db_session, sample_company):
    db_session.add(Contact(company_id=sample_company.id, first_name="A", email="aaa@a.com", source="t"))
    db_session.add(Contact(company_id=sample_company.id, first_name="Z", email="zzz@a.com", source="t"))
    await db_session.commit()

    resp = await client.get("/api/v1/contacts/", params={
        "company_id": str(sample_company.id), "sort_by": "email", "sort_dir": "asc", "has_email": True,
    })
    emails = [c["email"] for c in resp.json()["items"]]
    assert emails == sorted(emails)


@pytest.mark.asyncio
async def test_news_mark_actioned(client: AsyncClient, db_session, sample_company):
    news = CompanyNews(company_id=sample_company.id, title="Big expansion", is_actionable=True)
    db_session.add(news)
    await db_session.commit()

    resp = await client.patch(f"/api/v1/news/{news.id}/action")
    assert resp.status_code == 200
    body = resp.json()
    assert body["actioned"] is True
    assert body["id"] == str(news.id)
