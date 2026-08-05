"""Bounce forensics and per-recipient-domain deliverability.

The gap these cover: every deliverability surface in the codebase measures the
**sending** side (sender health, warm-up, SPF/DKIM/DMARC, per-mailbox bounce
rates), and blocks happen on the **receiving** side. A provider that starts
rejecting us does not move a blended bounce rate far enough to trip any alarm,
because the healthy domains hold the blend down — which is the scenario
``test_a_blocked_domain_hides_inside_a_healthy_blend`` pins.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.bounce_record import BounceRecord
from app.models.company import Company
from app.models.contact import Contact
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.models.suppression import SuppressionEntry
from app.services import bounce_analytics


async def _company(db, name="Acme Mfg"):
    company = Company(id=uuid.uuid4(), name=name, source="test", country="US")
    db.add(company)
    await db.flush()
    return company


async def _contact(db, company, email, first_name="Dana"):
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, email=email,
        first_name=first_name, source="test",
    )
    db.add(contact)
    await db.flush()
    return contact


async def _sequence(db, name="Seq"):
    seq = OutreachSequence(id=uuid.uuid4(), name=f"{name}-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    return seq


async def _enroll(db, contact, sequence):
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id,
        current_step=1, status="active",
    )
    db.add(enr)
    await db.flush()
    return enr


async def _events(db, enrollment, event_type, n=1, when=None):
    for _ in range(n):
        db.add(OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
            event_type=event_type,
            occurred_at=when or datetime.now(timezone.utc),
            event_metadata={},
        ))
    await db.flush()


async def _attempts(db, contact, sequence, *, sent, bounced=0, when=None):
    """``sent`` attempts for one address, ``bounced`` of which failed.

    One enrollment per send, because an attempt is ``(enrollment_id,
    step_number)`` — the send-claim interlock makes one ``sent`` event per pair
    the invariant, and ``outreach_events.AttemptTally`` counts bounces as
    *distinct attempts* so a redelivered DSN is free rather than a second
    bounce. Stacking a window onto one enrollment asks for a shape production
    cannot produce, and the arithmetic reads it correctly as one attempt that
    bounced N times.
    """
    when = when or datetime.now(timezone.utc)
    for i in range(sent):
        enr = OutreachEnrollment(
            id=uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id,
            current_step=1, status="active",
        )
        db.add(enr)
        db.add(OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enr.id, step_number=1,
            event_type="sent", occurred_at=when, event_metadata={},
        ))
        if i < bounced:
            db.add(OutreachEvent(
                id=uuid.uuid4(), enrollment_id=enr.id, step_number=1,
                event_type="bounced", occurred_at=when, event_metadata={},
            ))
    await db.flush()


async def _bounce_record(db, email, kind="hard", when=None, account_id=None,
                         diagnostic=None):
    record = BounceRecord(
        id=uuid.uuid4(),
        gmail_message_id=f"msg-{uuid.uuid4().hex}",
        gmail_account_id=account_id,
        recipient_email=email.lower(),
        bounce_kind=kind,
        status_code="5.1.1" if kind == "hard" else "4.2.2",
        diagnostic=diagnostic,
        result={},
        processed_at=when or datetime.now(timezone.utc),
    )
    db.add(record)
    await db.flush()
    return record


# --- email_domain ------------------------------------------------------------

def test_email_domain_parsing():
    assert bounce_analytics.email_domain("Dana@Acme.COM") == "acme.com"
    assert bounce_analytics.email_domain(" lead@sub.acme.com ") == "sub.acme.com"
    assert bounce_analytics.email_domain("") is None
    assert bounce_analytics.email_domain(None) is None
    # No ``@`` at all: rpartition puts everything in the tail, which would be a
    # silently wrong domain. Guarded because bounce ledgers do carry junk.
    assert bounce_analytics.email_domain("notanemail") == "notanemail"


# --- summary -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_empty(db_session):
    result = await bounce_analytics.summary(db_session, days=30)
    assert result["total"] == 0
    assert result["bounce_rate"] == 0.0
    assert result["daily"] == []


@pytest.mark.asyncio
async def test_summary_counts_kinds_and_rate(db_session):
    company = await _company(db_session)
    contact = await _contact(db_session, company, "lead@acme.com")
    seq = await _sequence(db_session)
    enr = await _enroll(db_session, contact, seq)
    await _events(db_session, enr, "sent", n=100)

    for i in range(3):
        await _bounce_record(db_session, f"h{i}@acme.com", kind="hard")
    await _bounce_record(db_session, "s0@acme.com", kind="soft")

    result = await bounce_analytics.summary(db_session, days=30)
    assert result["hard"] == 3
    assert result["soft"] == 1
    assert result["total"] == 4
    assert result["sent"] == 100
    assert result["bounce_rate"] == 4.0
    assert result["hard_bounce_rate"] == 3.0


@pytest.mark.asyncio
async def test_summary_counts_addresses_not_notifications(db_session):
    """A mailbox that soft-bounces daily is one bad address, not seven."""
    for _ in range(7):
        await _bounce_record(db_session, "full@acme.com", kind="soft")

    result = await bounce_analytics.summary(db_session, days=30)
    assert result["total"] == 7
    assert result["distinct_addresses"] == 1


@pytest.mark.asyncio
async def test_summary_reports_the_suppression_shortfall(db_session):
    """``auto_suppressed`` should track the hard bounces. A gap is the failure
    mode worth seeing: addresses bouncing without leaving rotation."""
    await _bounce_record(db_session, "a@acme.com", kind="hard")
    await _bounce_record(db_session, "b@acme.com", kind="hard")
    db_session.add(SuppressionEntry(
        id=uuid.uuid4(), email="a@acme.com", reason="bounce", source="auto"
    ))
    await db_session.flush()

    result = await bounce_analytics.summary(db_session, days=30)
    assert result["hard"] == 2
    assert result["auto_suppressed"] == 1


@pytest.mark.asyncio
async def test_summary_excludes_bounces_outside_the_window(db_session):
    await _bounce_record(db_session, "old@acme.com", kind="hard",
                         when=datetime.now(timezone.utc) - timedelta(days=45))
    await _bounce_record(db_session, "new@acme.com", kind="hard")

    result = await bounce_analytics.summary(db_session, days=30)
    assert result["total"] == 1


@pytest.mark.asyncio
async def test_summary_attributes_bounces_to_the_sending_mailbox(db_session):
    """Concentrated on one mailbox is a sender problem; spread evenly is a list
    problem. An aggregate cannot tell them apart."""
    account = GmailAccount(
        id=uuid.uuid4(), email="rep@apprend.tech",
        google_sub=f"sub-{uuid.uuid4().hex}",
        refresh_token_encrypted="enc", access_token_encrypted="enc",
    )
    db_session.add(account)
    await db_session.flush()

    await _bounce_record(db_session, "a@acme.com", account_id=account.id)
    await _bounce_record(db_session, "b@acme.com", account_id=account.id)
    await _bounce_record(db_session, "c@acme.com")  # no account linked

    result = await bounce_analytics.summary(db_session, days=30)
    by_sender = {row["email"]: row["bounces"] for row in result["by_sender"]}
    assert by_sender["rep@apprend.tech"] == 2
    # An unlinked bounce is reported against an unnamed sender, not dropped.
    assert by_sender[None] == 1


@pytest.mark.asyncio
async def test_summary_groups_diagnostics_on_the_first_line(db_session):
    """The tail of a DSN carries the recipient and a message id, which would
    split one refusal reason into as many strings as there were bounces."""
    for i in range(3):
        await _bounce_record(
            db_session, f"x{i}@acme.com",
            diagnostic=f"550 5.1.1 User unknown\nFinal-Recipient: x{i}@acme.com",
        )

    result = await bounce_analytics.summary(db_session, days=30)
    assert result["top_diagnostics"][0]["diagnostic"] == "550 5.1.1 User unknown"
    assert result["top_diagnostics"][0]["count"] == 3


@pytest.mark.asyncio
async def test_summary_daily_series_is_ordered_and_split(db_session):
    today = datetime.now(timezone.utc)
    await _bounce_record(db_session, "a@acme.com", kind="hard", when=today)
    await _bounce_record(db_session, "b@acme.com", kind="soft", when=today)
    await _bounce_record(db_session, "c@acme.com", kind="hard",
                         when=today - timedelta(days=2))

    result = await bounce_analytics.summary(db_session, days=30)
    daily = result["daily"]
    assert len(daily) == 2
    assert daily[0]["day"] < daily[1]["day"]
    assert daily[-1] == {
        "day": daily[-1]["day"], "hard": 1, "soft": 1, "blocked": 0, "total": 2,
    }


# --- by_recipient_domain -----------------------------------------------------

@pytest.mark.asyncio
async def test_domains_empty(db_session):
    result = await bounce_analytics.by_recipient_domain(db_session, days=30)
    assert result["domains"] == []
    assert result["total_sent"] == 0


@pytest.mark.asyncio
async def test_domain_rates_and_contact_counts(db_session):
    company = await _company(db_session)
    seq = await _sequence(db_session)
    for i in range(2):
        contact = await _contact(db_session, company, f"lead{i}@acme.com")
        enr = await _enroll(db_session, contact, seq)
        await _events(db_session, enr, "sent", n=25)
        await _events(db_session, enr, "replied", n=1)

    result = await bounce_analytics.by_recipient_domain(db_session, days=30)
    acme = next(d for d in result["domains"] if d["domain"] == "acme.com")
    assert acme["sent"] == 50
    # Two addresses, not two sends — "12 bounces" reads very differently across
    # 12 addresses than across one retried twelve times.
    assert acme["contacts"] == 2
    assert acme["replied"] == 2
    assert acme["reply_rate"] == 4.0
    assert result["total_sent"] == 50


@pytest.mark.asyncio
async def test_a_thin_domain_gets_no_verdict(db_session):
    """Two sends and one bounce is not a 50% bounce rate."""
    company = await _company(db_session)
    contact = await _contact(db_session, company, "lead@tiny.com")
    seq = await _sequence(db_session)
    enr = await _enroll(db_session, contact, seq)
    await _events(db_session, enr, "sent", n=2)
    await _events(db_session, enr, "bounced", n=1)

    result = await bounce_analytics.by_recipient_domain(db_session, days=30)
    tiny = next(d for d in result["domains"] if d["domain"] == "tiny.com")
    assert tiny["bounce_rate"] == 50.0  # reported...
    assert tiny["sufficient_sample"] is False
    assert tiny["risk"] == "insufficient_data"  # ...but never a verdict
    assert "tiny.com" not in result["flagged"]


@pytest.mark.asyncio
async def test_a_blocked_domain_hides_inside_a_healthy_blend(db_session):
    """The scenario the whole endpoint exists for.

    One provider rejects everything we send it; the rest are fine. The blended
    bounce rate lands around 4% — under the 5% kill-switch threshold and under
    every per-sender alarm — while one domain is at 100%.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)

    healthy = await _contact(db_session, company, "ok@gmail.com")
    await _attempts(db_session, healthy, seq, sent=1000)

    blocked = await _contact(db_session, company, "nope@outlook.com")
    await _attempts(db_session, blocked, seq, sent=40, bounced=40)

    result = await bounce_analytics.by_recipient_domain(db_session, days=30)

    blended = 40 / 1040
    assert blended < 0.05  # would not trip the global threshold

    assert result["flagged"] == ["outlook.com"]
    assert result["domains"][0]["domain"] == "outlook.com"  # riskiest first
    assert result["domains"][0]["risk"] == "blocked"
    assert result["sent_to_flagged_domains"] == 40
    healthy_row = next(d for d in result["domains"] if d["domain"] == "gmail.com")
    assert healthy_row["risk"] == "healthy"


