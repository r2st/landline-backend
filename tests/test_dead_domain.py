"""Writing off a recipient domain that has proved it will never take our mail.

Every deliverability gate in the system reasons about one address at a time, so
none of them can notice that the last three addresses we tried at ``acme.com``
were all dead. That matters because the pipeline *derives* addresses when Apollo
and Hunter are out of credits (``email_pattern_generator``), and a wrong address
convention is wrong for everyone at the company — the guesses fail as a set.

Each of those failures is a hard bounce charged against the **global** rate the
kill switch reads at 5%, so a handful of dead domains spends the whole fleet's
safety margin. ``bounce_analytics.by_recipient_domain`` already computes this
shape and renders it on a dashboard; nothing consulted it before sending.

Three clauses have to hold before a domain is written off, and each has a test
here for both sides:

1. hard bounces at ≥ N **distinct** addresses (one mailbox retried is one bad
   address, not a bad domain);
2. no open, click or reply from anywhere at the domain, ever, across **both**
   send tables;
3. not a mailbox provider — ``gmail.com`` cannot be written off on three
   bounces, and clause 2 does not protect it on a young list.
"""

import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bounce_record import BounceRecord
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.models.suppression import SuppressionEntry
from app.services import bounce_handler, domain_reputation

DOMAIN = "deadco.test"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _bounced(db: AsyncSession, *addresses: str, kind: str = "hard") -> None:
    """Ledger rows, as the mailbox sweep writes them."""
    for i, addr in enumerate(addresses):
        db.add(
            BounceRecord(
                id=_uuid.uuid4(),
                gmail_message_id=f"msg-{kind}-{addr}-{i}",
                recipient_email=addr,
                bounce_kind=kind,
                status_code="5.1.1" if kind == "hard" else "4.2.2",
                diagnostic="550 5.1.1 user unknown",
            )
        )
    await db.flush()


async def _contact(
    db: AsyncSession, company: Company, email: str, **kwargs
) -> Contact:
    contact = Contact(
        id=_uuid.uuid4(), company_id=company.id,
        first_name=email.split("@")[0], email=email, source="test", **kwargs,
    )
    db.add(contact)
    await db.flush()
    return contact


async def _engagement_event(
    db: AsyncSession, contact: Contact, event_type: str
) -> None:
    """A sequence-path open/click/reply — ``OutreachEvent``, no ``EmailSend``."""
    seq = OutreachSequence(id=_uuid.uuid4(), name="s", is_active=True)
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db.add(enr)
    await db.flush()
    db.add(
        OutreachEvent(
            id=_uuid.uuid4(), enrollment_id=enr.id, step_number=1,
            event_type=event_type, occurred_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()


# --------------------------------------------------------------------------
# Clause 1 — distinct hard-bounced addresses
# --------------------------------------------------------------------------


async def test_a_clean_domain_is_not_dead(db_session: AsyncSession):
    dead, reason = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is False
    assert reason == ""


async def test_below_the_threshold_the_domain_stands(db_session: AsyncSession):
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}")

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is False


async def test_three_distinct_dead_addresses_write_the_domain_off(
    db_session: AsyncSession,
):
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}", f"c@{DOMAIN}")

    dead, reason = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is True
    assert DOMAIN in reason
    assert "3 distinct" in reason


async def test_one_mailbox_bouncing_repeatedly_is_one_bad_address(
    db_session: AsyncSession,
):
    """The distinction ``bounce_analytics`` draws: twelve bounces across twelve
    addresses is a dead domain, one address retried twelve times is not."""
    for i in range(6):
        db_session.add(
            BounceRecord(
                id=_uuid.uuid4(), gmail_message_id=f"retry-{i}",
                recipient_email=f"same@{DOMAIN}", bounce_kind="hard",
            )
        )
    await db_session.flush()

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is False


async def test_case_differences_are_one_address_not_three(
    db_session: AsyncSession,
):
    """A DSN can echo the address in whatever case the header carried."""
    for i, addr in enumerate([f"Same@{DOMAIN}", f"SAME@{DOMAIN}", f"same@{DOMAIN}"]):
        db_session.add(
            BounceRecord(
                id=_uuid.uuid4(), gmail_message_id=f"case-{i}",
                recipient_email=addr, bounce_kind="hard",
            )
        )
    await db_session.flush()

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is False


async def test_soft_bounces_never_write_off_a_domain(db_session: AsyncSession):
    """Transient by definition. Suppressing a company over greylisting would be
    absurd, however many arrive."""
    await _bounced(
        db_session, f"a@{DOMAIN}", f"b@{DOMAIN}", f"c@{DOMAIN}", f"d@{DOMAIN}",
        kind="soft",
    )

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is False


