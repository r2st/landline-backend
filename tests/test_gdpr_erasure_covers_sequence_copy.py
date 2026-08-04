"""Erasure has to reach the table the sequence engine writes to.

Landline stores a sent message in one of two places (see CLAUDE.md, "Sent mail
lives in two tables"). Manual sends, template sends and inbox replies get an
``EmailSend`` row. The **sequence engine** — where the bulk of outreach volume
goes — writes no such row: the recipient address, the subject line and the full
rendered ``body_html`` are persisted in ``outreach_events.metadata`` instead.

``erase_subject`` scrubbed the first table and not the second. So a right-to-
erasure request against a prospect who had been through a five-step cadence
anonymised their contact row, reported success, and left five complete copies of
mail addressed to them by name — plus, in ``ai_email_drafts.personalization``,
the research agent's notes on the individual.

The tests below are written against the *asymmetry* rather than a column list,
because that is the durable statement: what ``export_subject`` is willing to
disclose in a subject access request is exactly what ``erase_subject`` has to be
able to remove. ``test_export_and_erasure_cover_the_same_tables`` is the one that
would have caught this, and is the one that catches the next table added to only
one of them.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.ai_email_draft import AiEmailDraft
from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services import gdpr

_EMAIL = "priya@target-shop.test"


async def _seed_sequence_subject(db, email=_EMAIL):
    """A subject who has actually been through a cadence."""
    company = Company(id=uuid.uuid4(), name="Target Shop", country="US", source="test")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Priya",
        last_name="Raghunathan", email=email, phone="+15559876", source="test",
    )
    db.add(contact)
    sequence = OutreachSequence(id=uuid.uuid4(), name="Q3 cold", is_active=True)
    db.add(sequence)
    await db.flush()
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id,
        status="active", current_step=3, enrolled_at=datetime.now(timezone.utc),
    )
    db.add(enrollment)
    await db.flush()

    # Three steps sent — the shape ``send_sequence_step`` writes.
    for step in (1, 2, 3):
        db.add(OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=step,
            event_type="sent", occurred_at=datetime.now(timezone.utc),
            event_metadata={
                "status": "sent",
                "tracked": True,
                "ai_generated": True,
                "sender_account_id": str(uuid.uuid4()),
                "gmail_thread_id": f"thread-{step}",
                "to_email": email,
                "subject": f"Priya — a question about Target Shop (step {step})",
                "body_html": (
                    "<p>Hi Priya, I saw Target Shop just opened a second "
                    "location on Devon Ave — congratulations.</p>"
                ),
            },
        ))
    # An engagement event with no copy on it: the scrub must not invent one.
    db.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
        event_type="opened", occurred_at=datetime.now(timezone.utc),
        event_metadata={"source": "pixel"},
    ))

    db.add(AiEmailDraft(
        id=uuid.uuid4(), enrollment_id=enrollment.id, sequence_id=sequence.id,
        contact_id=contact.id, company_id=company.id, step_number=4,
        subject="Priya, following up", body="<p>Hi Priya,</p>",
        static_subject="Following up", static_body="<p>Hi {{first_name}},</p>",
        ai_generated=True, research_used=True,
        personalization={
            "note": "second location on Devon Ave",
            "linkedin": "priya-raghunathan",
        },
        send_result={"status": "sent", "to_email": _EMAIL},
        status="approved",
    ))
    await db.commit()
    return contact, enrollment


def _all_event_text(events) -> str:
    return " ".join(str(e.event_metadata) for e in events)


@pytest.mark.asyncio
async def test_erasure_scrubs_the_sequence_send_copy(db_session):
    contact, enrollment = await _seed_sequence_subject(db_session)

    result = await gdpr.erase_subject(db_session, _EMAIL)

    events = list(
        (
            await db_session.execute(
                select(OutreachEvent).where(
                    OutreachEvent.enrollment_id == enrollment.id
                )
            )
        ).scalars().all()
    )
    blob = _all_event_text(events)
    # Nothing identifying the person survives in the send record.
    assert _EMAIL not in blob
    assert "Priya" not in blob
    assert "Devon Ave" not in blob
    assert result["scrubbed"]["outreach_events"] == 3


@pytest.mark.asyncio
async def test_erasure_keeps_the_operational_facts(db_session):
    """Erasure removes the person, not the fact that a message was sent.

    ``status`` and ``tracked`` feed deliverability rates whose denominator is
    attempts; the Gmail ids are what the reply matcher and ``send_claim``
    reconciliation key on. Dropping the blob wholesale would be a quieter bug
    than leaving it — the numbers would simply start being wrong.
    """
    contact, enrollment = await _seed_sequence_subject(db_session)
    await gdpr.erase_subject(db_session, _EMAIL)

    sent = list(
        (
            await db_session.execute(
                select(OutreachEvent).where(
                    OutreachEvent.enrollment_id == enrollment.id,
                    OutreachEvent.event_type == "sent",
                )
            )
        ).scalars().all()
    )
    assert len(sent) == 3
    for ev in sent:
        assert ev.event_metadata["status"] == "sent"
        assert ev.event_metadata["tracked"] is True
        assert ev.event_metadata["ai_generated"] is True
        assert ev.event_metadata["gmail_thread_id"].startswith("thread-")


@pytest.mark.asyncio
async def test_erasure_scrubs_ai_draft_copy_and_research(db_session):
    contact, _ = await _seed_sequence_subject(db_session)
    await gdpr.erase_subject(db_session, _EMAIL)

    draft = (
        await db_session.execute(
            select(AiEmailDraft).where(AiEmailDraft.contact_id == contact.id)
        )
    ).scalars().first()
    assert draft is not None
    assert "Priya" not in (draft.subject or "")
    assert "Priya" not in (draft.body or "")
    assert "Priya" not in (draft.static_subject or "")
    # The research dossier is dropped entirely, not redacted key-by-key: nothing
    # reads a key out of it, so leaving the key names behind would describe what
    # was collected about someone who asked to be forgotten.
    assert draft.personalization == {}
    assert _EMAIL not in str(draft.send_result)


@pytest.mark.asyncio
async def test_erasure_is_idempotent_on_the_event_blob(db_session):
    """A second run reports zero new scrubs rather than rewriting rows.

    The rest of ``erase_subject`` is idempotent via ``anonymized_at``; the event
    pass has no such stamp, so it decides per blob whether anything changed.
    Without that it would report three scrubs forever and an operator could not
    tell a repeat request from a fresh one.
    """
    await _seed_sequence_subject(db_session)
    first = await gdpr.erase_subject(db_session, _EMAIL)
    second = await gdpr.erase_subject(db_session, _EMAIL)
    assert first["scrubbed"]["outreach_events"] == 3
    assert second["scrubbed"]["outreach_events"] == 0


@pytest.mark.asyncio
async def test_erasure_survives_a_reload(db_session):
    """The JSONB column is actually written, not just changed in memory.

    ``event_metadata`` is a plain dict on a JSONB column, so SQLAlchemy does not
    track mutation *inside* it. A scrub that edited keys on the loaded value
    would pass every in-session assertion above and persist nothing — which is
    the exact failure mode this test exists for.
    """
    _, enrollment = await _seed_sequence_subject(db_session)
    # Read the id out before expiring: afterwards, touching the attribute is
    # itself a lazy load, which an async session refuses.
    enrollment_id = enrollment.id
    await gdpr.erase_subject(db_session, _EMAIL)
    db_session.expire_all()

    events = list(
        (
            await db_session.execute(
                select(OutreachEvent).where(
                    OutreachEvent.enrollment_id == enrollment_id
                )
            )
        ).scalars().all()
    )
    assert _EMAIL not in _all_event_text(events)
    assert "Devon Ave" not in _all_event_text(events)


@pytest.mark.asyncio
async def test_export_and_erasure_cover_the_same_tables(db_session):
    """Whatever the SAR discloses, the erasure has to be able to remove.

    Stated as a scan over the export rather than a fixed list, so a table added
    to ``export_subject`` without a matching scrub fails here. The exemptions are
    named individually and each has a reason that does not generalise.
    """
    await _seed_sequence_subject(db_session)
    export = await gdpr.export_subject(db_session, _EMAIL)

    # Sections the export returns that hold no free text about the subject, or
    # whose removal would defeat the erasure itself.
    exempt = {
        "subject_email",       # the request's own input
        "generated_at",
        "record_counts",
        "companies",           # the employer is not the data subject
        "outreach_enrollments",  # ids and status only — no PII columns
        # The consent record is what proves the opt-out/erasure happened; the
        # recognised carve-out for keeping the minimum needed to honour it.
        "consent_records",
    }
    disclosed = {k for k, v in export.items() if k not in exempt and v}

    await gdpr.erase_subject(db_session, _EMAIL)
    after = await gdpr.export_subject(db_session, _EMAIL)

    for section in disclosed:
        rows = after.get(section) or []
        blob = str(rows)
        assert "Priya" not in blob, f"{section} still names the subject"
        assert "Devon Ave" not in blob, f"{section} still holds subject research"
        assert _EMAIL not in blob, f"{section} still holds the subject's address"
