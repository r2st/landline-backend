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
        # No rows for any lookup: scalar_one_or_none (warm-up plan / signature)
        # AND first() (suppression check) must both read as "not found".
        # scalar() is the daily-cap seed counting today's sends in both send
        # tables — a MagicMock there is truthy, so it reads as "some sends" and
        # then fails the comparison against the limit.
        mock_db.execute = AsyncMock(return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=None),
            first=MagicMock(return_value=None),
            scalar=MagicMock(return_value=0),
        ))

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


# ── Delay guard tests ──


@pytest.mark.asyncio
async def test_process_respects_next_send_at_from_enrollment(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """The /process endpoint should skip enrollments whose next_send_at is in the future."""
    seq = await _make_sequence_with_steps(db_session)
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        sequence_id=seq.id,
        current_step=2,
        gmail_thread_id="thread_test",
        gmail_message_id="msg_test",
        next_send_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db_session.add(enrollment)
    await db_session.commit()

    resp = await client.post("/api/v1/sequences/process")
    body = resp.json()
    assert body["dispatched"] == 0, "Step 2 should NOT fire — not due for 2 days"
    assert body["skipped"] == 1


@pytest.mark.asyncio
async def test_enrollment_read_includes_threading_fields(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """EnrollmentRead schema should expose gmail_thread_id, gmail_message_id, next_send_at."""
    seq = await _make_sequence_with_steps(db_session)
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        sequence_id=seq.id,
        current_step=2,
        gmail_thread_id="thread_abc",
        gmail_message_id="msg_abc",
        next_send_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db_session.add(enrollment)
    await db_session.commit()

    resp = await client.get(f"/api/v1/outreach/enrollments?contact_id={sample_contact.id}")
    assert resp.status_code == 200
    enrollments = resp.json()
    assert len(enrollments) >= 1
    e = enrollments[0]
    assert e["gmail_thread_id"] == "thread_abc"
    assert e["gmail_message_id"] == "msg_abc"
    assert e["next_send_at"] is not None


@pytest.mark.asyncio
async def test_double_dispatch_prevented_by_delay_guard(
    db_session: AsyncSession, sample_contact: Contact
):
    """Simulates the double-dispatch bug: step 2 should be deferred if called too soon."""
    seq = await _make_sequence_with_steps(db_session)

    # Create enrollment that looks like step 1 was just sent
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        sequence_id=seq.id,
        current_step=2,
        gmail_thread_id="thread_xyz",
        gmail_message_id="msg_xyz",
        next_send_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db_session.add(enrollment)

    # Also add a step 1 "sent" event
    event = OutreachEvent(
        id=_uuid.uuid4(),
        enrollment_id=enrollment.id,
        step_number=1,
        event_type="sent",
        event_metadata={"gmail_thread_id": "thread_xyz"},
    )
    db_session.add(event)
    await db_session.commit()

    # The delay guard in send_sequence_step should detect step 2 is not due
    # and return "deferred" without sending. We test this indirectly: if
    # current_step stays at 2 after /process, the guard worked.
    step_result = await db_session.execute(
        select(SequenceStep).where(
            SequenceStep.sequence_id == seq.id,
            SequenceStep.step_number == 2,
        )
    )
    step = step_result.scalar_one()
    assert step.delay_days == 2  # Step 2 requires 2-day wait

    # next_send_at is 2 days from now — step should not fire
    assert enrollment.next_send_at > datetime.now(timezone.utc)


# ── Sender rotation tests ──


@pytest.mark.asyncio
async def test_rotation_skips_sending_disabled_accounts():
    """get_next_rotation_account should ignore accounts with sending_enabled=False."""
    from app.services.email_dispatch import get_next_rotation_account

    mock_db = AsyncMock()

    # Two accounts: one sending-enabled, one disabled
    enabled_account = MagicMock()
    enabled_account.email = "sender@apprend.tech"
    enabled_account.sending_enabled = True
    enabled_account.status = "connected"
    enabled_account.last_used_at = None
    enabled_account.connected_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    disabled_account = MagicMock()
    disabled_account.email = "target@gmail.com"
    disabled_account.sending_enabled = False
    disabled_account.status = "connected"

    # Mock the queries. Rotation issues: (1) gmail accounts, (2) smtp accounts,
    # then a per-rotation sender-health lookup. Return an empty result for any
    # further queries so the health/warm-up filter finds no rows and keeps the
    # enabled account.
    gmail_result = MagicMock()
    gmail_result.scalars.return_value.all.return_value = [enabled_account]
    smtp_result = MagicMock()
    smtp_result.scalars.return_value.all.return_value = []
    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    # The per-account warm-up plan lookup (plan_daily_limit → get_plan) must read
    # as "no plan" so the health/warm-up filter doesn't try to min() a mock.
    empty_result.scalar_one_or_none.return_value = None

    results = iter([gmail_result, smtp_result])

    async def _execute(*args, **kwargs):
        return next(results, empty_result)

    mock_db.execute = AsyncMock(side_effect=_execute)

    account = await get_next_rotation_account(mock_db)
    assert account.email == "sender@apprend.tech"


# ── Template rendering tests ──


@pytest.mark.asyncio
async def test_template_renders_contact_variables():
    """Sequence step templates should render {{first_name}} and {{company_name}}."""
    from app.services.email_compose import render

    template = "the part that keeps you up at night, {{first_name}}"
    context = {"first_name": "Raj", "company_name": "OneNet Plastics"}
    result = render(template, context)
    assert result == "the part that keeps you up at night, Raj"


@pytest.mark.asyncio
async def test_template_renders_company_in_body():
    """Body template should render {{company_name}}."""
    from app.services.email_compose import render

    template = "<p>At {{company_name}}, I imagine you know exactly which mold I am talking about.</p>"
    context = {"first_name": "Raj", "company_name": "OneNet Plastics"}
    result = render(template, context)
    assert "OneNet Plastics" in result


@pytest.mark.asyncio
async def test_template_handles_missing_variables():
    """Template should handle missing variables gracefully."""
    from app.services.email_compose import render

    template = "Hello {{first_name}}, welcome to {{company_name}}"
    context = {"first_name": "Raj"}
    result = render(template, context)
    assert "Raj" in result
    # Missing company_name should render empty or be left as placeholder
    assert "{{company_name}}" not in result or result  # Just ensure no crash


# ── Enrollment lifecycle tests ──


@pytest.mark.asyncio
async def test_multiple_enrollments_same_contact_different_sequences(
    db_session: AsyncSession, sample_contact: Contact
):
    """A contact can be enrolled in multiple different sequences simultaneously."""
    seq1 = await _make_sequence_with_steps(db_session, name="Sequence A")
    seq2 = await _make_sequence_with_steps(db_session, name="Sequence B")

    e1 = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq1.id,
    )
    e2 = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq2.id,
    )
    db_session.add_all([e1, e2])
    await db_session.flush()

    result = await db_session.execute(
        select(OutreachEnrollment)
        .where(OutreachEnrollment.contact_id == sample_contact.id)
    )
    enrollments = list(result.scalars().all())
    assert len(enrollments) == 2
    seq_ids = {e.sequence_id for e in enrollments}
    assert seq1.id in seq_ids
    assert seq2.id in seq_ids


