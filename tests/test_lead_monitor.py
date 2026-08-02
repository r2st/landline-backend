"""Daily lead-gen health check (app.tasks.lead_monitor).

The tracking API shipped with zero consumers; this task is the consumer. The
two failure modes it exists to catch, pinned here:

* a day with zero sends on either send path (EmailSend + OutreachEvent),
* sequence mail going out with zero AI-composed copy — the silent
  static-template degradation that ran unnoticed for weeks.

Every test below the platform-level ones runs **two orgs**, because that is the
only arrangement in which the bug this file now guards against is visible. Both
alerts used to be evaluated against the platform total, so each asked "is every
tenant dead?" — and a single-org fixture cannot tell that apart from "is this
tenant dead?". The suite was green throughout; it was measuring a sum of one.

That is the general hazard with a single-tenant fixture and an aggregate: the
assertions are all true, and they stay true after the aggregation stops meaning
what the test name says.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.models.tenant import Organization
from app.org_context import reset_current_org_id, set_current_org_id
from app.tasks.lead_monitor import ALERT_PREFIX, collect_lead_gen_health

pytestmark = pytest.mark.asyncio


async def _enrollment(
    db: AsyncSession, contact: Contact, *, status: str = "active"
) -> OutreachEnrollment:
    seq = OutreachSequence(id=_uuid.uuid4(), name=f"Seq {_uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status=status,
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


@pytest_asyncio.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=_uuid.uuid4(), name="Beta Industrial", slug="beta-industrial",
        is_active=True, billing_plan="free",
    )
    db_session.add(org)
    await db_session.commit()
    return org


async def _seed_org(
    db: AsyncSession,
    org_id,
    *,
    sends: int = 0,
    ai_sends: int = 0,
    enrollment_status: str = "active",
):
    """Give ``org_id`` a contact, an enrollment and today's sent events.

    Everything is committed under that org's context so the ``before_flush``
    hook stamps ``org_id`` — seeding without it lands every row on the
    first-party org and the test quietly becomes single-tenant again.
    """
    token = set_current_org_id(org_id)
    try:
        company = Company(
            id=_uuid.uuid4(), name=f"Co {_uuid.uuid4().hex[:6]}", source="test"
        )
        db.add(company)
        await db.flush()
        contact = Contact(
            id=_uuid.uuid4(), company_id=company.id,
            first_name="Lead", last_name="Person",
            email=f"lead-{_uuid.uuid4().hex[:8]}@example.com",
        )
        db.add(contact)
        await db.flush()
        enrollment = await _enrollment(db, contact, status=enrollment_status)
        for i in range(sends):
            db.add(_sent_event(enrollment, ai=i < ai_sends))
        await db.commit()
        return enrollment
    finally:
        reset_current_org_id(token)


def _alerts_naming(report: dict, org: Organization) -> list[str]:
    return [a for a in report["alerts"] if str(org.id) in a]


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


# --- two tenants: what a platform-wide sum cannot represent -----------------

async def test_a_dead_tenant_alerts_even_though_the_platform_is_busy(
    db_session: AsyncSession, org_b: Organization, sample_contact: Contact
):
    """The whole point. One tenant sending masked every other tenant's silence.

    Org B has an ``active`` enrollment and sent nothing; the first-party org
    sent plenty. The platform total is healthy and always was — under the old
    sum this returned zero alerts, which is the alarm reporting "fine" for a
    tenant whose outreach had stopped completely.
    """
    busy = await _enrollment(db_session, sample_contact)
    db_session.add(_sent_event(busy, ai=True))
    db_session.add(_sent_event(busy, ai=True))
    await db_session.commit()
    await _seed_org(db_session, org_b.id, sends=0)

    report = await collect_lead_gen_health(db_session)

    assert report["sends_today"] == 2  # platform total: healthy
    named = _alerts_naming(report, org_b)
    assert len(named) == 1
    assert "sent 0 emails today" in named[0]
    assert "Beta Industrial" in named[0]


async def test_a_tenant_with_nothing_enrolled_does_not_alert(
    db_session: AsyncSession, org_b: Organization, sample_contact: Contact
):
    """Zero sends is only anomalous when something was supposed to send.

    An idle tenant would otherwise emit one ERROR every day forever, and a
    channel that cries wolf daily is one nobody reads — the same reasoning that
    keeps ``DangerMonitor`` to one alert per account per breach.
    """
    busy = await _enrollment(db_session, sample_contact)
    db_session.add(_sent_event(busy, ai=True))
    await db_session.commit()
    await _seed_org(db_session, org_b.id, sends=0, enrollment_status="paused")

    report = await collect_lead_gen_health(db_session)

    assert _alerts_naming(report, org_b) == []
    assert [r for r in report["by_org"] if r["org_id"] == str(org_b.id)][0][
        "expecting_sends"
    ] is False


async def test_one_tenants_dead_llm_key_alerts_under_a_healthy_platform_rate(
    db_session: AsyncSession, org_b: Organization, sample_contact: Contact
):
    """Per-org LLM keys (084) make this a per-org failure, so it needs a per-org rate.

    Org B shipped both of today's sends as the static template — its key is out
    of quota — while the first-party org composed all of its own. The platform
    rate is 0.5 and raises nothing, which is exactly how a tenant runs on
    mail-merge copy for a week without a single log line.
    """
    healthy = await _enrollment(db_session, sample_contact)
    db_session.add(_sent_event(healthy, ai=True))
    db_session.add(_sent_event(healthy, ai=True))
    await db_session.commit()
    await _seed_org(db_session, org_b.id, sends=2, ai_sends=0)

    report = await collect_lead_gen_health(db_session)

    assert report["ai_composed_rate"] == 0.5  # platform rate: unremarkable
    named = _alerts_naming(report, org_b)
    assert len(named) == 1
    assert "static template" in named[0]
    assert "its own LLM key" in named[0]


async def test_a_platform_wide_outage_stays_one_alert(
    db_session: AsyncSession, org_b: Organization, sample_contact: Contact
):
    """When everyone is down, say so once — don't enumerate the tenant list.

    Both orgs have active enrollments and neither sent. The per-org checks would
    each fire, turning a single outage into N lines that scale with the customer
    count; the platform alert subsumes them.
    """
    await _enrollment(db_session, sample_contact)
    await db_session.commit()
    await _seed_org(db_session, org_b.id, sends=0)

    report = await collect_lead_gen_health(db_session)

    assert report["sends_today"] == 0
    assert len(report["alerts"]) == 1
    assert "0 emails sent today" in report["alerts"][0]
    assert str(org_b.id) not in report["alerts"][0]


async def test_by_org_attributes_sends_to_the_tenant_that_made_them(
    db_session: AsyncSession, org_b: Organization, sample_contact: Contact
):
    """The per-org breakdown is only worth having if the attribution is right."""
    healthy = await _enrollment(db_session, sample_contact)
    db_session.add(_sent_event(healthy, ai=True))
    await db_session.commit()
    await _seed_org(db_session, org_b.id, sends=3, ai_sends=2)

    report = await collect_lead_gen_health(db_session)

    rows = {r["org_id"]: r for r in report["by_org"]}
    assert rows[str(org_b.id)]["sequence_sends_today"] == 3
    assert rows[str(org_b.id)]["ai_composed_today"] == 2
    assert sum(r["sequence_sends_today"] for r in report["by_org"]) == (
        report["sequence_sends_today"]
    )


async def test_a_deactivated_tenant_is_not_alerted_on(
    db_session: AsyncSession, org_b: Organization, sample_contact: Contact
):
    """A suspended tenant is not sending on purpose; it is not a stalled queue."""
    busy = await _enrollment(db_session, sample_contact)
    db_session.add(_sent_event(busy, ai=True))
    await db_session.commit()
    await _seed_org(db_session, org_b.id, sends=0)
    org_b.is_active = False
    db_session.add(org_b)
    await db_session.commit()

    report = await collect_lead_gen_health(db_session)

    assert _alerts_naming(report, org_b) == []
    assert str(org_b.id) not in {r["org_id"] for r in report["by_org"]}


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
