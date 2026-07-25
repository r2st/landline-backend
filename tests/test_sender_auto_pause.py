"""Per-sender auto-pause: the response should fit the scope of the problem.

The danger monitor already measured bounce rate per sending account, but its only
lever was the global kill switch — so one mailbox working a bad list slice
stopped every other sender, froze every enrollment, and required a manual resume
with a trip id. These tests pin the surgical behaviour: an account-scope breach
disables that account, a global-scope breach still halts everything, and neither
fires twice for a condition already reported.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.danger_signal import DangerSignalEvent
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.services import system_settings
from app.services.danger_monitor import get_danger_monitor

pytestmark = pytest.mark.asyncio


async def _company(db: AsyncSession) -> Company:
    c = Company(id=uuid.uuid4(), name="Acme", domain="acme.com", country="US", source="test")
    db.add(c)
    await db.flush()
    return c


async def _account(db: AsyncSession, email: str) -> GmailAccount:
    acct = GmailAccount(
        id=uuid.uuid4(),
        email=email,
        google_sub=f"sub-{uuid.uuid4()}",
        refresh_token_encrypted="x",
        is_active=True,
        status="connected",
        sending_enabled=True,
        connected_at=datetime.now(timezone.utc) - timedelta(days=90),
    )
    db.add(acct)
    await db.flush()
    return acct


async def _sends(
    db: AsyncSession,
    company: Company,
    account: GmailAccount,
    *,
    total: int,
    bounced: int = 0,
):
    ts = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(total):
        status = "bounced" if i < bounced else "sent"
        contact = None
        if status == "bounced":
            contact = Contact(
                id=uuid.uuid4(),
                company_id=company.id,
                first_name="A",
                last_name="B",
                email=f"c{uuid.uuid4().hex[:8]}@x.com",
            )
            db.add(contact)
            await db.flush()
        db.add(
            EmailSend(
                id=uuid.uuid4(),
                to_email="lead@x.com",
                subject="hi",
                status=status,
                sent_at=ts,
                from_email=account.email,
                sender_account_id=account.id,
                contact_id=contact.id if contact is not None else None,
            )
        )
    await db.flush()


@pytest.fixture(autouse=True)
def _restore_setting():
    s = get_settings()
    before = s.danger_account_auto_pause_enabled
    yield
    s.danger_account_auto_pause_enabled = before


async def test_breaching_sender_is_paused_and_healthy_ones_keep_sending(
    db_session: AsyncSession,
):
    """One bad mailbox out of two → that mailbox stops, the other doesn't."""
    company = await _company(db_session)
    bad = await _account(db_session, "bad@apprend.tech")
    good = await _account(db_session, "good@apprend.tech")

    # 40 sends from the bad account, 6 bounced → 15%, well over the 5% line and
    # past the 20-send minimum sample.
    await _sends(db_session, company, bad, total=40, bounced=6)
    # 40 clean sends from the good account, and enough of them that the *global*
    # rate (6/80 = 7.5%) … would also breach. Add clean volume so it doesn't:
    await _sends(db_session, company, good, total=200, bounced=0)

    report = await get_danger_monitor().evaluate(db_session)
    assert report.account_breaches, "the bad account should have breached"
    assert not report.global_breaches, "aggregate is diluted below threshold"

    paused = await get_danger_monitor().pause_accounts(db_session, report)
    await db_session.flush()

    assert len(paused) == 1
    assert paused[0]["account_email"] == "bad@apprend.tech"

    await db_session.refresh(bad)
    await db_session.refresh(good)
    assert bad.sending_enabled is False
    assert good.sending_enabled is True


async def test_pause_records_an_auditable_event(db_session: AsyncSession):
    company = await _company(db_session)
    bad = await _account(db_session, "bad@apprend.tech")
    good = await _account(db_session, "good@apprend.tech")
    await _sends(db_session, company, bad, total=40, bounced=6)
    await _sends(db_session, company, good, total=200, bounced=0)

    report = await get_danger_monitor().evaluate(db_session)
    await get_danger_monitor().pause_accounts(db_session, report)
    await db_session.flush()

    events = (
        await db_session.execute(
            select(DangerSignalEvent).where(
                DangerSignalEvent.action_taken == "account_paused"
            )
        )
    ).scalars().all()
    assert len(events) == 1
    event = events[0]
    assert event.scope == "account"
    assert event.account_id == bad.id
    assert event.account_email == "bad@apprend.tech"
    assert event.sample_size == 40
    # The post-mortem needs the number and what it was measured against.
    assert event.observed_value > event.threshold
    assert "reason" in event.details


