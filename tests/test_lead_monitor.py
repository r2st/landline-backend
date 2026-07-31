"""Daily lead-gen health check (app.tasks.lead_monitor).

The tracking API shipped with zero consumers; this task is the consumer. The
two failure modes it exists to catch, pinned here:

* a day with zero sends on either send path (EmailSend + OutreachEvent),
* sequence mail going out with zero AI-composed copy — the silent
  static-template degradation that ran unnoticed for weeks.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.tasks.lead_monitor import ALERT_PREFIX, collect_lead_gen_health

pytestmark = pytest.mark.asyncio


async def _enrollment(db: AsyncSession, contact: Contact) -> OutreachEnrollment:
    seq = OutreachSequence(id=_uuid.uuid4(), name=f"Seq {_uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
    )
    db.add(enrollment)
    await db.flush()
    return enrollment


def _sent_event(enrollment: OutreachEnrollment, *, ai: bool, when=None) -> OutreachEvent:
    return OutreachEvent(
        id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
        event_type="sent", occurred_at=when or datetime.now(timezone.utc),
        event_metadata={"ai_generated": ai},
    )


async def test_zero_sends_raises_the_dead_pipeline_alert(db_session: AsyncSession):
    report = await collect_lead_gen_health(db_session)

    assert report["sends_today"] == 0
    assert len(report["alerts"]) == 1
    assert report["alerts"][0].startswith(ALERT_PREFIX)
    assert "0 emails sent" in report["alerts"][0]


async def test_all_static_sequence_sends_raise_the_ai_dead_alert(
    db_session: AsyncSession, sample_contact: Contact
):
    enrollment = await _enrollment(db_session, sample_contact)
    db_session.add(_sent_event(enrollment, ai=False))
    db_session.add(_sent_event(enrollment, ai=False))
    await db_session.commit()

    report = await collect_lead_gen_health(db_session)

    assert report["sends_today"] == 2
    assert report["sequence_sends_today"] == 2
    assert report["ai_composed_rate"] == 0.0
    assert len(report["alerts"]) == 1
    assert "ai_composed_rate=0" in report["alerts"][0]


async def test_healthy_day_raises_no_alerts(
    db_session: AsyncSession, sample_contact: Contact
):
    enrollment = await _enrollment(db_session, sample_contact)
    db_session.add(_sent_event(enrollment, ai=True))
    db_session.add(_sent_event(enrollment, ai=False))
    await db_session.commit()

    report = await collect_lead_gen_health(db_session)

    assert report["sends_today"] == 2
    assert report["ai_composed_today"] == 1
    assert report["ai_composed_rate"] == 0.5
    assert report["alerts"] == []


async def test_manual_sends_count_but_do_not_trigger_the_ai_alert(
    db_session: AsyncSession, sample_contact: Contact
):
    # A day of only EmailSend-path mail (manual/template sends) has no
    # sequence sends, so the AI-composition rate is vacuous — no alert.
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id,
        to_email=sample_contact.email, subject="Manual note",
        status="sent", sent_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    report = await collect_lead_gen_health(db_session)

    assert report["sends_today"] == 1
    assert report["sequence_sends_today"] == 0
    assert report["alerts"] == []


async def test_yesterdays_sends_do_not_mask_a_dead_today(
    db_session: AsyncSession, sample_contact: Contact
):
    enrollment = await _enrollment(db_session, sample_contact)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.add(_sent_event(enrollment, ai=True, when=yesterday))
    await db_session.commit()

    report = await collect_lead_gen_health(db_session)

    assert report["sends_today"] == 0
    assert len(report["alerts"]) == 1
    assert "0 emails sent" in report["alerts"][0]


async def test_events_missing_metadata_count_as_static(
    db_session: AsyncSession, sample_contact: Contact
):
    # Sent events written before ea828f6 carry no ai_generated flag at all;
    # they must read as static, not crash the sweep.
    enrollment = await _enrollment(db_session, sample_contact)
    db_session.add(OutreachEvent(
        id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
        event_type="sent", occurred_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    report = await collect_lead_gen_health(db_session)

    assert report["sequence_sends_today"] == 1
    assert report["ai_composed_today"] == 0
    assert "ai_composed_rate=0" in report["alerts"][0]


async def test_beat_schedule_has_the_health_check(db_session):
    from app.tasks.celery_app import celery_app

    entry = celery_app.conf.beat_schedule.get("check-lead-gen-health")
    assert entry is not None
    assert entry["task"] == "app.tasks.lead_monitor.check_lead_gen_health"
    # Daily, after the send window: any crontab is fine, but it must be a
    # crontab (a float interval would fire it mid-morning with a vacuous
    # "0 sends today").
    from celery.schedules import crontab as _crontab
    assert isinstance(entry["schedule"], _crontab)
