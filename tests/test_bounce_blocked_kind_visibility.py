"""The third bounce kind existed everywhere except the surfaces that report it.

``blocked`` — a spam verdict, a DMARC/SPF failure, our sending IP on a blocklist
— was added as a first-class classification precisely because treating it as a
hard bounce burned the recipient list for something the recipient had no part
in. ``BOUNCE_KINDS`` names it, the ``ck_bounce_kind`` CHECK (migration 113)
accepts it, ``classify_bounce`` returns it and ``record_bounce`` gives it its own
inert path.

Every *read* surface still knew about two kinds:

* ``summary`` computed ``total = hard + soft``. A policy incident produces
  nothing but blocked records, and produces them by the hundred, so the bounce
  dashboard read **zero** during the one event it exists for — while
  ``distinct_addresses`` right beside it, which counts every row, spiked.
* ``by_recipient_domain`` folded blocked into ``soft_bounces`` via a two-way
  ``hard if kind == "hard" else soft``. Blocked is the one kind that arrives as
  a cluster on a *single receiving domain*, because that is exactly what a DMARC
  break looks like — so the surface built to name the domain refusing our mail
  described the refusal as transient trouble at the recipient's end.
* ``recent(kind="blocked")`` matched no branch, applied no filter, and returned
  the whole ledger. A filter that silently does nothing reads as "there is
  nothing else to see".

These pin all three, plus the router pattern that made ``kind=blocked`` a 422.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.bounce_record import BounceRecord
from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services import bounce_analytics


async def _bounce(db, email, kind, when=None):
    db.add(BounceRecord(
        id=uuid.uuid4(),
        gmail_message_id=f"msg-{uuid.uuid4().hex}",
        recipient_email=email.lower(),
        bounce_kind=kind,
        status_code="5.7.1" if kind == "blocked" else "5.1.1",
        diagnostic=None,
        result={},
        processed_at=when or datetime.now(timezone.utc),
    ))
    await db.flush()


async def _sends(db, email, *, sent, bounced):
    """``sent``/``bounced`` sequence-engine events for one address.

    One enrollment per send. An attempt is ``(enrollment_id, step_number)`` —
    the send-claim interlock makes one ``sent`` event per pair the invariant —
    and ``outreach_events.AttemptTally`` counts bounces as *distinct attempts*
    so a redelivered DSN is free rather than a second bounce.

    Stacking the whole window onto one enrollment at step 1 asked for a shape
    production cannot produce, and the arithmetic reads it correctly as one
    attempt that bounced, redelivered N-1 times.
    """
    company = Company(id=uuid.uuid4(), name=f"Co {uuid.uuid4().hex[:6]}", source="test")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, email=email,
        first_name="Dana", source="test",
    )
    db.add(contact)
    sequence = OutreachSequence(id=uuid.uuid4(), name=f"Seq-{uuid.uuid4().hex[:6]}")
    db.add(sequence)
    await db.flush()
    now = datetime.now(timezone.utc)
    for i in range(sent):
        enrollment = OutreachEnrollment(
            id=uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id,
            current_step=1, status="active",
        )
        db.add(enrollment)
        db.add(OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
            event_type="sent", occurred_at=now, event_metadata={},
        ))
        if i < bounced:
            db.add(OutreachEvent(
                id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
                event_type="bounced", occurred_at=now, event_metadata={},
            ))
    await db.flush()


# --- summary -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_counts_policy_blocks_in_the_total(db_session):
    """A pure-policy incident must not read as zero bounces.

    Three blocked records and nothing else is what a broken DMARC record looks
    like on a Tuesday afternoon. ``total = hard + soft`` reported 0.
    """
    for i in range(3):
        await _bounce(db_session, f"live{i}@acme.com", "blocked")

    result = await bounce_analytics.summary(db_session, days=30)

    assert result["blocked"] == 3
    assert result["total"] == 3
    # The ledger and the address count agree, which is what makes the documented
    # "auto_suppressed should track the addresses" comparison meaningful.
    assert result["distinct_addresses"] == 3


@pytest.mark.asyncio
async def test_summary_keeps_blocked_out_of_the_hard_bounce_rate(db_session):
    """``hard_bounce_rate`` asks how many addresses are dead. None of these are.

    This is the half that must *not* change: the whole reason ``blocked`` is its
    own kind is that a policy rejection is no evidence about the recipient's
    mailbox. It belongs in the overall rate (the message did come back, and the
    kill switch's own rail counts it) and nowhere near the dead-address one.
    """
    await _sends(db_session, "a@acme.com", sent=100, bounced=0)
    await _bounce(db_session, "a@acme.com", "blocked")
    await _bounce(db_session, "b@acme.com", "blocked")
    await _bounce(db_session, "c@acme.com", "hard")

    result = await bounce_analytics.summary(db_session, days=30)

    assert result["sent"] == 100
    assert result["hard"] == 1
    assert result["blocked"] == 2
    assert result["hard_bounce_rate"] == 1.0        # the one dead address
    assert result["bounce_rate"] == 3.0             # everything that came back


@pytest.mark.asyncio
async def test_summary_daily_series_breaks_out_blocked(db_session):
    """The trend line summed every kind while the total above it summed two.

    ``daily[*]["total"]`` has always been ``sum(counts.values())``, so a blocked
    record moved the chart and not the headline — two numbers on one page,
    derived from one query, disagreeing.
    """
    now = datetime.now(timezone.utc)
    await _bounce(db_session, "a@acme.com", "hard", when=now)
    await _bounce(db_session, "b@acme.com", "blocked", when=now)

    result = await bounce_analytics.summary(db_session, days=30)
    day = result["daily"][-1]

    assert day["hard"] == 1
    assert day["blocked"] == 1
    assert day["total"] == 2 == result["total"]


# --- by_recipient_domain -----------------------------------------------------

@pytest.mark.asyncio
async def test_domain_report_does_not_file_policy_blocks_as_soft(db_session):
    """A filter verdict at one provider is not that provider having a bad day.

    The two-way fold made ``soft_bounces`` the bucket for everything that wasn't
    hard, so a domain-wide block — the clustered failure this whole report was
    built to surface — arrived labelled as transient mailbox trouble.
    """
    await _sends(db_session, "dana@outlook.com", sent=40, bounced=12)
    for i in range(12):
        await _bounce(db_session, f"user{i}@outlook.com", "blocked")

    result = await bounce_analytics.by_recipient_domain(
        db_session, days=30, min_sends=20
    )
    outlook = next(d for d in result["domains"] if d["domain"] == "outlook.com")

    assert outlook["blocked_bounces"] == 12
    assert outlook["soft_bounces"] == 0
    assert outlook["hard_bounces"] == 0
    # The event-derived rate is untouched: a blocked send still bounced, so the
    # risk verdict is unchanged. Only the *attribution* was wrong.
    assert outlook["bounce_rate"] == 30.0
    assert outlook["risk"] == "blocked"


@pytest.mark.asyncio
async def test_domain_report_still_separates_hard_from_soft(db_session):
    """The three-way split must not have collapsed the original two."""
    await _sends(db_session, "dana@acme.com", sent=40, bounced=4)
    await _bounce(db_session, "a@acme.com", "hard")
    await _bounce(db_session, "b@acme.com", "soft")
    await _bounce(db_session, "c@acme.com", "soft")

    result = await bounce_analytics.by_recipient_domain(
        db_session, days=30, min_sends=20
    )
    acme = next(d for d in result["domains"] if d["domain"] == "acme.com")

    assert (acme["hard_bounces"], acme["soft_bounces"], acme["blocked_bounces"]) == (1, 2, 0)


# --- recent ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recent_filters_to_blocked_instead_of_ignoring_the_filter(db_session):
    """``kind="blocked"`` returned the unfiltered ledger.

    Worse than an empty page: the operator asked "show me the policy blocks",
    got every bounce we have, and had no way to tell the filter hadn't run.
    """
    await _bounce(db_session, "hard@acme.com", "hard")
    await _bounce(db_session, "soft@acme.com", "soft")
    await _bounce(db_session, "blocked1@acme.com", "blocked")
    await _bounce(db_session, "blocked2@acme.com", "blocked")

    page = await bounce_analytics.recent(db_session, days=30, kind="blocked")

    assert page["total"] == 2
    assert {b["recipient_email"] for b in page["bounces"]} == {
        "blocked1@acme.com", "blocked2@acme.com",
    }


@pytest.mark.asyncio
async def test_recent_ignores_a_kind_that_is_not_a_kind(db_session):
    """An unknown value still means "no filter", not an error or an empty page."""
    await _bounce(db_session, "hard@acme.com", "hard")
    await _bounce(db_session, "soft@acme.com", "soft")

    page = await bounce_analytics.recent(db_session, days=30, kind="nonsense")

    assert page["total"] == 2


@pytest.mark.asyncio
async def test_recent_excludes_blocked_from_the_hard_page(db_session):
    """The existing filters keep meaning what they meant."""
    await _bounce(db_session, "dead@acme.com", "hard")
    await _bounce(db_session, "policy@acme.com", "blocked")

    page = await bounce_analytics.recent(db_session, days=30, kind="hard")

    assert page["total"] == 1
    assert page["bounces"][0]["recipient_email"] == "dead@acme.com"


@pytest.mark.asyncio
async def test_bounces_endpoint_accepts_the_blocked_kind(client):
    """The route pattern predated the third kind, so asking for it was a 422."""
    response = await client.get("/api/v1/dialtone/bounces?kind=blocked")
    assert response.status_code == 200

    # And an actual nonsense value is still refused, rather than the pattern
    # having been loosened into a rubber stamp.
    bad = await client.get("/api/v1/dialtone/bounces?kind=nonsense")
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_blocked_outside_the_window_is_excluded_like_any_other(db_session):
    """Window handling is shared; pinned so the new kind isn't special-cased."""
    now = datetime.now(timezone.utc)
    await _bounce(db_session, "old@acme.com", "blocked", when=now - timedelta(days=45))
    await _bounce(db_session, "new@acme.com", "blocked", when=now)

    result = await bounce_analytics.summary(db_session, days=30)

    assert result["blocked"] == 1
    assert result["total"] == 1
