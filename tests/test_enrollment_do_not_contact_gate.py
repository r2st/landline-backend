"""Every enrollment writer asks the same do-not-contact question.

``check_send_allowed`` is the compliance floor and it holds — nothing here was
ever mailed. What it cannot stop is the *enrollment*, and six paths created
those with six different ideas of who is mailable:

* ``autonomous_prospector`` asked all three questions (the ``suppressed`` flag,
  the legacy ``unsubscribed`` flag, and the org do-not-contact LIST);
* ``signal_enrollment`` and ``POST /sequences/{id}/enroll`` read the two flags
  and not the list;
* ``POST /outreach/enrollments`` and the public API's ``POST
  /sequences/enrollments`` asked nothing beyond "is there an address";
* ``social_listening_agent.auto_enroll_lead`` asked nothing at all, while its
  own docstring claimed it mirrored the prospector.

**The list is the clause that gets dropped, and it is the one that cannot be
reconstructed from the contact row in front of you.** ``suppression.add_entry``
deliberately does not touch ``Contact.suppressed`` — an address outlives every
contact row that carries it — so three ordinary situations produce a contact
whose flags are clean and whose address must never be mailed: a bulk compliance
CSV upload, a bounce recorded against a re-scraped duplicate row, and an erasure
whose subject the scraper later rediscovered from the same public source that
found them the first time. That last one is precisely what
``gdpr.erase_subject`` writes its list entry *for*.

What it cost, since nothing leaked: a live enrollment for someone who opted out,
dispatched and paid for before the gate refused it, counted as outreach on a
campaign that can never send it — and in the social-listening path, a company
advanced to ``pipeline_stage='outreach'`` on the strength of it, which is a
claim about the company that no downstream gate corrects.

``suppression.blocking_reason`` is the shared test, and these are its callers.
"""

import uuid as _uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence, SequenceStep
from app.models.suppression import SuppressionEntry
from app.services import suppression

pytestmark = pytest.mark.asyncio


async def _sequence(db: AsyncSession, *, industry: str | None = None) -> OutreachSequence:
    seq = OutreachSequence(
        id=_uuid.uuid4(), name="Cold Open", is_active=True, status="active",
        industry=industry,
    )
    db.add(seq)
    await db.flush()
    db.add(
        SequenceStep(
            id=_uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
            subject_template="{{first_name}}?", body_template="<p>Hi</p>",
        )
    )
    await db.flush()
    return seq


async def _list_the_address(db: AsyncSession, email: str, *, reason: str = "manual"):
    """Put an address on the org list WITHOUT touching the contact's flags.

    This is what ``add_entry`` does, and the asymmetry is the whole point of
    these tests: the contact row goes on looking mailable.
    """
    db.add(SuppressionEntry(id=_uuid.uuid4(), email=email.lower(), reason=reason, source="manual"))
    await db.flush()
    await db.commit()


# --- The shared predicate ----------------------------------------------------

