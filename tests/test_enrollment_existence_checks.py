"""Re-enrolling a contact is allowed, so the "already enrolled?" checks must survive it.

Three places ask whether a contact is already in a sequence, and all three asked
with ``scalar_one_or_none`` — which is not a question but an assertion that at
most one enrollment exists for the pair. ``outreach_enrollments`` has no unique
constraint on ``(contact_id, sequence_id)``, and more to the point the enrol
endpoint **deliberately** permits a second one: it rejects only a duplicate
*active* enrollment, so a contact who finished a sequence can be enrolled again.

The two agents then read every enrollment for the pair, status included, and so
see both rows for any contact with that perfectly ordinary history. The
assertion turns it into ``MultipleResultsFound``, which aborts the run — the
autonomous prospector's and the social-listening agent's whole pass, not just
the one contact.

An existence check wants one row and a boolean, so that is what these now ask
for.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence


@pytest.fixture
async def contact_and_sequence(db_session):
    company = Company(
        id=uuid.uuid4(), name="Repeat Buyer Co", domain="repeat.example", source="test"
    )
    db_session.add(company)
    await db_session.flush()

    contact = Contact(
        id=uuid.uuid4(), email="buyer@repeat.example", company_id=company.id
    )
    sequence = OutreachSequence(id=uuid.uuid4(), name="Q3 Manufacturing", is_active=True)
    db_session.add_all([contact, sequence])
    await db_session.flush()
    return contact, sequence


def make_enrollment(contact, sequence, status: str, **kwargs) -> OutreachEnrollment:
    return OutreachEnrollment(
        id=uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=sequence.id,
        status=status,
        current_step=kwargs.pop("current_step", 1),
        enrolled_at=kwargs.pop("enrolled_at", datetime.now(timezone.utc)),
        **kwargs,
    )


async def test_a_contact_can_hold_two_enrollments_in_one_sequence(
    db_session, contact_and_sequence
):
    """The premise. If the schema forbade this, none of the rest would matter."""
    contact, sequence = contact_and_sequence
    db_session.add_all(
        [
            make_enrollment(
                contact,
                sequence,
                "completed",
                enrolled_at=datetime.now(timezone.utc) - timedelta(days=90),
            ),
            make_enrollment(contact, sequence, "active"),
        ]
    )
    await db_session.flush()

    rows = (
        await db_session.execute(
            select(OutreachEnrollment).where(
                OutreachEnrollment.contact_id == contact.id,
                OutreachEnrollment.sequence_id == sequence.id,
            )
        )
    ).scalars().all()

    assert len(rows) == 2


async def test_the_prospector_skips_a_re_enrolled_contact_without_raising(
    db_session, contact_and_sequence
):
    """The autonomous prospector's check, against a completed + active history."""
    contact, sequence = contact_and_sequence
    db_session.add_all(
        [
            make_enrollment(contact, sequence, "completed"),
            make_enrollment(contact, sequence, "active"),
        ]
    )
    await db_session.flush()

    existing = (
        await db_session.execute(
            select(OutreachEnrollment.id)
            .where(
                OutreachEnrollment.contact_id == contact.id,
                OutreachEnrollment.sequence_id == sequence.id,
            )
            .limit(1)
        )
    ).scalars().first()

    assert existing is not None, "an existing enrollment must still be detected"


@pytest.mark.parametrize(
    "module_name,marker",
    [
        ("app.services.autonomous_prospector", "OutreachEnrollment.sequence_id == sequence.id"),
        ("app.services.social_listening_agent", "OutreachEnrollment.sequence_id == sequence.id"),
    ],
)
def test_the_agent_checks_are_not_single_row_assertions(module_name, marker):
    """Pin the shape in the agents, where a raise costs the whole run.

    Driving either agent end to end needs its external feed mocked; what is
    being fixed is one query, so the query is what is held in place.
    """
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(module_name))
    assert marker in source, f"{module_name}: the enrollment check moved"

    statement = source.split(marker, 1)[1][:300]
    assert "scalar_one_or_none" not in statement, (
        f"{module_name} asserts a single enrollment per (contact, sequence). "
        "The enrol endpoint allows a second one once the first completes, so "
        "this raises on any re-enrolled contact and aborts the agent's run."
    )
    assert ".limit(1)" in statement


async def test_the_enrol_endpoint_still_rejects_a_duplicate_active_enrollment(
    client, db_session, contact_and_sequence
):
    """The fix must not turn the 409 into an accidental second enrollment."""
    contact, sequence = contact_and_sequence
    db_session.add(make_enrollment(contact, sequence, "active"))
    await db_session.commit()

    response = await client.post(
        "/api/v1/outreach/enrollments",
        json={"contact_id": str(contact.id), "sequence_id": str(sequence.id)},
    )

    assert response.status_code == 409, response.text


async def test_the_enrol_endpoint_survives_two_active_enrollments(
    client, db_session, contact_and_sequence
):
    """Two active rows are reachable by a race, and must still answer 409.

    Nothing in the schema prevents two concurrent enrol calls both passing the
    check and both inserting. Once that has happened, the endpoint asserting a
    single active enrollment answers every later attempt with a 500 instead of
    the 409 it exists to give.
    """
    contact, sequence = contact_and_sequence
    db_session.add_all(
        [
            make_enrollment(contact, sequence, "active"),
            make_enrollment(contact, sequence, "active"),
        ]
    )
    await db_session.commit()

    response = await client.post(
        "/api/v1/outreach/enrollments",
        json={"contact_id": str(contact.id), "sequence_id": str(sequence.id)},
    )

    assert response.status_code == 409, response.text
