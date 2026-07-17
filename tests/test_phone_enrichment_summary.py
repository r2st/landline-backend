"""Tests for the Dialtone phone-enrichment summary endpoint.

``GET /dialtone/phone-enrichment/summary`` powers the Console's enrichment card:
how many warm contacts got an auto-found number, the hit rate against attempts,
the Apollo-direct-dial vs company-line split, and the most recent finds. Only
sweep-sourced numbers ('apollo'/'company') count — a hand-typed or scanned
number is not a Dialtone action.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact


def _now():
    return datetime.now(timezone.utc)


async def _make_contact(
    db: AsyncSession, company_id, *, source, enriched_at, phone="216-555-0999",
    first="Jane", last="Doe",
):
    contact = Contact(
        id=uuid.uuid4(),
        company_id=company_id,
        first_name=first,
        last_name=last,
        email=f"{uuid.uuid4().hex[:8]}@acmestamp.com",
        phone=phone,
        phone_source=source,
        phone_enriched_at=enriched_at,
        source="test",
    )
    db.add(contact)
    await db.flush()
    return contact


@pytest.mark.asyncio
async def test_summary_counts_hit_rate_and_source_split(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    cid = sample_company.id
    # 2 apollo hits, 1 company-line hit.
    await _make_contact(db_session, cid, source="apollo", enriched_at=_now())
    await _make_contact(db_session, cid, source="apollo", enriched_at=_now() - timedelta(hours=1))
    await _make_contact(db_session, cid, source="company", enriched_at=_now() - timedelta(hours=2))
    # 1 attempt that missed (stamped but no source, no phone).
    await _make_contact(db_session, cid, source=None, enriched_at=_now() - timedelta(hours=3), phone=None)

    resp = await client.get("/api/v1/dialtone/phone-enrichment/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["enriched"] == 3
    assert body["attempts"] == 4
    assert body["apollo"] == 2
    assert body["company"] == 1
    # 3 of 4 attempts found a number.
    assert body["hit_rate"] == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_summary_ignores_manual_and_card_sources(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    cid = sample_company.id
    # A scanned card and a hand-typed number are NOT enrichment sweep results,
    # and have no phone_enriched_at — they must not count as attempts or hits.
    manual = Contact(
        id=uuid.uuid4(), company_id=cid, first_name="Hand", last_name="Typed",
        email="hand@acmestamp.com", phone="216-555-0001", phone_source="manual",
        source="test",
    )
    card = Contact(
        id=uuid.uuid4(), company_id=cid, first_name="Card", last_name="Scan",
        email="card@acmestamp.com", phone="216-555-0002", phone_source="card",
        source="test",
    )
    db_session.add_all([manual, card])
    await db_session.flush()

    resp = await client.get("/api/v1/dialtone/phone-enrichment/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enriched"] == 0
    assert body["attempts"] == 0
    assert body["hit_rate"] == 0.0


@pytest.mark.asyncio
async def test_summary_recent_is_newest_first_with_labels(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    cid = sample_company.id
    await _make_contact(
        db_session, cid, source="company", enriched_at=_now() - timedelta(days=1),
        first="Older", last="Lead", phone="216-555-1000",
    )
    await _make_contact(
        db_session, cid, source="apollo", enriched_at=_now(),
        first="Newer", last="Lead", phone="216-555-2000",
    )

    resp = await client.get("/api/v1/dialtone/phone-enrichment/summary")
    assert resp.status_code == 200, resp.text
    recent = resp.json()["recent"]
    assert len(recent) == 2
    # Newest-first ordering.
    assert recent[0]["contact_name"] == "Newer Lead"
    assert recent[0]["source"] == "apollo"
    assert recent[0]["source_label"] == "direct dial"
    assert recent[0]["company_name"] == sample_company.name
    assert recent[1]["source_label"] == "company line"


@pytest.mark.asyncio
async def test_summary_respects_recent_limit_zero(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    await _make_contact(db_session, sample_company.id, source="apollo", enriched_at=_now())
    resp = await client.get("/api/v1/dialtone/phone-enrichment/summary?recent_limit=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enriched"] == 1
    assert body["recent"] == []


@pytest.mark.asyncio
async def test_summary_empty_when_nothing_enriched(client: AsyncClient):
    resp = await client.get("/api/v1/dialtone/phone-enrichment/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "enriched": 0,
        "attempts": 0,
        "hit_rate": 0.0,
        "apollo": 0,
        "company": 0,
        "recent": [],
    }
