"""The send gate asks all three do-not-contact questions, not one.

``email_dispatch.check_send_allowed`` is the compliance floor for every path
that is **not** the sequence engine — the smart-reply auto-send, the meeting
agent's four sends, the inbox reply composer, the per-account test send, and
``send_outreach_email`` itself (manual sends, template blasts). It consulted the
kill switch and the org suppression LIST.

It did not read the two per-contact flags. ``send_sequence_step`` does, and its
own comment says why: three writers have set ``Contact.unsubscribed`` alone at
various times, so rows carrying the flag with no list entry are in the database
now — which is the entire reason that redundant read exists.

So an opt-out recorded by one of those older writers stopped the cadence and
stopped nothing else. Same shape as the gate's origin story one level down: the
floor was consolidated out of the callers, and one of its clauses stayed behind.

Every test here plants exactly that state — a flag with **no** ``SuppressionEntry``
— because a contact whose opt-out went through ``opt_out.record`` has a list
entry and was already refused. The gap is only visible where the two disagree.
"""

import uuid

import pytest

from app.models.company import Company
from app.models.contact import Contact
from app.models.suppression import SuppressionEntry
from app.services import email_dispatch, suppression


async def _contact(db_session, email: str, **flags) -> Contact:
    company = Company(id=uuid.uuid4(), name="Acme", country="US", source="test")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, email=email,
        first_name="Test", last_name="Lead", **flags,
    )
    db_session.add(contact)
    await db_session.commit()
    return contact


# --- The gate ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_unsubscribed_flag_blocks_the_send(db_session):
    """``unsubscribed=True`` with no list entry — the footer-link legacy state."""
    await _contact(db_session, "legacy@example.com", unsubscribed=True)

    blocked = await email_dispatch.check_send_allowed(db_session, "legacy@example.com")

    assert blocked is not None, "a contact who unsubscribed was still mailable"
    assert blocked["status"] == "suppressed"
    assert "unsubscribed" in blocked["reason"].lower()


@pytest.mark.asyncio
async def test_suppressed_flag_blocks_the_send(db_session):
    await _contact(db_session, "flagged@example.com", suppressed=True)

    blocked = await email_dispatch.check_send_allowed(db_session, "flagged@example.com")

    assert blocked is not None
    assert blocked["status"] == "suppressed"


@pytest.mark.asyncio
async def test_clean_contact_still_passes(db_session):
    """The control: the gate must not start refusing ordinary recipients."""
    await _contact(db_session, "fine@example.com")

    assert await email_dispatch.check_send_allowed(db_session, "fine@example.com") is None


@pytest.mark.asyncio
async def test_address_with_no_contact_row_still_passes(db_session):
    """A bulk import may name an address we hold no row for; that is not a block."""
    assert await email_dispatch.check_send_allowed(db_session, "stranger@example.com") is None


@pytest.mark.asyncio
async def test_list_entry_still_blocks(db_session):
    """The clause that already worked keeps working."""
    from app.org_context import get_current_org_id, APPREND_ORG_ID

    db_session.add(
        SuppressionEntry(
            org_id=get_current_org_id() or APPREND_ORG_ID,
            email="listed@example.com",
            reason="complaint",
            source="manual",
        )
    )
    await db_session.commit()

    blocked = await email_dispatch.check_send_allowed(db_session, "listed@example.com")
    assert blocked is not None
    assert blocked["status"] == "suppressed"


@pytest.mark.asyncio
async def test_flag_is_matched_case_insensitively(db_session):
    """The To: header casing is not the casing the row was stored with."""
    await _contact(db_session, "mixedcase@example.com", unsubscribed=True)

    blocked = await email_dispatch.check_send_allowed(db_session, "MixedCase@Example.COM")
    assert blocked is not None


@pytest.mark.asyncio
async def test_one_flagged_duplicate_is_enough(db_session):
    """``contacts.email`` has no unique constraint and the scraper re-creates rows.

    The flag is a fact about the person. Which of their duplicate rows a send
    path happened to load is not, so any flagged row refuses the address.
    """
    await _contact(db_session, "dupe@example.com")
    await _contact(db_session, "dupe@example.com", unsubscribed=True)

    blocked = await email_dispatch.check_send_allowed(db_session, "dupe@example.com")
    assert blocked is not None


@pytest.mark.asyncio
async def test_kill_switch_bypass_does_not_lift_the_flags(db_session):
    """Suppression is stricter than the operational pause, for all three clauses."""
    await _contact(db_session, "bypass@example.com", unsubscribed=True)

    blocked = await email_dispatch.check_send_allowed(
        db_session, "bypass@example.com", bypass_kill_switch=True
    )
    assert blocked is not None

    blocked = await email_dispatch.check_send_allowed(
        db_session, "bypass@example.com", check_kill_switch=False
    )
    assert blocked is not None


# --- The resolver ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_address_blocking_reason_names_the_clause(db_session):
    """Operator-facing: which clause refused, not a bare False."""
    await _contact(db_session, "why@example.com", unsubscribed=True)
    assert (
        await suppression.address_blocking_reason(db_session, "why@example.com")
        == suppression.UNSUBSCRIBED
    )


@pytest.mark.asyncio
async def test_address_blocking_reason_ignores_malformed_addresses(db_session):
    """Deciding an address is malformed is the transport's job, not this gate's."""
    assert await suppression.address_blocking_reason(db_session, "not-an-address") is None
    assert await suppression.address_blocking_reason(db_session, None) is None
    assert await suppression.address_blocking_reason(db_session, "") is None


# --- The paths that only ever had this gate --------------------------------------


@pytest.mark.asyncio
async def test_send_outreach_email_refuses_a_legacy_unsubscriber(db_session, monkeypatch):
    """End to end: nothing reaches a transport for a flag-only opt-out."""
    await _contact(db_session, "e2e@example.com", unsubscribed=True)

    sent = []

    class _FakeSes:
        sender = "outbound@example.com"

        def send_email(self, **kwargs):
            sent.append(kwargs)
            return {"status": "sent", "message_id": "x"}

    monkeypatch.setattr(email_dispatch, "EmailSender", lambda: _FakeSes())

    async def _no_account(db):
        return None

    monkeypatch.setattr(email_dispatch, "get_active_account", _no_account)
    monkeypatch.setattr(email_dispatch, "get_next_rotation_account", _no_account)

    result = await email_dispatch.send_outreach_email(
        db_session,
        to_email="e2e@example.com",
        subject="Quick question",
        body_html="<p>Hi</p>",
        skip_spam_scan=True,
    )

    assert result["status"] == "suppressed"
    assert sent == [], "a suppressed address reached the transport"
