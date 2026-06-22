"""Tests for the unified company activity feed."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.company_news import CompanyNews
from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.models.lead_score import LeadScore
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence


@pytest.mark.asyncio
async def test_activity_404_for_unknown_company(client: AsyncClient):
    resp = await client.get(f"/api/v1/companies/{_uuid.uuid4()}/activity")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_activity_contact_added_appears(
    client: AsyncClient, sample_company: Company, sample_contact: Contact
):
    resp = await client.get(f"/api/v1/companies/{sample_company.id}/activity")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] >= 1
    types = {i["type"] for i in data["items"]}
    assert "contact_added" in types


@pytest.mark.asyncio
async def test_activity_aggregates_all_sources(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)

    # Email send with full engagement.
    send = EmailSend(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        to_email=sample_contact.email,
        subject="Intro",
        status="replied",
        sent_at=now - timedelta(days=5),
        opened_at=now - timedelta(days=4),
        clicked_at=now - timedelta(days=4),
        replied_at=now - timedelta(days=3),
    )
    db_session.add(send)

    # Lead score.
    db_session.add(LeadScore(
        id=_uuid.uuid4(),
        company_id=sample_company.id,
        score_type="rule_based",
        fit_score=82,
        scored_at=now - timedelta(days=2),
    ))

    # News.
    db_session.add(CompanyNews(
        id=_uuid.uuid4(),
        company_id=sample_company.id,
        title="Acme opens new plant",
        summary="Expansion in Ohio",
        category="expansion",
        published_at=now - timedelta(days=1),
    ))

    # Interaction / note.
    db_session.add(Interaction(
        id=_uuid.uuid4(),
        company_id=sample_company.id,
        contact_id=sample_contact.id,
        interaction_type="call",
        subject="Discovery call",
        notes="Great fit",
        occurred_at=now - timedelta(hours=12),
    ))

    # Outreach sequence event.
    seq = OutreachSequence(id=_uuid.uuid4(), name="Seq A")
    db_session.add(seq)
    await db_session.flush()
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
    )
    db_session.add(enrollment)
    await db_session.flush()
    db_session.add(OutreachEvent(
        id=_uuid.uuid4(),
        enrollment_id=enrollment.id,
        step_number=1,
        event_type="sent",
        occurred_at=now - timedelta(days=6),
    ))

    await db_session.commit()

    resp = await client.get(f"/api/v1/companies/{sample_company.id}/activity")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    types = {i["type"] for i in data["items"]}
    for expected in (
        "email_sent", "email_opened", "email_clicked", "reply",
        "score_change", "news", "note", "outreach_event", "contact_added",
    ):
        assert expected in types, f"missing {expected}: {types}"

    # Newest-first ordering.
    timestamps = [i["timestamp"] for i in data["items"]]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_activity_filter_by_type(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    db_session.add(LeadScore(
        id=_uuid.uuid4(),
        company_id=sample_company.id,
        score_type="ai",
        fit_score=70,
    ))
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/companies/{sample_company.id}/activity",
        params={"activity_type": "score_change"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert all(i["type"] == "score_change" for i in data["items"])


@pytest.mark.asyncio
async def test_activity_invalid_filter_rejected(
    client: AsyncClient, sample_company: Company
):
    resp = await client.get(
        f"/api/v1/companies/{sample_company.id}/activity",
        params={"activity_type": "bogus"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_activity_pagination(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    for i in range(5):
        db_session.add(LeadScore(
            id=_uuid.uuid4(),
            company_id=sample_company.id,
            score_type="rule_based",
            fit_score=50 + i,
        ))
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/companies/{sample_company.id}/activity",
        params={"activity_type": "score_change", "page": 1, "page_size": 2},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 5
    assert len(data["items"]) == 2
    assert data["page"] == 1
    assert data["page_size"] == 2