async def test_pausing_is_idempotent_across_sweeps(db_session: AsyncSession):
    """The monitor runs every 5 minutes; a persisting breach must not re-alert.

    An alert that fires every five minutes for a condition the operator already
    knows about gets muted, and a muted alert is worse than no alert.
    """
    company = await _company(db_session)
    bad = await _account(db_session, "bad@apprend.tech")
    good = await _account(db_session, "good@apprend.tech")
    await _sends(db_session, company, bad, total=40, bounced=6)
    await _sends(db_session, company, good, total=200, bounced=0)

    monitor = get_danger_monitor()
    report = await monitor.evaluate(db_session)
    first = await monitor.pause_accounts(db_session, report)
    await db_session.flush()

    # ``get_sending_accounts`` now excludes the paused account, so re-evaluate
    # from scratch exactly as the next sweep would.
    report2 = await monitor.evaluate(db_session)
    second = await monitor.pause_accounts(db_session, report2)

    assert len(first) == 1
    assert second == []


async def test_one_account_tripping_two_metrics_is_paused_once(db_session: AsyncSession):
    """Bounce rate and hard-bounce count both trip → one pause, one alert."""
    company = await _company(db_session)
    bad = await _account(db_session, "bad@apprend.tech")
    good = await _account(db_session, "good@apprend.tech")

    # 6 hard bounces (>= the count threshold of 5) out of 40 (15% > 5%).
    ts = datetime.now(timezone.utc) - timedelta(hours=1)
    for i in range(40):
        contact = None
        status = "sent"
        if i < 6:
            status = "bounced"
            contact = Contact(
                id=uuid.uuid4(), company_id=company.id, first_name="A", last_name="B",
                email=f"h{uuid.uuid4().hex[:8]}@x.com",
                last_bounce_type="hard", last_bounced_at=ts,
            )
            db_session.add(contact)
            await db_session.flush()
        db_session.add(
            EmailSend(
                id=uuid.uuid4(), to_email="lead@x.com", subject="hi", status=status,
                sent_at=ts, from_email=bad.email, sender_account_id=bad.id,
                contact_id=contact.id if contact is not None else None,
            )
        )
    await _sends(db_session, company, good, total=300, bounced=0)
    await db_session.flush()

    report = await get_danger_monitor().evaluate(db_session)
    bad_breaches = [m for m in report.account_breaches if m.account_id == bad.id]
    assert len(bad_breaches) >= 2, "expected both bounce_rate and hard_bounce_count"

    paused = await get_danger_monitor().pause_accounts(db_session, report)
    assert len(paused) == 1


async def test_disabled_setting_pauses_nothing(db_session: AsyncSession):
    get_settings().danger_account_auto_pause_enabled = False

    company = await _company(db_session)
    bad = await _account(db_session, "bad@apprend.tech")
    good = await _account(db_session, "good@apprend.tech")
    await _sends(db_session, company, bad, total=40, bounced=6)
    await _sends(db_session, company, good, total=200, bounced=0)

    report = await get_danger_monitor().evaluate(db_session)
    assert report.account_breaches
    assert await get_danger_monitor().pause_accounts(db_session, report) == []

    await db_session.refresh(bad)
    assert bad.sending_enabled is True


async def test_global_breach_still_halts_everything(db_session: AsyncSession):
    """A shared-reputation problem is not fixed by pausing one mailbox."""
    company = await _company(db_session)
    a = await _account(db_session, "a@apprend.tech")
    b = await _account(db_session, "b@apprend.tech")
    await _sends(db_session, company, a, total=50, bounced=5)
    await _sends(db_session, company, b, total=50, bounced=5)

    report = await get_danger_monitor().evaluate(db_session)
    assert report.global_breaches

    await get_danger_monitor().trip(db_session, report)
    await db_session.flush()
    assert await system_settings.is_sending_enabled(db_session) is False


async def test_missing_account_is_skipped_not_fatal(db_session: AsyncSession):
    """A breach for an account deleted since the metrics were computed."""
    from app.services.danger_monitor import DangerReport, MetricResult, Thresholds

    report = DangerReport(
        generated_at=datetime.now(timezone.utc),
        window_hours=24,
        thresholds=Thresholds(
            bounce_rate=0.05, bounce_rate_warn=0.03,
            complaint_rate=0.001, complaint_rate_warn=0.0005,
            hard_bounce_count=5, hard_bounce_count_warn=3, min_sample_size=20,
        ),
        sending_enabled=True,
        metrics=[
            MetricResult(
                metric="bounce_rate", scope="account", window="24h",
                observed_value=0.2, threshold=0.05, warn_threshold=0.03,
                sample_size=40, status="breach",
                account_id=uuid.uuid4(), account_email="ghost@apprend.tech",
            )
        ],
    )

    assert await get_danger_monitor().pause_accounts(db_session, report) == []
