"""Tests for warm-lead phone enrichment.

Covers the two halves of the feature:

* The *warm-signal* query — which contacts count as warm (3+ distinct opens, or
  any click/reply, across both the one-off and the sequence send paths) and
  which are excluded (no phone gap, already attempted, suppressed).
* The *enrichment* step — Apollo people-match parsing, the company-line
  fallback, and the idempotency stamp that stops a second charge.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.enrichment import ApolloEnrichmentService
from app.services.phone_enrichment import (
    OPEN_THRESHOLD,
    enrich_contact_phone,
    warm_contacts_needing_phone,
)


def _now():
    return datetime.now(timezone.utc)


async def _make_contact(db: AsyncSession, company: Company, **overrides) -> Contact:
    contact = Contact(
        id=uuid.uuid4(),
        company_id=company.id,
        first_name="Dana",
        last_name="Reyes",
        title="Plant Manager",
        email=f"dana-{uuid.uuid4().hex[:6]}@acmestamp.com",
        source="test",
    )
    for k, v in overrides.items():
        setattr(contact, k, v)
    db.add(contact)
    await db.flush()
    return contact


async def _opened_send(db: AsyncSession, contact: Contact, **overrides) -> EmailSend:
    send = EmailSend(
        id=uuid.uuid4(),
        contact_id=contact.id,
        to_email=contact.email,
        subject="Following up",
        status="opened",
        sent_at=_now(),
        opened_at=_now(),
    )
    for k, v in overrides.items():
        setattr(send, k, v)
    db.add(send)
    await db.flush()
    return send


async def _enrollment_with_opens(
    db: AsyncSession, contact: Contact, n_opens: int, event_type: str = "opened"
) -> OutreachEnrollment:
    seq = OutreachSequence(id=uuid.uuid4(), name="Cold seq")
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active"
    )
    db.add(enr)
    await db.flush()
    for step in range(1, n_opens + 1):
        db.add(
            OutreachEvent(
                id=uuid.uuid4(),
                enrollment_id=enr.id,
                step_number=step,
                event_type=event_type,
                occurred_at=_now(),
            )
        )
    await db.flush()
    return enr


# --- warm-signal query -------------------------------------------------------


@pytest.mark.asyncio
async def test_three_opens_via_sends_is_warm(db_session, sample_company):
    contact = await _make_contact(db_session, sample_company)
    for _ in range(OPEN_THRESHOLD):
        await _opened_send(db_session, contact)

    warm = await warm_contacts_needing_phone(db_session)
    assert contact.id in {c.id for c in warm}


@pytest.mark.asyncio
async def test_two_opens_is_not_warm(db_session, sample_company):
    contact = await _make_contact(db_session, sample_company)
    for _ in range(OPEN_THRESHOLD - 1):
        await _opened_send(db_session, contact)

    warm = await warm_contacts_needing_phone(db_session)
    assert contact.id not in {c.id for c in warm}


@pytest.mark.asyncio
async def test_single_reply_is_warm(db_session, sample_company):
    contact = await _make_contact(db_session, sample_company)
    db_session.add(
        EmailReply(
            id=uuid.uuid4(),
            contact_id=contact.id,
            from_email=contact.email,
            received_at=_now(),
        )
    )
    await db_session.flush()

    warm = await warm_contacts_needing_phone(db_session)
    assert contact.id in {c.id for c in warm}


@pytest.mark.asyncio
async def test_single_click_is_warm(db_session, sample_company):
    contact = await _make_contact(db_session, sample_company)
    await _opened_send(db_session, contact, clicked_at=_now())

    warm = await warm_contacts_needing_phone(db_session)
    assert contact.id in {c.id for c in warm}


@pytest.mark.asyncio
async def test_three_sequence_opens_is_warm(db_session, sample_company):
    contact = await _make_contact(db_session, sample_company)
    await _enrollment_with_opens(db_session, contact, OPEN_THRESHOLD)

    warm = await warm_contacts_needing_phone(db_session)
    assert contact.id in {c.id for c in warm}


@pytest.mark.asyncio
async def test_contact_with_phone_is_excluded(db_session, sample_company):
    contact = await _make_contact(db_session, sample_company, phone="216-555-0000")
    for _ in range(OPEN_THRESHOLD):
        await _opened_send(db_session, contact)

    warm = await warm_contacts_needing_phone(db_session)
    assert contact.id not in {c.id for c in warm}


@pytest.mark.asyncio
async def test_already_attempted_contact_is_excluded(db_session, sample_company):
    contact = await _make_contact(
        db_session, sample_company, phone_enriched_at=_now()
    )
    for _ in range(OPEN_THRESHOLD):
        await _opened_send(db_session, contact)

    warm = await warm_contacts_needing_phone(db_session)
    assert contact.id not in {c.id for c in warm}


@pytest.mark.asyncio
async def test_suppressed_contact_is_excluded(db_session, sample_company):
    contact = await _make_contact(db_session, sample_company, suppressed=True)
    db_session.add(
        EmailReply(
            id=uuid.uuid4(),
            contact_id=contact.id,
            from_email=contact.email,
            received_at=_now(),
        )
    )
    await db_session.flush()

    warm = await warm_contacts_needing_phone(db_session)
    assert contact.id not in {c.id for c in warm}


# --- Apollo phone parsing ----------------------------------------------------


def test_extract_phones_prefers_mobile_and_direct():
    person = {
        "phone_numbers": [
            {"type": "work_hq", "sanitized_number": "+16165550100"},
            {"type": "mobile", "sanitized_number": "+16165559999"},
        ],
    }
    direct, mobile = ApolloEnrichmentService._extract_phones(person)
    assert direct == "+16165550100"
    assert mobile == "+16165559999"


def test_extract_phones_falls_back_to_org_line():
    person = {"organization": {"phone": "+16165551234"}}
    direct, mobile = ApolloEnrichmentService._extract_phones(person)
    assert direct == "+16165551234"
    assert mobile is None


@pytest.mark.asyncio
async def test_match_person_phone_skips_without_key():
    service = ApolloEnrichmentService()
    service.api_key = ""
    result = await service.match_person_phone(
        None, first_name="A", last_name="B", email=None, domain="x.com"
    )
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_match_person_phone_skips_without_identity():
    service = ApolloEnrichmentService()
    service.api_key = "key"
    result = await service.match_person_phone(
        None, first_name=None, last_name=None, email=None, domain=None
    )
    assert result["status"] == "skipped"


# --- enrichment step ---------------------------------------------------------


class _FakeService:
    """Stand-in Apollo service that returns a scripted match result."""

    def __init__(self, result):
        self.api_key = "key"
        self._result = result

    async def match_person_phone(self, *args, **kwargs):
        return self._result


@pytest.mark.asyncio
async def test_enrich_sets_phone_and_source_from_apollo(db_session, sample_company):
    contact = await _make_contact(db_session, sample_company)
    fake = _FakeService(
        {"status": "found", "phone": "+16165550100", "mobile": "+16165559999"}
    )

    res = await enrich_contact_phone(db_session, contact, service=fake, notify=False)

    assert res["source"] == "apollo"
    assert contact.phone == "+16165550100"
    assert contact.mobile == "+16165559999"
    assert contact.phone_source == "apollo"
    assert contact.phone_enriched_at is not None


@pytest.mark.asyncio
async def test_enrich_falls_back_to_company_line(db_session, sample_company):
    # sample_company.phone is "216-555-0100"
    contact = await _make_contact(db_session, sample_company)
    fake = _FakeService({"status": "no_phone", "phone": None, "mobile": None})

    res = await enrich_contact_phone(db_session, contact, service=fake, notify=False)

    assert res["source"] == "company"
    assert contact.phone == sample_company.phone
    assert contact.phone_source == "company"


@pytest.mark.asyncio
async def test_enrich_stamps_attempt_even_when_nothing_found(db_session):
    # Company with no phone → no fallback available.
    company = Company(
        id=uuid.uuid4(), name="Nolines Inc", domain="nolines.example",
        country="US", source="test", phone=None,
    )
    db_session.add(company)
    await db_session.flush()
    contact = await _make_contact(db_session, company)
    fake = _FakeService({"status": "not_found", "phone": None, "mobile": None})

    res = await enrich_contact_phone(db_session, contact, service=fake, notify=False)

    assert res["source"] is None
    assert contact.phone is None
    # Idempotency: the attempt is recorded so the sweep won't retry.
    assert contact.phone_enriched_at is not None


@pytest.mark.asyncio
async def test_enriched_contact_not_returned_next_sweep(db_session, sample_company):
    contact = await _make_contact(db_session, sample_company)
    for _ in range(OPEN_THRESHOLD):
        await _opened_send(db_session, contact)
    fake = _FakeService(
        {"status": "found", "phone": "+16165550100", "mobile": None}
    )

    await enrich_contact_phone(db_session, contact, service=fake, notify=False)

    warm = await warm_contacts_needing_phone(db_session)
    assert contact.id not in {c.id for c in warm}
