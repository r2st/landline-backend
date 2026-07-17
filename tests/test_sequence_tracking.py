"""Open/click tracking for outreach sequence sends.

Covers the instrumentation that makes engagement signals real for the sequence
path: pixel/link injection, the OutreachEvent rows the endpoints write, and the
fail-open guard protecting the ``if_opened_no_reply`` branch from silently
skipping forever on untracked sends.
"""

import uuid

import pytest

from app.config import get_settings
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.email_tracking import (
    apply_tracking,
    inject_click_tracking,
    inject_open_pixel,
)
from app.tasks.outreach import _prior_step_engagement, evaluate_step_condition


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

def test_inject_open_pixel_appends_tracking_image():
    eid = str(uuid.uuid4())
    out = inject_open_pixel("<p>Hello</p>", eid, 1)
    assert "/api/v1/track/open/" in out
    assert 'width="1"' in out
    assert "<p>Hello</p>" in out


def test_inject_open_pixel_goes_inside_body_tag():
    eid = str(uuid.uuid4())
    out = inject_open_pixel("<html><body><p>Hi</p></body></html>", eid, 1)
    assert out.index("track/open") < out.index("</body>")


def test_inject_click_tracking_wraps_http_links():
    eid = str(uuid.uuid4())
    out = inject_click_tracking('<a href="https://example.com/demo">Demo</a>', eid, 2)
    assert "/api/v1/track/click/" in out
    assert 'href="https://example.com/demo"' not in out


def test_inject_click_tracking_preserves_unsubscribe_link():
    """Unsubscribe MUST stay a direct link — wrapping it risks CAN-SPAM breakage."""
    eid = str(uuid.uuid4())
    html = '<a href="https://app.example.com/track/unsubscribe?token=abc">Unsubscribe</a>'
    assert inject_click_tracking(html, eid, 1) == html


def test_inject_click_tracking_ignores_non_http_schemes():
    eid = str(uuid.uuid4())
    html = '<a href="mailto:sales@example.com">Email us</a>'
    assert inject_click_tracking(html, eid, 1) == html


def test_apply_tracking_does_not_wrap_its_own_pixel():
    """Links are wrapped before the pixel is appended, so the pixel URL stays raw."""
    eid = str(uuid.uuid4())
    out = apply_tracking('<a href="https://example.com">x</a>', eid, 1, enabled=True)
    # Exactly one open pixel, and it was not itself turned into a click redirect.
    assert out.count("/api/v1/track/open/") == 1
    assert "track/click" in out
    pixel_fragment = out[out.index("<img") :]
    assert "track/click" not in pixel_fragment


def test_apply_tracking_disabled_is_a_noop():
    eid = str(uuid.uuid4())
    html = '<a href="https://example.com">x</a>'
    assert apply_tracking(html, eid, 1, enabled=False) == html


def test_apply_tracking_respects_config_flag(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "sequence_tracking_enabled", False)
    html = "<p>hi</p>"
    assert apply_tracking(html, str(uuid.uuid4()), 1) == html


def test_injection_never_raises_on_malformed_html():
    """A tracking failure must never be able to stop a send."""
    eid = str(uuid.uuid4())
    assert inject_click_tracking("", eid, 1) == ""
    assert inject_open_pixel("", eid, 1) == ""
    assert "track/open" in inject_open_pixel("<a href=", eid, 1)


# ---------------------------------------------------------------------------
# Endpoint → OutreachEvent
# ---------------------------------------------------------------------------

async def _make_enrollment(db, contact, step_number=1, tracked=True):
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db.add(seq)
    await db.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=step_number,
        delay_days=0, subject_template="s", body_template="b",
    )
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=step_number,
    )
    db.add_all([step, enrollment])
    await db.flush()
    db.add(
        OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=step_number,
            event_type="sent", event_metadata={"tracked": tracked},
        )
    )
    await db.flush()
    return seq, step, enrollment