async def test_a_neighbouring_domain_does_not_count(
    db_session: AsyncSession,
):
    """Suffix match on ``@domain``, not a substring: ``notdeadco.test`` and an
    address whose *local* part contains the string are different domains."""
    await _bounced(
        db_session,
        f"a@not{DOMAIN}", f"b@not{DOMAIN}", f"c@not{DOMAIN}",
        f"{DOMAIN}@elsewhere.test",
    )

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is False


async def test_a_subdomain_is_a_different_mail_host(db_session: AsyncSession):
    """``mail.acme.com`` can be healthy while ``acme.com`` is dead."""
    await _bounced(
        db_session, f"a@mail.{DOMAIN}", f"b@mail.{DOMAIN}", f"c@mail.{DOMAIN}"
    )

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is False
    dead_sub, _ = await domain_reputation.is_domain_dead(db_session, f"mail.{DOMAIN}")
    assert dead_sub is True


async def test_contact_stamps_count_when_no_ledger_row_exists(
    db_session: AsyncSession, sample_company: Company
):
    """``BounceRecord`` is the *sweep's* dedup key, not a complete record.

    The deliverability router's manual entry and the reply task both call
    ``record_bounce`` directly and leave no ledger row — counting the ledger
    alone would let a domain keep its send budget purely because of which code
    path noticed it was dead.
    """
    for name in ("a", "b", "c"):
        await _contact(
            db_session, sample_company, f"{name}@{DOMAIN}",
            last_bounce_type="hard", bounce_count=1,
        )

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is True


async def test_the_two_sources_are_unioned_not_added(
    db_session: AsyncSession, sample_company: Company
):
    """One address in both the ledger and a contact stamp is still one address."""
    for name in ("a", "b"):
        await _contact(
            db_session, sample_company, f"{name}@{DOMAIN}",
            last_bounce_type="hard", bounce_count=1,
        )
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}")

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is False, "two addresses seen twice is not four addresses"


# --------------------------------------------------------------------------
# Clause 2 — any sign of life exempts the domain
# --------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", ["opened", "clicked", "replied"])
async def test_engagement_anywhere_at_the_domain_exempts_it(
    db_session: AsyncSession, sample_company: Company, event_type: str
):
    """Positive evidence outranks negative. Three dead mailboxes at a company
    that has replied to us means three bad addresses, not a dead domain."""
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}", f"c@{DOMAIN}")
    live = await _contact(db_session, sample_company, f"jane@{DOMAIN}")
    await _engagement_event(db_session, live, event_type)

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is False


async def test_engagement_recorded_on_the_other_send_table_also_exempts(
    db_session: AsyncSession, sample_company: Company
):
    """Sequence sends write ``OutreachEvent`` and no ``EmailSend`` row at all.

    Reading one table would make a domain look silent purely because its
    relationship ran through the other path — the split that made the unified
    inbox blind to every sequence send.
    """
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}", f"c@{DOMAIN}")
    live = await _contact(db_session, sample_company, f"jane@{DOMAIN}")
    db_session.add(
        EmailSend(
            id=_uuid.uuid4(), contact_id=live.id, to_email=live.email,
            subject="hi", status="sent",
            sent_at=datetime.now(timezone.utc) - timedelta(days=2),
            replied_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    await db_session.flush()

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is False


async def test_a_send_alone_is_not_a_sign_of_life(
    db_session: AsyncSession, sample_company: Company
):
    """We record a send when the dispatcher returns, which says nothing about
    what the receiving host did with it."""
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}", f"c@{DOMAIN}")
    quiet = await _contact(db_session, sample_company, f"jane@{DOMAIN}")
    await _engagement_event(db_session, quiet, "sent")
    db_session.add(
        EmailSend(
            id=_uuid.uuid4(), contact_id=quiet.id, to_email=quiet.email,
            subject="hi", status="sent", sent_at=datetime.now(timezone.utc),
        )
    )
    await db_session.flush()

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is True


async def test_engagement_at_a_neighbouring_domain_does_not_exempt(
    db_session: AsyncSession, sample_company: Company
):
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}", f"c@{DOMAIN}")
    other = await _contact(db_session, sample_company, f"jane@not{DOMAIN}")
    await _engagement_event(db_session, other, "replied")

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is True


# --------------------------------------------------------------------------
# Clause 3 — mailbox providers, and the disable switch
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider", ["gmail.com", "outlook.com", "yahoo.com", "icloud.com", "proton.me"]
)
async def test_a_mailbox_provider_is_never_written_off(
    db_session: AsyncSession, provider: str
):
    """A bounce here is one wrong address out of a namespace of a billion, and
    the blast radius of being wrong is the entire send programme. Clause 2 would
    normally cover this, but not on day one of a fresh deployment."""
    await _bounced(
        db_session, f"a@{provider}", f"b@{provider}", f"c@{provider}", f"d@{provider}"
    )

    dead, _ = await domain_reputation.is_domain_dead(db_session, provider)
    assert dead is False