@pytest.mark.asyncio
async def test_at_risk_band_sits_between_healthy_and_blocked(db_session):
    company = await _company(db_session)
    seq = await _sequence(db_session)
    contact = await _contact(db_session, company, "lead@wobbly.com")
    await _attempts(db_session, contact, seq, sent=100, bounced=10)  # 10%

    result = await bounce_analytics.by_recipient_domain(db_session, days=30)
    row = next(d for d in result["domains"] if d["domain"] == "wobbly.com")
    assert row["risk"] == "at_risk"


@pytest.mark.asyncio
async def test_domain_hard_soft_split_comes_from_the_ledger(db_session):
    """``OutreachEvent`` records *that* a send bounced, never what kind. The
    classification only exists in the bounce ledger."""
    company = await _company(db_session)
    seq = await _sequence(db_session)
    contact = await _contact(db_session, company, "lead@acme.com")
    enr = await _enroll(db_session, contact, seq)
    await _events(db_session, enr, "sent", n=30)
    await _events(db_session, enr, "bounced", n=3)
    await _bounce_record(db_session, "lead@acme.com", kind="hard")
    await _bounce_record(db_session, "other@acme.com", kind="soft")

    result = await bounce_analytics.by_recipient_domain(db_session, days=30)
    row = next(d for d in result["domains"] if d["domain"] == "acme.com")
    assert row["hard_bounces"] == 1
    assert row["soft_bounces"] == 1


