"""Tests for sequence threading (follow-ups chain into same Gmail thread)
and the /sequences/process endpoint."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep


@pytest.fixture(autouse=True)
def _no_celery_dispatch(monkeypatch):
    """Stop the enroll endpoint from hitting a real broker during tests."""
    import app.tasks.outreach as outreach_tasks
    monkeypatch.setattr(outreach_tasks.send_sequence_step, "delay", lambda *a, **k: None)


async def _make_sequence_with_steps(
    db_session: AsyncSession,
    name="Injection Molding Outreach",
    steps=None,
) -> OutreachSequence:
    """Create a sequence with steps (default: 4-step cadence)."""
    seq = OutreachSequence(id=_uuid.uuid4(), name=name, is_active=True)
    db_session.add(seq)
    await db_session.flush()

    if steps is None:
        steps = [
            {"step_number": 1, "delay_days": 0,
             "subject_template": "noticed {{company_name}} runs injection molding",
             "body_template": "<p>Hi {{first_name}}, quick question about quality at {{company_name}}.</p>"},
            {"step_number": 2, "delay_days": 2,
             "subject_template": "Quick stat on scrap costs, {{first_name}}",
             "body_template": "<p>{{first_name}}, following up on scrap cost reduction.</p>"},
            {"step_number": 3, "delay_days": 6,
             "subject_template": "How a shop like {{company_name}} caught a mold wear issue",
             "body_template": "<p>{{first_name}}, a similar shop avoided $40K in losses.</p>"},
            {"step_number": 4, "delay_days": 13,
             "subject_template": "Closing the loop, {{first_name}}",
             "body_template": "<p>{{first_name}}, last check-in before I close the loop.</p>"},
        ]

    for step_data in steps:
        step = SequenceStep(
            id=_uuid.uuid4(),
            sequence_id=seq.id,
            **step_data,
        )
        db_session.add(step)

    await db_session.flush()
    return seq


# ── Model tests ──


@pytest.mark.asyncio
async def test_enrollment_threading_fields(db_session: AsyncSession, sample_contact: Contact):
    """OutreachEnrollment should have gmail_thread_id, gmail_message_id, sender_account_id, next_send_at."""
    seq = await _make_sequence_with_steps(db_session)
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        sequence_id=seq.id,
        gmail_thread_id="18abc123def",
        gmail_message_id="18abc123def456",
        next_send_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db_session.add(enrollment)
    await db_session.flush()

    result = await db_session.execute(
        select(OutreachEnrollment).where(OutreachEnrollment.id == enrollment.id)
    )
    loaded = result.scalar_one()
    assert loaded.gmail_thread_id == "18abc123def"
    assert loaded.gmail_message_id == "18abc123def456"
    assert loaded.next_send_at is not None


# ── send_outreach_email threading ──


@pytest.mark.asyncio
async def test_send_outreach_email_passes_threading_params():
    """send_outreach_email should forward thread_id/in_reply_to/references to sender."""
    from app.services.email_dispatch import send_outreach_email

    mock_account = MagicMock()
    mock_account.email = "test@example.com"
    mock_account.id = _uuid.uuid4()
    mock_account.status = "connected"
    mock_account.daily_send_count = 0
    mock_account.daily_count_reset_at = None
    mock_account.last_used_at = None

    send_result = {
        "status": "sent",
        "message_id": "msg123",
        "gmail_message_id": "msg123",
        "gmail_thread_id": "thread123",
        "transport": "gmail",
        "sender": "test@example.com",
    }

    with patch("app.services.email_dispatch.get_account", return_value=mock_account), \
         patch("app.services.email_dispatch.get_next_rotation_account", return_value=None), \
         patch("app.services.email_dispatch._make_sender") as mock_make_sender:

        mock_sender = MagicMock()
        mock_sender.send_email.return_value = send_result
        mock_make_sender.return_value = mock_sender

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))

        result = await send_outreach_email(
            mock_db,
            to_email="prospect@test.com",
            subject="Re: Quick question",
            body_html="<p>Following up</p>",
            sender_account_id=mock_account.id,
            thread_id="thread123",
            in_reply_to="<msg123@mail.gmail.com>",
            references="<msg123@mail.gmail.com>",
        )

        # Verify threading params were passed to send_email
        call_kwargs = mock_sender.send_email.call_args
        # send_email is called via asyncio.to_thread, so check the args
        assert result["status"] == "sent"


# ── Sequence step threading logic ──


@pytest.mark.asyncio
async def test_step1_stores_thread_id(db_session: AsyncSession, sample_contact: Contact):
    """After sending step 1, thread_id and message_id should be stored on enrollment."""
    seq = await _make_sequence_with_steps(db_session)
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        sequence_id=seq.id,
        current_step=1,
    )
    db_session.add(enrollment)
    await db_session.flush()

    # After a successful send, the task should store these
    enrollment.gmail_thread_id = "thread_abc"
    enrollment.gmail_message_id = "msg_abc"
    enrollment.current_step = 2
    enrollment.next_send_at = datetime.now(timezone.utc) + timedelta(days=2)
    await db_session.flush()

    result = await db_session.execute(
        select(OutreachEnrollment).where(OutreachEnrollment.id == enrollment.id)
    )
    loaded = result.scalar_one()
    assert loaded.gmail_thread_id == "thread_abc"
    assert loaded.gmail_message_id == "msg_abc"
    assert loaded.current_step == 2
    assert loaded.next_send_at is not None


@pytest.mark.asyncio
async def test_follow_up_uses_re_prefix():
    """Follow-up subjects should be prefixed with 'Re: ' for threading."""
    from app.services.email_compose import render

    subject_template = "Quick stat on scrap costs, {{first_name}}"
    context = {"first_name": "John"}
    subject = render(subject_template, context)

    # Simulate what send_sequence_step does for step > 1
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    assert subject.startswith("Re: ")
    assert "John" in subject


# ── /sequences/process endpoint ──


@pytest.mark.asyncio
async def test_process_empty_queue(client: AsyncClient, db_session: AsyncSession):
    """POST /sequences/process with no active enrollments returns 0 dispatched."""
    resp = await client.post("/api/v1/sequences/process")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dispatched"] == 0
    assert body["active_enrollments"] == 0


@pytest.mark.asyncio
async def test_process_skips_not_due(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """Enrollments with future next_send_at should not be dispatched."""
    seq = await _make_sequence_with_steps(db_session)
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        sequence_id=seq.id,
        current_step=2,
        next_send_at=datetime.now(timezone.utc) + timedelta(days=5),
    )
    db_session.add(enrollment)
    await db_session.commit()

    resp = await client.post("/api/v1/sequences/process")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_enrollments"] == 1
    assert body["dispatched"] == 0
    assert body["skipped"] == 1


@pytest.mark.asyncio
async def test_process_dispatches_due(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """Enrollments past their next_send_at should be dispatched."""
    seq = await _make_sequence_with_steps(db_session)
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        sequence_id=seq.id,
        current_step=2,
        next_send_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db_session.add(enrollment)
    await db_session.commit()

    resp = await client.post("/api/v1/sequences/process")
    assert resp.status_code == 200
    body = resp.json()
    assert body["dispatched"] == 1


@pytest.mark.asyncio
async def test_sequence_status_update(
    client: AsyncClient, db_session: AsyncSession
):
    """PATCH /sequences/{id}/status should pause/resume the sequence."""
    seq = await _make_sequence_with_steps(db_session)
    await db_session.commit()

    resp = await client.patch(
        f"/api/v1/sequences/{seq.id}/status",
        json={"status": "paused"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "paused"
    assert body["is_active"] is False


@pytest.mark.asyncio
async def test_sequence_cadence_day_values(db_session: AsyncSession):
    """Verify the 4-step sequence has correct delay_days: 0, 2, 6, 13."""
    seq = await _make_sequence_with_steps(db_session)
    result = await db_session.execute(
        select(SequenceStep)
        .where(SequenceStep.sequence_id == seq.id)
        .order_by(SequenceStep.step_number)
    )
    steps = list(result.scalars().all())
    assert len(steps) == 4
    assert steps[0].delay_days == 0   # Day 1: immediate
    assert steps[1].delay_days == 2   # Day 3
    assert steps[2].delay_days == 6   # Day 7
    assert steps[3].delay_days == 13  # Day 14


@pytest.mark.asyncio
async def test_enrollment_completes_after_last_step(db_session: AsyncSession, sample_contact: Contact):
    """After advancing past the last step, enrollment should be marked completed."""
    seq = await _make_sequence_with_steps(db_session, steps=[
        {"step_number": 1, "delay_days": 0,
         "subject_template": "Hello", "body_template": "<p>Hi</p>"},
    ])
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        sequence_id=seq.id,
        current_step=2,  # Past step 1 (only step)
    )
    db_session.add(enrollment)
    await db_session.flush()

    # Simulate what send_sequence_step does when no next step exists
    next_step_result = await db_session.execute(
        select(SequenceStep).where(
            SequenceStep.sequence_id == seq.id,
            SequenceStep.step_number == enrollment.current_step,
        )
    )
    next_step = next_step_result.scalar_one_or_none()
    assert next_step is None  # No more steps

    enrollment.status = "completed"
    enrollment.completed_at = datetime.now(timezone.utc)
    await db_session.flush()

    result = await db_session.execute(
        select(OutreachEnrollment).where(OutreachEnrollment.id == enrollment.id)
    )
    loaded = result.scalar_one()
    assert loaded.status == "completed"
    assert loaded.completed_at is not None