@pytest.mark.asyncio
async def test_open_token_records_opened_event(db_session, sample_contact):
    from sqlalchemy import select

    from app.routers.tracking import _record_outreach_engagement

    _, _, enrollment = await _make_enrollment(db_session, sample_contact)

    wrote = await _record_outreach_engagement(db_session, str(enrollment.id), 1, "opened")
    assert wrote is True

    events = (await db_session.execute(
        select(OutreachEvent).where(
            OutreachEvent.enrollment_id == enrollment.id,
            OutreachEvent.event_type == "opened",
        )
    )).scalars().all()
    assert len(events) == 1
    assert events[0].step_number == 1


@pytest.mark.asyncio
async def test_repeat_opens_are_deduplicated(db_session, sample_contact):
    """Clients re-fetch the pixel on every render; the optimizer reads counts."""
    from sqlalchemy import select

    from app.routers.tracking import _record_outreach_engagement

    _, _, enrollment = await _make_enrollment(db_session, sample_contact)

    assert await _record_outreach_engagement(db_session, str(enrollment.id), 1, "opened") is True
    assert await _record_outreach_engagement(db_session, str(enrollment.id), 1, "opened") is False
    assert await _record_outreach_engagement(db_session, str(enrollment.id), 1, "opened") is False

    events = (await db_session.execute(
        select(OutreachEvent).where(
            OutreachEvent.enrollment_id == enrollment.id,
            OutreachEvent.event_type == "opened",
        )
    )).scalars().all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_unknown_enrollment_is_ignored(db_session):
    from app.routers.tracking import _record_outreach_engagement

    assert await _record_outreach_engagement(db_session, str(uuid.uuid4()), 1, "opened") is False


# ---------------------------------------------------------------------------
# Fail-open guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_if_opened_no_reply_fails_open_when_prior_send_untracked(
    db_session, sample_contact
):
    """The regression that made this branch dead code.

    Prior step sent with no pixel → no open can ever be recorded → gating on a
    positive open signal would skip this step forever. It must send instead.
    """
    seq, _, enrollment = await _make_enrollment(
        db_session, sample_contact, step_number=1, tracked=False
    )
    step2 = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=2, delay_days=1,
        subject_template="s2", body_template="b2", send_condition="if_opened_no_reply",
    )
    db_session.add(step2)
    enrollment.current_step = 2
    await db_session.flush()

    should_send, reason = await evaluate_step_condition(db_session, enrollment, step2)
    assert should_send is True
    assert "without open tracking" in reason


@pytest.mark.asyncio
async def test_if_opened_no_reply_skips_when_tracked_and_unopened(
    db_session, sample_contact
):
    """With a pixel actually sent, a missing open is a real negative — skip."""
    seq, _, enrollment = await _make_enrollment(
        db_session, sample_contact, step_number=1, tracked=True
    )
    step2 = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=2, delay_days=1,
        subject_template="s2", body_template="b2", send_condition="if_opened_no_reply",
    )
    db_session.add(step2)
    enrollment.current_step = 2
    await db_session.flush()

    should_send, reason = await evaluate_step_condition(db_session, enrollment, step2)
    assert should_send is False
    assert "not opened" in reason


@pytest.mark.asyncio
async def test_if_opened_no_reply_sends_when_opened(db_session, sample_contact):
    seq, _, enrollment = await _make_enrollment(
        db_session, sample_contact, step_number=1, tracked=True
    )
    db_session.add(
        OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
            event_type="opened", event_metadata={},
        )
    )
    step2 = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=2, delay_days=1,
        subject_template="s2", body_template="b2", send_condition="if_opened_no_reply",
    )
    db_session.add(step2)
    enrollment.current_step = 2
    await db_session.flush()

    should_send, _ = await evaluate_step_condition(db_session, enrollment, step2)
    assert should_send is True


@pytest.mark.asyncio
async def test_prior_step_engagement_reports_tracked_flag(db_session, sample_contact):
    _, _, enrollment = await _make_enrollment(
        db_session, sample_contact, step_number=1, tracked=True
    )
    eng = await _prior_step_engagement(db_session, enrollment, 1)
    assert eng["prior_tracked"] is True
    assert eng["opened"] is False
    assert eng["replied"] is False