@pytest.mark.asyncio
async def test_min_sends_is_caller_adjustable(db_session):
    company = await _company(db_session)
    seq = await _sequence(db_session)
    contact = await _contact(db_session, company, "lead@small.com")
    enr = await _enroll(db_session, contact, seq)
    await _events(db_session, enr, "sent", n=5)
    await _events(db_session, enr, "bounced", n=5)

    default = await bounce_analytics.by_recipient_domain(db_session, days=30)
    assert default["domains"][0]["risk"] == "insufficient_data"

    lowered = await bounce_analytics.by_recipient_domain(
        db_session, days=30, min_sends=5
    )
    assert lowered["domains"][0]["risk"] == "blocked"


# --- recent ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recent_resolves_the_contact_and_suppression_state(db_session):
    company = await _company(db_session)
    await _contact(db_session, company, "known@acme.com", first_name="Dana")
    await _bounce_record(db_session, "known@acme.com", kind="hard")

    result = await bounce_analytics.recent(db_session, days=30)
    assert result["total"] == 1
    row = result["bounces"][0]
    assert row["contact_name"] == "Dana"
    assert row["domain"] == "acme.com"
    # The actionable case: hard-bounced and still mailable.
    assert row["suppressed"] is False


@pytest.mark.asyncio
async def test_recent_reads_suppression_from_the_list_too(db_session):
    """An address can be out of rotation with no contact row at all."""
    await _bounce_record(db_session, "listed@acme.com", kind="hard")
    db_session.add(SuppressionEntry(
        id=uuid.uuid4(), email="listed@acme.com", reason="bounce", source="auto"
    ))
    await db_session.flush()

    result = await bounce_analytics.recent(db_session, days=30)
    assert result["bounces"][0]["contact_id"] is None
    assert result["bounces"][0]["suppressed"] is True