async def test_a_threshold_below_one_disables_the_rule(db_session: AsyncSession):
    """An operator who wants the old address-at-a-time behaviour gets it by
    configuration, not by a value that writes off every bounced domain."""
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}", f"c@{DOMAIN}")

    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN, threshold=0)
    assert dead is False


async def test_the_threshold_defaults_to_the_configured_value(
    db_session: AsyncSession,
):
    from app.config import get_settings

    configured = get_settings().dead_domain_bounce_threshold
    await _bounced(db_session, *[f"x{i}@{DOMAIN}" for i in range(configured - 1)])
    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is False, "one short of the setting must not trip it"

    await _bounced(db_session, f"x{configured}@{DOMAIN}")
    dead, _ = await domain_reputation.is_domain_dead(db_session, DOMAIN)
    assert dead is True


@pytest.mark.parametrize("value", [None, "", "not-an-address", "localhost"])
async def test_unparseable_domains_are_never_dead(db_session: AsyncSession, value):
    dead, reason = await domain_reputation.is_domain_dead(db_session, value)
    assert dead is False
    assert reason == ""


# --------------------------------------------------------------------------
# Acting on the verdict — bounce_handler
# --------------------------------------------------------------------------


async def test_the_bounce_that_proves_it_suppresses_the_colleagues(
    db_session: AsyncSession, sample_company: Company
):
    """The whole point: the third bounce takes the *remaining* mailboxes out.

    Without this the colleagues stay enrolled and due, and we walk the list one
    guaranteed hard bounce at a time — each charged against the global rate the
    kill switch reads.
    """
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}")
    third = await _contact(db_session, sample_company, f"c@{DOMAIN}")
    survivors = [
        await _contact(db_session, sample_company, f"jane@{DOMAIN}"),
        await _contact(db_session, sample_company, f"info@{DOMAIN}"),
    ]
    seq = OutreachSequence(id=_uuid.uuid4(), name="s", is_active=True)
    db_session.add(seq)
    await db_session.flush()
    for contact in survivors:
        db_session.add(
            OutreachEnrollment(
                id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
                current_step=1, status="active",
            )
        )
    await db_session.flush()

    result = await bounce_handler.record_bounce(
        db_session, third.email, status_code="5.1.1", diagnostic="user unknown"
    )

    written_off = result["domain_written_off"]
    assert written_off is not None
    assert written_off["domain"] == DOMAIN
    assert written_off["contacts_suppressed"] == 2
    assert written_off["enrollments_paused"] == 2

    for contact in survivors:
        await db_session.refresh(contact)
        assert contact.suppressed is True
    stopped = (
        await db_session.execute(
            select(OutreachEnrollment.status).where(
                OutreachEnrollment.sequence_id == seq.id
            )
        )
    ).scalars().all()
    assert set(stopped) == {"bounced"}


async def test_the_survivors_reach_the_address_level_suppression_list(
    db_session: AsyncSession, sample_company: Company
):
    """The contact flag dies with the contact row; a re-import creates a fresh
    one with ``suppressed = False``. The address list is what survives it."""
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}")
    third = await _contact(db_session, sample_company, f"c@{DOMAIN}")
    await _contact(db_session, sample_company, f"jane@{DOMAIN}")

    await bounce_handler.record_bounce(db_session, third.email, kind="hard")

    listed = set(
        (
            await db_session.execute(
                select(SuppressionEntry.email).where(
                    SuppressionEntry.email.like(f"%@{DOMAIN}")
                )
            )
        ).scalars().all()
    )
    assert f"jane@{DOMAIN}" in listed
    assert f"c@{DOMAIN}" in listed


async def test_a_bounce_short_of_the_threshold_touches_no_colleague(
    db_session: AsyncSession, sample_company: Company
):
    await _bounced(db_session, f"a@{DOMAIN}")
    second = await _contact(db_session, sample_company, f"b@{DOMAIN}")
    survivor = await _contact(db_session, sample_company, f"jane@{DOMAIN}")

    result = await bounce_handler.record_bounce(db_session, second.email, kind="hard")

    assert result["domain_written_off"] is None
    await db_session.refresh(survivor)
    assert survivor.suppressed is not True


async def test_a_soft_bounce_never_writes_off_the_domain_through_the_handler(
    db_session: AsyncSession, sample_company: Company
):
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}", f"c@{DOMAIN}")
    survivor = await _contact(db_session, sample_company, f"jane@{DOMAIN}")
    victim = await _contact(db_session, sample_company, f"d@{DOMAIN}")

    result = await bounce_handler.record_bounce(db_session, victim.email, kind="soft")

    assert result["domain_written_off"] is None
    await db_session.refresh(survivor)
    assert survivor.suppressed is not True