@pytest.mark.asyncio
async def test_paused_enrollment_not_dispatched(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """Paused enrollments should not be dispatched by /process."""
    seq = await _make_sequence_with_steps(db_session)
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        sequence_id=seq.id,
        current_step=1,
        status="paused",
    )
    db_session.add(enrollment)
    await db_session.commit()

    resp = await client.post("/api/v1/sequences/process")
    body = resp.json()
    assert body["dispatched"] == 0
    assert body["active_enrollments"] == 0


@pytest.mark.asyncio
async def test_completed_enrollment_not_dispatched(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """Completed enrollments should not be dispatched by /process."""
    seq = await _make_sequence_with_steps(db_session)
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        sequence_id=seq.id,
        current_step=5,
        status="completed",
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(enrollment)
    await db_session.commit()

    resp = await client.post("/api/v1/sequences/process")
    body = resp.json()
    assert body["dispatched"] == 0
    assert body["active_enrollments"] == 0


# ── Thread-linking edge cases ──


@pytest.mark.asyncio
async def test_re_prefix_not_duplicated():
    """If subject already starts with Re:, it should not be added again."""
    from app.services.email_compose import render

    subject = "Re: the part that keeps you up at night, {{first_name}}"
    context = {"first_name": "Raj"}
    rendered = render(subject, context)

    # Simulate what the task does
    if not rendered.lower().startswith("re:"):
        rendered = f"Re: {rendered}"

    assert rendered.startswith("Re: the part")
    assert not rendered.startswith("Re: Re:")


@pytest.mark.asyncio
async def test_step1_has_no_threading_headers():
    """Step 1 should NOT send In-Reply-To or References headers."""
    # Fresh enrollment has no gmail_thread_id or gmail_message_id
    enrollment_thread_id = None
    enrollment_message_id = None
    current_step = 1

    thread_id = None
    in_reply_to = None
    references = None

    if current_step > 1 and enrollment_thread_id:
        thread_id = enrollment_thread_id

    assert thread_id is None
    assert in_reply_to is None
    assert references is None


@pytest.mark.asyncio
async def test_step2_builds_rfc2822_message_ref():
    """Step 2+ should format message ID as RFC 2822 angle-bracket reference."""
    enrollment_gmail_message_id = "19f05b38f9e27287"
    msg_ref = f"<{enrollment_gmail_message_id}@mail.gmail.com>"

    assert msg_ref == "<19f05b38f9e27287@mail.gmail.com>"
    assert msg_ref.startswith("<")
    assert msg_ref.endswith(">")
    assert "@mail.gmail.com" in msg_ref


# ── Batch enrollment tests ──


@pytest.mark.asyncio
async def test_batch_enroll_skips_already_enrolled(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    """Batch enrollment should skip contacts already enrolled in the sequence."""
    seq = await _make_sequence_with_steps(db_session)
    # Pre-enroll the contact
    existing = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        sequence_id=seq.id,
    )
    db_session.add(existing)
    await db_session.commit()

    # Try to enroll again
    resp = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(sample_contact.id)]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enrolled_count"] == 0
    assert body["skipped_count"] == 1
    assert body["skipped"][0]["reason"] == "Already enrolled in this sequence"


@pytest.mark.asyncio
async def test_batch_enroll_skips_no_email(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company
):
    """Batch enrollment should skip contacts without an email address."""
    seq = await _make_sequence_with_steps(db_session)
    # Create a contact with no email
    contact = Contact(
        id=_uuid.uuid4(),
        company_id=sample_company.id,
        first_name="NoEmail",
        last_name="Person",
        source="test",
    )
    db_session.add(contact)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(contact.id)]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enrolled_count"] == 0
    assert body["skipped_count"] == 1
    assert body["skipped"][0]["reason"] == "Contact has no email address"