@pytest.mark.asyncio
async def test_recent_does_not_fan_out_on_duplicate_contacts(db_session):
    """A re-import creates a second contact row for the same mailbox. Joining on
    email would emit one page row per contact — a page holding fewer bounces
    than it claims, against a ``total`` that counted each once."""
    company = await _company(db_session)
    await _contact(db_session, company, "dupe@acme.com", first_name="Old")
    await _contact(db_session, await _company(db_session, "Acme (dup)"),
                   "dupe@acme.com", first_name="New")
    await _bounce_record(db_session, "dupe@acme.com", kind="hard")

    result = await bounce_analytics.recent(db_session, days=30)
    assert result["total"] == 1
    assert len(result["bounces"]) == 1
    # The newest row wins — it is the one a send path would most likely pick up.
    assert result["bounces"][0]["contact_name"] == "New"


@pytest.mark.asyncio
async def test_recent_domain_filter_is_a_suffix_not_a_substring(db_session):
    await _bounce_record(db_session, "a@acme.com")
    await _bounce_record(db_session, "b@notacme.com")
    await _bounce_record(db_session, "acme.com@other.com")

    result = await bounce_analytics.recent(db_session, days=30, domain="acme.com")
    assert [r["recipient_email"] for r in result["bounces"]] == ["a@acme.com"]


@pytest.mark.asyncio
async def test_recent_filters_by_kind_and_paginates(db_session):
    for i in range(5):
        await _bounce_record(db_session, f"h{i}@acme.com", kind="hard")
    await _bounce_record(db_session, "s@acme.com", kind="soft")

    hard = await bounce_analytics.recent(db_session, days=30, kind="hard")
    assert hard["total"] == 5

    page = await bounce_analytics.recent(db_session, days=30, kind="hard",
                                         limit=2, offset=2)
    # ``total`` describes the filter, not the page — a count that shrank as you
    # paged would be worse than none.
    assert page["total"] == 5
    assert len(page["bounces"]) == 2


# --- endpoints ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_bounce_endpoints_are_wired(client, db_session):
    company = await _company(db_session)
    contact = await _contact(db_session, company, "lead@acme.com")
    seq = await _sequence(db_session)
    await _attempts(db_session, contact, seq, sent=40, bounced=12)
    await _bounce_record(db_session, "lead@acme.com", kind="hard")
    await db_session.commit()

    summary = await client.get("/api/v1/dialtone/bounces/summary?days=30")
    assert summary.status_code == 200, summary.text
    assert summary.json()["hard"] == 1

    domains = await client.get("/api/v1/dialtone/deliverability/domains?days=30")
    assert domains.status_code == 200, domains.text
    assert domains.json()["flagged"] == ["acme.com"]

    listing = await client.get("/api/v1/dialtone/bounces?days=30&kind=hard")
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 1


@pytest.mark.asyncio
async def test_bounce_list_rejects_an_unknown_kind(client):
    resp = await client.get("/api/v1/dialtone/bounces?kind=medium")
    assert resp.status_code == 422