async def test_blocking_reason_reads_all_three_sources(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    assert await suppression.blocking_reason(db_session, sample_contact) is None

    sample_contact.unsubscribed = True
    await db_session.flush()
    assert await suppression.blocking_reason(db_session, sample_contact) == suppression.UNSUBSCRIBED

    sample_contact.unsubscribed = False
    sample_contact.suppressed = True
    await db_session.flush()
    assert await suppression.blocking_reason(db_session, sample_contact) == suppression.SUPPRESSED

    sample_contact.suppressed = False
    await db_session.flush()
    await _list_the_address(db_session, sample_contact.email)
    assert await suppression.blocking_reason(db_session, sample_contact) == suppression.ON_LIST


async def test_the_list_clause_is_the_one_the_flags_cannot_answer(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """The regression in one assertion: clean flags, unmailable address.

    A compliance upload writes the entry and nothing else. A checker reading
    only the contact row sees a perfectly ordinary prospect.
    """
    await _list_the_address(db_session, sample_contact.email, reason="unsubscribe")

    assert sample_contact.suppressed is False
    assert sample_contact.unsubscribed is False
    assert suppression.blocking_flag(sample_contact) is None, (
        "the contact row is clean — which is exactly why the list has to be asked"
    )
    assert await suppression.is_mailable(db_session, sample_contact) is False


async def test_suppressed_addresses_batches_and_normalizes(
    db_session: AsyncSession, sample_company: Company,
):
    await _list_the_address(db_session, "listed@example.com")

    found = await suppression.suppressed_addresses(
        db_session, ["  LISTED@Example.com ", "clean@example.com", None, "not-an-email"]
    )
    assert found == {"listed@example.com"}
    assert await suppression.suppressed_addresses(db_session, []) == set()


# --- The autonomous enrollers ------------------------------------------------

async def test_social_listening_agent_will_not_enroll_an_opted_out_contact(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """The worst of the six: it checked only that the address was non-empty."""
    from app.services.social_listening_agent import SocialListeningService

    await _sequence(db_session, industry=None)
    sample_contact.unsubscribed = True
    sample_company.pipeline_stage = "researching"
    await db_session.commit()

    created = await SocialListeningService().auto_enroll_lead(
        db_session, sample_contact, object()
    )

    assert created is False
    rows = (
        await db_session.execute(
            OutreachEnrollment.__table__.select().where(
                OutreachEnrollment.contact_id == sample_contact.id
            )
        )
    ).all()
    assert rows == []
    assert sample_company.pipeline_stage == "researching", (
        "a refused enrollment must not advance the company's pipeline stage"
    )


async def test_social_listening_agent_honours_the_list_alone(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """Flags clean, address listed — the case the flags cannot see."""
    from app.services.social_listening_agent import SocialListeningService

    await _sequence(db_session, industry=None)
    await _list_the_address(db_session, sample_contact.email, reason="bounce")

    created = await SocialListeningService().auto_enroll_lead(
        db_session, sample_contact, object()
    )
    assert created is False


async def test_signal_enrollment_skips_a_listed_address(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """``_emailable`` read the two flags and stopped."""
    from app.services import signal_enrollment

    await _list_the_address(db_session, sample_contact.email, reason="complaint")

    assert await signal_enrollment._emailable(db_session, sample_contact) is False
    assert await signal_enrollment._emailable_subset(db_session, [sample_contact]) == []


async def test_signal_enrollment_subset_keeps_the_mailable_ones(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """The batch helper must not throw the good rows out with the bad."""
    from app.services import signal_enrollment

    listed = Contact(
        id=_uuid.uuid4(), company_id=sample_company.id,
        first_name="Dana", last_name="Reed", email="listed@example.com",
    )
    flagged = Contact(
        id=_uuid.uuid4(), company_id=sample_company.id,
        first_name="Sam", last_name="Vo", email="flagged@example.com",
        suppressed=True,
    )
    db_session.add_all([listed, flagged])
    await db_session.flush()
    await _list_the_address(db_session, "listed@example.com")

    kept = await signal_enrollment._emailable_subset(
        db_session, [sample_contact, listed, flagged]
    )
    assert [c.id for c in kept] == [sample_contact.id]


# --- The enrollment endpoints ------------------------------------------------

async def test_bulk_enroll_reports_the_list_as_the_skip_reason(
    client, auth_headers, admin_user, db_session: AsyncSession,
    sample_company: Company, sample_contact: Contact,
):
    seq = await _sequence(db_session)
    await _list_the_address(db_session, sample_contact.email)

    resp = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(sample_contact.id)]},
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enrolled"] == []
    assert body["skipped"] == [
        {"contact_id": str(sample_contact.id), "reason": suppression.ON_LIST}
    ]


async def test_bulk_enroll_still_enrolls_a_mailable_contact(
    client, auth_headers, admin_user, db_session: AsyncSession,
    sample_company: Company, sample_contact: Contact,
):
    """The control: the batch list query must not refuse everyone."""
    seq = await _sequence(db_session)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(sample_contact.id)]},
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enrolled"] == [str(sample_contact.id)]


async def test_single_enroll_endpoint_refuses_a_suppressed_contact(
    client, auth_headers, admin_user, db_session: AsyncSession,
    sample_company: Company, sample_contact: Contact,
):
    """It checked for an address and nothing else — then dispatched the send."""
    seq = await _sequence(db_session)
    sample_contact.suppressed = True
    await db_session.commit()

    resp = await client.post(
        "/api/v1/outreach/enrollments",
        json={"contact_id": str(sample_contact.id), "sequence_id": str(seq.id)},
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == suppression.SUPPRESSED


async def test_single_enroll_endpoint_refuses_a_listed_address(
    client, auth_headers, admin_user, db_session: AsyncSession,
    sample_company: Company, sample_contact: Contact,
):
    seq = await _sequence(db_session)
    await _list_the_address(db_session, sample_contact.email, reason="erasure")

    resp = await client.post(
        "/api/v1/outreach/enrollments",
        json={"contact_id": str(sample_contact.id), "sequence_id": str(seq.id)},
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == suppression.ON_LIST