async def test_a_bounce_with_no_contact_row_still_checks_the_domain(
    db_session: AsyncSession, sample_company: Company
):
    """A re-import that replaced the contact is exactly how an address ends up
    with no row, and the domain evidence is then all that is left."""
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}", f"c@{DOMAIN}")
    survivor = await _contact(db_session, sample_company, f"jane@{DOMAIN}")

    result = await bounce_handler.record_bounce(
        db_session, f"ghost@{DOMAIN}", kind="hard"
    )

    assert result["matched_contacts"] == 0
    assert result["domain_written_off"]["contacts_suppressed"] == 1
    await db_session.refresh(survivor)
    assert survivor.suppressed is True


async def test_writing_off_a_domain_is_idempotent(
    db_session: AsyncSession, sample_company: Company
):
    """A second DSN for the same dead domain must not re-report contacts it
    already took out — the count is what an operator reads to size the damage."""
    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}", f"c@{DOMAIN}")
    await _contact(db_session, sample_company, f"jane@{DOMAIN}")

    first = await bounce_handler.record_bounce(db_session, f"x@{DOMAIN}", kind="hard")
    second = await bounce_handler.record_bounce(db_session, f"y@{DOMAIN}", kind="hard")

    assert first["domain_written_off"]["contacts_suppressed"] == 1
    assert second["domain_written_off"]["contacts_suppressed"] == 0


# --------------------------------------------------------------------------
# The backstop at the send chokepoint
# --------------------------------------------------------------------------


def _stub_pipeline(monkeypatch, db_session):
    """Neutralise everything around the gate under test (mirrors the pattern in
    ``test_company_send_throttle``)."""
    import app.tasks.outreach as outreach_tasks
    from app.services import ai_compose

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)

    async def _compose(db, enrollment, contact, company, step, subject, body):
        return ai_compose.ComposeResult("send", subject, body, reason="static template")

    monkeypatch.setattr(ai_compose, "compose_step", _compose)

    class _Optimizer:
        async def decide(self, *a, **k):
            return {"action": "send"}

    monkeypatch.setattr(outreach_tasks, "get_sequence_optimizer", lambda: _Optimizer())
    monkeypatch.setattr(outreach_tasks, "record_decision", lambda *a, **k: None)

    sends = []

    async def _send(db, **kwargs):
        sends.append(kwargs)
        return {"status": "sent", "message_id": "m1"}

    monkeypatch.setattr(outreach_tasks, "send_outreach_email", _send)
    return sends


async def _enrollment_due_now(db: AsyncSession, contact: Contact):
    seq = OutreachSequence(id=_uuid.uuid4(), name="CNC", is_active=True)
    db.add(seq)
    await db.flush()
    db.add(
        SequenceStep(
            id=_uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
            subject_template="hi {{first_name}}", body_template="<p>hi</p>",
        )
    )
    enr = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
        next_send_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(enr)
    await db.flush()
    await db.commit()
    return enr


async def test_the_send_task_refuses_a_contact_found_after_the_write_off(
    db_session: AsyncSession, sample_company: Company, monkeypatch
):
    """The case ``bounce_handler`` structurally cannot cover: a contact
    discovered *after* the domain died has no suppression entry, because the
    address did not exist when the entry list was written."""
    import app.tasks.outreach as outreach_tasks

    await _bounced(db_session, f"a@{DOMAIN}", f"b@{DOMAIN}", f"c@{DOMAIN}")
    newcomer = await _contact(db_session, sample_company, f"newhire@{DOMAIN}")
    # Pre-verified so the address-level gates upstream — the MX check and
    # Hunter — wave it through. They are what this gate exists *beside*: a dead
    # domain that still publishes MX is invisible to both, and pinning them open
    # is the only way to prove this test isn't just re-testing them.
    newcomer.email_status = "valid"
    newcomer.email_verified_at = datetime.now(timezone.utc)
    enrollment = await _enrollment_due_now(db_session, newcomer)
    sends = _stub_pipeline(monkeypatch, db_session)

    result = await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert result["status"] == "skipped"
    assert result["reason"] == "dead_recipient_domain"
    assert sends == [], "nothing may reach the dispatcher"

    status = (
        await db_session.execute(
            select(OutreachEnrollment.status).where(
                OutreachEnrollment.id == enrollment.id
            )
        )
    ).scalar_one()
    assert status == "paused"


async def test_the_send_task_does_not_block_a_healthy_domain(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch
):
    import app.tasks.outreach as outreach_tasks

    sample_contact.email_status = "valid"
    sample_contact.email_verified_at = datetime.now(timezone.utc)
    enrollment = await _enrollment_due_now(db_session, sample_contact)
    sends = _stub_pipeline(monkeypatch, db_session)

    result = await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert result.get("reason") != "dead_recipient_domain"
    assert len(sends) == 1