# --- The receiving domain that refused everything ----------------------------
#
# This report's whole premise is that the domain rejecting *everything* is
# invisible in a blended rate. It had a blind spot of exactly that shape.
#
# A **synchronous** rejection writes a ``bounced`` event with no ``sent``
# partner — the transport refused the message, so nothing was ever accepted.
# Divided by ``count(sent)`` a domain that refused every message has a
# denominator of zero, and ``_rate`` answers 0.0 rather than dividing. Worse,
# ``sufficient = sent >= min_sends`` was then False, so it took the
# ``insufficient_data`` verdict and sorted to the bottom of a list ordered by
# risk. ``risk`` is not a display value — ``blocked`` is what tells an operator
# to stop mailing a domain.

@pytest.mark.asyncio
async def test_a_domain_that_refused_every_message_is_blocked_not_unknown(db_session):
    company = await _company(db_session)
    seq = await _sequence(db_session)
    contact = await _contact(db_session, company, "lead@fortress.com")
    # Refused on the wire: a bounce event and no send event, 30 times.
    for _ in range(30):
        enr = await _enroll(db_session, contact, seq)
        await _events(db_session, enr, "bounced", n=1)

    result = await bounce_analytics.by_recipient_domain(db_session, days=30)
    row = next(d for d in result["domains"] if d["domain"] == "fortress.com")

    assert row["attempts"] == 30
    assert row["sent"] == 0, "nothing was ever accepted — that is the finding"
    assert row["bounced"] == 30
    assert row["bounce_rate"] == 100.0
    assert row["sufficient_sample"] is True
    assert row["risk"] == "blocked"
    assert "fortress.com" in result["flagged"]


@pytest.mark.asyncio
async def test_that_domain_sorts_above_the_healthy_ones(db_session):
    """Risk-first ordering only helps if the risky domain gets a verdict."""
    company = await _company(db_session)
    seq = await _sequence(db_session)
    fine = await _contact(db_session, company, "ok@gmail.com")
    await _attempts(db_session, fine, seq, sent=500)
    refused = await _contact(db_session, company, "lead@fortress.com")
    for _ in range(30):
        enr = await _enroll(db_session, refused, seq)
        await _events(db_session, enr, "bounced", n=1)

    result = await bounce_analytics.by_recipient_domain(db_session, days=30)
    assert result["domains"][0]["domain"] == "fortress.com", (
        f"risk-first ordering put {result['domains'][0]['domain']} above the "
        "domain refusing every message"
    )


@pytest.mark.asyncio
async def test_a_retried_dsn_does_not_move_a_domains_verdict(db_session):
    """The numerator half: a mailer-daemon retrying is one bounce.

    Six deliveries of one DSN against 40 attempts is 2.5%, which is healthy.
    Counted as rows it is 15% — an ``at_risk`` verdict on a domain that refused
    one message.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    contact = await _contact(db_session, company, "lead@retry.com")
    await _attempts(db_session, contact, seq, sent=39)
    enr = await _enroll(db_session, contact, seq)
    await _events(db_session, enr, "sent", n=1)
    await _events(db_session, enr, "bounced", n=6)

    result = await bounce_analytics.by_recipient_domain(db_session, days=30)
    row = next(d for d in result["domains"] if d["domain"] == "retry.com")
    assert row["attempts"] == 40
    assert row["bounced"] == 1
    assert row["bounce_rate"] == 2.5
    assert row["risk"] == "healthy"


@pytest.mark.asyncio
async def test_an_async_bounce_is_not_a_second_attempt(db_session):
    """The boundary in the other direction.

    The ordinary bounce is asynchronous — the message was accepted and the DSN
    arrived hours later — so the pair is one attempt. Counting two would
    understate every bounce rate, which is the mirror of the bug being fixed.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    contact = await _contact(db_session, company, "lead@async.com")
    await _attempts(db_session, contact, seq, sent=20, bounced=2)

    result = await bounce_analytics.by_recipient_domain(db_session, days=30)
    row = next(d for d in result["domains"] if d["domain"] == "async.com")
    assert row["attempts"] == 20, "20 attempts, not 22"
    assert row["bounced"] == 2
    assert row["bounce_rate"] == 10.0


@pytest.mark.asyncio
async def test_a_refused_address_still_counts_as_a_contact(db_session):
    """"12 bounces" is unreadable against zero contacts.

    Contacts were counted from ``sent`` groups, so an address that only ever
    failed synchronously was never counted at its domain at all.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    for i in range(3):
        contact = await _contact(db_session, company, f"user{i}@fortress.com")
        for _ in range(10):
            enr = await _enroll(db_session, contact, seq)
            await _events(db_session, enr, "bounced", n=1)

    result = await bounce_analytics.by_recipient_domain(db_session, days=30)
    row = next(d for d in result["domains"] if d["domain"] == "fortress.com")
    assert row["contacts"] == 3
    assert row["attempts"] == 30
