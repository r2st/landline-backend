"""What we sent a contact, and what a queued message is not.

``contact_engagement`` is the roll-up behind ``GET /contacts/{id}/engagement`` —
the panel that answers "have we emailed this person, and did they care". It
reads both send tables (CLAUDE.md, "Sent mail lives in two tables") and
denominates every rate in sends, so the definition of "a send" is the number
every other number on the panel divides by.

That definition was a hand-written literal listing six of the seven non-wire
statuses, missing ``pending`` — the status an ``EmailSend`` is created in and
sits in until a transport accepts it. A message composed and not yet sent
therefore appeared on the timeline with a timestamp, was counted in ``sent``,
and divided into the open and reply rates. ``NON_WIRE_STATUSES`` is on the model
precisely so this vocabulary lives in one place; the tests below are driven off
it rather than off a second literal, so a status added later is covered here
without anyone remembering to come back.

The module had no test file at all before this one.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import NON_WIRE_STATUSES, WIRE_STATUSES, EmailSend
from app.services.contact_engagement import contact_engagement

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def _send(contact: Contact, *, status: str, **kwargs) -> EmailSend:
    return EmailSend(
        id=uuid.uuid4(),
        contact_id=contact.id,
        to_email=contact.email,
        subject="Quick question",
        status=status,
        sent_at=NOW - timedelta(days=1),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_a_pending_send_is_not_a_send(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """The one the drifted literal let through.

    ``pending`` is where every row starts. Counting it says we emailed someone
    we have only composed to — on the panel an operator reads before picking up
    the phone.
    """
    db_session.add(_send(sample_contact, status="pending"))
    await db_session.flush()

    result = await contact_engagement(db_session, sample_contact.id)

    assert result["sent"] == 0
    assert result["events"] == []
    assert result["first_touch_at"] is None


@pytest.mark.parametrize("status", sorted(NON_WIRE_STATUSES))
@pytest.mark.asyncio
async def test_nothing_that_stayed_home_counts(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    status: str,
):
    """Driven off the model's list, so a new status is covered by construction."""
    db_session.add(_send(sample_contact, status=status))
    await db_session.flush()

    result = await contact_engagement(db_session, sample_contact.id)

    assert result["sent"] == 0, f"{status!r} counted as a send"
    assert result["events"] == []


@pytest.mark.parametrize("status", [s for s in WIRE_STATUSES if s != "bounced"])
@pytest.mark.asyncio
async def test_everything_that_reached_the_wire_counts(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    status: str,
):
    """The other half, and the reason this can't just be ``status == 'sent'``.

    ``EmailSend.status`` is a lifecycle column: the tracking router advances it
    up ``sent → opened → clicked → replied``. A send the recipient opened is
    still a send, and a roll-up that only counted ``sent`` would shrink as the
    campaign worked.
    """
    db_session.add(_send(sample_contact, status=status))
    await db_session.flush()

    result = await contact_engagement(db_session, sample_contact.id)

    assert result["sent"] == 1, f"{status!r} was not counted as a send"


@pytest.mark.asyncio
async def test_a_bounce_is_a_bounce_and_not_a_send(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    db_session.add(_send(sample_contact, status="bounced"))
    await db_session.flush()

    result = await contact_engagement(db_session, sample_contact.id)

    assert result["bounced"] == 1
    assert result["sent"] == 0


@pytest.mark.asyncio
async def test_rates_are_not_diluted_by_unsent_mail(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """The consequence, stated as the number an operator actually looks at.

    One send, opened. Three more composed and never sent. With ``pending``
    counted the open rate reads 25% — a contact who engaged with everything we
    sent them, presented as one who ignored three quarters of it.
    """
    db_session.add(_send(
        sample_contact, status="opened", opened_at=NOW - timedelta(hours=2),
    ))
    for _ in range(3):
        db_session.add(_send(sample_contact, status="pending"))
    await db_session.flush()

    result = await contact_engagement(db_session, sample_contact.id)

    assert result["sent"] == 1
    assert result["opened"] == 1
    assert result["open_rate"] == 1.0
