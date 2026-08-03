"""``550 5.7.1 likely unsolicited mail`` is a 5, and it is not a dead address.

``classify_bounce`` decided on the status class first: a 5 is permanent,
permanent is hard, and ``record_bounce`` gives a hard bounce the full
dead-address treatment — contact ``suppressed`` and ``unsubscribed``, live
enrollments closed as ``bounced``, a ``reason="bounce"`` entry on the org
do-not-contact list, and past ``dead_domain_bounce_threshold`` distinct
addresses, the same done to every remaining contact at the company.

Nothing in a ``5.7.x`` is about the recipient. Their mailbox is alive and being
read; what was refused was our DMARC alignment, our sending IP's reputation, or
a content verdict. Three properties make that combination expensive rather than
merely wrong:

* **The blast radius is the audience, not the address.** A broken DMARC record
  rejects the whole send, so one afternoon suppresses everyone behind every
  Microsoft 365 and Google Workspace tenant we mailed — most of a B2B list.
* **The domain write-off generalises it.** ``MAILBOX_PROVIDER_DOMAINS`` shields
  ``gmail.com``; it does not shield ``acme.com`` hosted on M365, and the
  colleagues swept up were never named by any rejection.
* **It does not wash out.** Fixing the DNS record restores sending and restores
  nothing else — ``bounce`` is not a reversible suppression reason.

The tests below are written against the *outcome* (is this contact still
mailable?) rather than the returned label, because a classifier that returns
``"blocked"`` while the caller suppresses anyway is the bug intact.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.models.suppression import SuppressionEntry
from app.services.bounce_handler import BOUNCE_KINDS, classify_bounce, record_bounce
from app.services.suppression import is_suppressed

# Verbatim shapes from the four providers that reject most B2B cold email.
GMAIL_SPAM = (
    "550",
    "5.7.1 [203.0.113.9] Our system has detected that this message is "
    "likely unsolicited mail. To reduce the amount of spam sent to Gmail, "
    "this message has been blocked.",
)
M365_DMARC = (
    "550 5.7.509",
    "Access denied, sending domain acme-outreach.com does not pass DMARC "
    "verification and has a DMARC policy of reject",
)
M365_BANNED_IP = (
    "550 5.7.606",
    "Access denied, banned sending IP [203.0.113.9]",
)
SPAMHAUS = (
    "554",
    "5.7.1 Service unavailable; Client host [203.0.113.9] blocked using "
    "zen.spamhaus.org",
)
DEAD_MAILBOX = ("550", "5.1.1 The email account that you tried to reach does not exist")

POLICY_BLOCKS = [
    pytest.param(*GMAIL_SPAM, id="gmail-spam-verdict"),
    pytest.param(*M365_DMARC, id="m365-dmarc-reject"),
    pytest.param(*M365_BANNED_IP, id="m365-banned-ip"),
    pytest.param(*SPAMHAUS, id="spamhaus-listing"),
]


async def _company(db, name="Acme Mfg", domain=None):
    company = Company(
        id=uuid.uuid4(), name=name, source="test", country="US", domain=domain
    )
    db.add(company)
    await db.flush()
    return company


async def _contact(db, company, email):
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, email=email,
        first_name="Dana", source="test",
    )
    db.add(contact)
    await db.flush()
    return contact


async def _enroll(db, contact):
    seq = OutreachSequence(id=uuid.uuid4(), name=f"Seq-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active"
    )
    db.add(enr)
    await db.flush()
    return enr


async def _entries(db, email):
    return (
        await db.execute(
            select(SuppressionEntry).where(SuppressionEntry.email == email)
        )
    ).scalars().all()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code,diagnostic", POLICY_BLOCKS)
def test_a_policy_rejection_is_not_a_hard_bounce(code, diagnostic):
    assert classify_bounce(status_code=code, diagnostic=diagnostic) == "blocked"


def test_a_dead_mailbox_is_still_hard():
    """The regression that matters in the other direction.

    Loosening the classifier until real dead addresses stop being suppressed
    would leave them in rotation, earning hard bounces against our own
    reputation — the failure the whole module was built to stop.
    """
    assert classify_bounce(*DEAD_MAILBOX) == "hard"


def test_an_explicit_dead_recipient_outranks_a_stray_policy_word():
    """"User unknown … mail blocked" is evidence about the mailbox.

    ``blocked`` is in the policy phrase list and ``user unknown`` is direct
    evidence, so the ordering has to prefer the specific one. This is why the
    hard-phrase check sits ahead of the loose policy match and behind the
    enhanced ``5.7.x`` code, which is unambiguous on its own.
    """
    assert classify_bounce("550", "5.1.1 User unknown; mail blocked") == "hard"


def test_a_transient_policy_refusal_stays_soft():
    """``4.7.x`` is greylisting, not a reputation alarm.

    It suppresses nothing today and clears on its own, so promoting it to
    ``blocked`` would buy nothing and would blunt a signal an operator reads as
    "our sending reputation is in trouble right now".
    """
    assert classify_bounce("451", "4.7.1 Greylisted, please try again later") == "soft"


def test_ambiguous_input_still_defaults_to_soft():
    assert classify_bounce(None, "") == "soft"
    assert classify_bounce("", "something went wrong") == "soft"


def test_every_classification_is_a_declared_kind():
    """``BOUNCE_KINDS`` is what migration 113's CHECK constraint permits.

    A classifier returning a fourth string would insert fine under SQLite and
    fail the constraint in production, on the bounce path, where the exception
    is swallowed per-message.
    """
    cases = [*POLICY_BLOCKS, pytest.param(*DEAD_MAILBOX)]
    for case in cases:
        assert classify_bounce(*case.values) in BOUNCE_KINDS


# ---------------------------------------------------------------------------
# What record_bounce does with it — the half that actually costs money
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("code,diagnostic", POLICY_BLOCKS)
async def test_a_policy_block_leaves_the_recipient_mailable(db_session, code, diagnostic):
    company = await _company(db_session)
    contact = await _contact(db_session, company, "live@acme.com")
    enrollment = await _enroll(db_session, contact)

    result = await record_bounce(
        db_session, contact.email, status_code=code, diagnostic=diagnostic
    )

    await db_session.refresh(contact)
    await db_session.refresh(enrollment)

    assert result["kind"] == "blocked"
    assert result["suppressed"] == 0
    assert contact.suppressed is not True
    assert contact.unsubscribed is not True
    assert enrollment.status == "active"
    assert await _entries(db_session, contact.email) == []
    assert not await is_suppressed(db_session, contact.email)


@pytest.mark.asyncio
async def test_a_policy_block_does_not_move_the_soft_bounce_counter(db_session):
    """``bounce_count`` is the counter ``soft_bounce_suppress_threshold`` reads.

    Incrementing it would put the whole failure back on a timer: the fifth
    blocked send to a perfectly live contact would suppress them anyway, and by
    then nothing connects the suppression to the DMARC record that caused it.
    """
    company = await _company(db_session)
    contact = await _contact(db_session, company, "live@acme.com")

    for _ in range(10):
        await record_bounce(
            db_session, contact.email,
            status_code=GMAIL_SPAM[0], diagnostic=GMAIL_SPAM[1],
        )

    await db_session.refresh(contact)
    assert (contact.bounce_count or 0) == 0
    assert contact.suppressed is not True


@pytest.mark.asyncio
async def test_a_policy_block_is_still_recorded_for_the_operator(db_session):
    """Inert is not invisible. An operator has to be able to see what happened.

    ``last_bounce_type`` is how the deliverability report tells "this mailbox is
    dead" apart from "this mailbox is fine and our domain is on fire", and they
    call for opposite responses.
    """
    company = await _company(db_session)
    contact = await _contact(db_session, company, "live@acme.com")

    await record_bounce(
        db_session, contact.email,
        status_code=M365_DMARC[0], diagnostic=M365_DMARC[1],
    )

    await db_session.refresh(contact)
    assert contact.last_bounce_type == "blocked"
    assert contact.last_bounced_at is not None


@pytest.mark.asyncio
async def test_a_policy_block_never_writes_off_the_domain(db_session):
    """The generalising step, and the one that costs a whole company.

    Threshold is 1 here so a single rejection would be enough if ``blocked``
    reached the write-off at all. The colleague has never bounced and was never
    named by the rejection.
    """
    from app.config import get_settings

    settings = get_settings()
    original = settings.dead_domain_bounce_threshold
    settings.dead_domain_bounce_threshold = 1
    try:
        company = await _company(db_session, name="Acme", domain="acme.com")
        rejected = await _contact(db_session, company, "dana@acme.com")
        colleague = await _contact(db_session, company, "sam@acme.com")

        result = await record_bounce(
            db_session, rejected.email,
            status_code=GMAIL_SPAM[0], diagnostic=GMAIL_SPAM[1],
        )

        await db_session.refresh(colleague)
        assert result["domain_written_off"] is None
        assert colleague.suppressed is not True
        assert not await is_suppressed(db_session, colleague.email)
    finally:
        settings.dead_domain_bounce_threshold = original


@pytest.mark.asyncio
async def test_a_real_hard_bounce_still_suppresses_everything(db_session):
    """The control. Without it these tests pass on a handler that does nothing."""
    company = await _company(db_session)
    contact = await _contact(db_session, company, "dead@acme.com")
    enrollment = await _enroll(db_session, contact)

    result = await record_bounce(
        db_session, contact.email,
        status_code=DEAD_MAILBOX[0], diagnostic=DEAD_MAILBOX[1],
    )

    await db_session.refresh(contact)
    await db_session.refresh(enrollment)

    assert result["kind"] == "hard"
    assert contact.suppressed is True
    assert enrollment.status == "bounced"
    assert await is_suppressed(db_session, contact.email)


@pytest.mark.asyncio
async def test_a_forced_kind_of_blocked_is_honoured(db_session):
    """``kind=`` is how the ingestion sweep passes its own classification through.

    It ledgers the kind on ``BounceRecord`` *before* calling the handler, so the
    two must agree or the audit row describes a decision that wasn't taken.
    """
    company = await _company(db_session)
    contact = await _contact(db_session, company, "live@acme.com")

    result = await record_bounce(db_session, contact.email, kind="blocked")

    await db_session.refresh(contact)
    assert result["kind"] == "blocked"
    assert contact.suppressed is not True


@pytest.mark.asyncio
async def test_a_policy_block_for_an_unknown_address_lists_nothing(db_session):
    """A hard bounce with no contact row still lists the address. A block must not.

    That branch exists because a DSN is proof we sent there — but a policy
    rejection is proof of nothing about the address, and the suppression list is
    the durable half that outlives every contact row.
    """
    result = await record_bounce(
        db_session, "stranger@example.com",
        status_code=SPAMHAUS[0], diagnostic=SPAMHAUS[1],
    )

    assert result["kind"] == "blocked"
    assert result["list_entry_added"] is False
    assert await _entries(db_session, "stranger@example.com") == []
