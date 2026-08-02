"""``_auto_enroll`` routes to the industry's sequence and skips unemailable contacts.

Two bugs lived here. The sequence lookup took the first active sequence with no
ordering and no industry filter, so a CNC Machining lead could be pitched
injection molding copy. And the contact lookup ignored ``email``, so a contact
nobody can email got an enrollment that would never send — a lead that looks
worked without a single touch.

Routing runs for real against the ``industry_verticals`` registry; only the DB
rows are fixtures, so a change to the keyword matcher or the vertical keys fails
here rather than in production.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.services.autonomous_prospector import get_prospector

pytestmark = pytest.mark.asyncio

CNC_SEQUENCE_NAME = "CNC Machining — Deal Cracker"


async def _seq(db: AsyncSession, name: str, industry: str | None, **kw) -> OutreachSequence:
    seq = OutreachSequence(
        id=uuid.uuid4(),
        name=name,
        industry=industry,
        is_active=kw.get("is_active", True),
        status=kw.get("status", "active"),
    )
    db.add(seq)
    await db.flush()
    return seq


async def _company(db: AsyncSession, industry_segment: str | None, name="Precision Parts Inc") -> Company:
    company = Company(
        id=uuid.uuid4(),
        name=name,
        # companies.domain is unique — keep each fixture company distinct.
        domain=f"{uuid.uuid4().hex[:10]}.example.com",
        country="CA",
        industry_segment=industry_segment,
        source="test",
    )
    db.add(company)
    await db.flush()
    return company


async def _contact(db: AsyncSession, company: Company, email: str | None, is_primary=True) -> Contact:
    contact = Contact(
        id=uuid.uuid4(),
        company_id=company.id,
        first_name="Dale",
        last_name="Toolpath",
        email=email,
        is_primary=is_primary,
    )
    db.add(contact)
    await db.flush()
    return contact


async def _enrollment_for(db: AsyncSession, contact_id) -> OutreachEnrollment | None:
    return (
        await db.execute(
            select(OutreachEnrollment).where(OutreachEnrollment.contact_id == contact_id)
        )
    ).scalars().first()


@pytest_asyncio.fixture
async def verticals(db_session):
    """The active sequence set, mirroring production's tagged verticals.

    Injection molding is created *first* so an unordered ``.first()`` — the old
    behaviour — picks it. That is what makes the CNC assertions meaningful.
    """
    return {
        "molding": await _seq(db_session, "Plastic Injection Molding — Deal Cracker", "injection_molding"),
        "cnc": await _seq(db_session, CNC_SEQUENCE_NAME, "cnc_machining"),
        "stamping": await _seq(db_session, "Metal Stamping & Forming — Deal Cracker", "metal_stamping"),
    }


# ── sequence routing ────────────────────────────────────────────────────────


async def test_cnc_company_gets_the_cnc_sequence(db_session, verticals):
    """The headline bug: 42 CNC leads were in line for injection molding copy."""
    company = await _company(db_session, "CNC Machining")
    contact = await _contact(db_session, company, "dale@precisionparts.example.com")

    assert await get_prospector()._auto_enroll(db_session, company) is True

    enrollment = await _enrollment_for(db_session, contact.id)
    assert enrollment is not None
    assert enrollment.sequence_id == verticals["cnc"].id
    assert enrollment.sequence_id != verticals["molding"].id


async def test_each_vertical_routes_to_its_own_sequence(db_session, verticals):
    """Routing is per-industry, not "whichever row came back first"."""
    for segment, key in [
        ("CNC Machining", "cnc"),
        ("Injection Molding", "molding"),
        ("Metal Stamping", "stamping"),
    ]:
        company = await _company(db_session, segment, name=f"{segment} Co")
        contact = await _contact(db_session, company, f"owner@{key}.example.com")

        assert await get_prospector()._auto_enroll(db_session, company) is True

        enrollment = await _enrollment_for(db_session, contact.id)
        assert enrollment.sequence_id == verticals[key].id, segment


async def test_unknown_industry_falls_back_to_an_active_sequence(db_session, verticals):
    """No vertical matches — still enroll, rather than dropping the lead."""
    company = await _company(db_session, "Artisanal Cheese Caves")
    contact = await _contact(db_session, company, "owner@cheese.example.com")

    assert await get_prospector()._auto_enroll(db_session, company) is True

    enrollment = await _enrollment_for(db_session, contact.id)
    assert enrollment is not None


async def test_null_industry_segment_falls_back(db_session, verticals):
    """Places candidates can land with no industry at all."""
    company = await _company(db_session, None, name="Unlabelled Shop")
    contact = await _contact(db_session, company, "owner@unlabelled.example.com")

    assert await get_prospector()._auto_enroll(db_session, company) is True
    assert await _enrollment_for(db_session, contact.id) is not None


async def test_company_name_routes_when_industry_is_missing(db_session, verticals):
    """resolve_vertical reads the name too — a shop that says what it does."""
    company = await _company(db_session, None, name="Toronto CNC Machining Ltd")
    contact = await _contact(db_session, company, "owner@toronto.example.com")

    await get_prospector()._auto_enroll(db_session, company)

    enrollment = await _enrollment_for(db_session, contact.id)
    assert enrollment.sequence_id == verticals["cnc"].id


async def test_paused_vertical_sequence_is_not_enrolled_into(db_session):
    """A matching-but-paused sequence must not be picked; fall back to an active one."""
    await _seq(db_session, CNC_SEQUENCE_NAME, "cnc_machining", is_active=False, status="paused")
    generic = await _seq(db_session, "Generic Outreach", None)
    company = await _company(db_session, "CNC Machining")
    contact = await _contact(db_session, company, "dale@precisionparts.example.com")

    assert await get_prospector()._auto_enroll(db_session, company) is True

    enrollment = await _enrollment_for(db_session, contact.id)
    assert enrollment.sequence_id == generic.id


async def test_no_sequences_at_all_still_advances_the_stage(db_session):
    company = await _company(db_session, "CNC Machining")
    await _contact(db_session, company, "dale@precisionparts.example.com")

    assert await get_prospector()._auto_enroll(db_session, company) is False
    assert company.pipeline_stage == "outreach"


# ── contacts that can actually be emailed ───────────────────────────────────


async def test_contact_without_email_is_not_enrolled(db_session, verticals):
    """An enrollment for a contact with no email can never send."""
    company = await _company(db_session, "CNC Machining")
    contact = await _contact(db_session, company, None)

    assert await get_prospector()._auto_enroll(db_session, company) is False
    assert await _enrollment_for(db_session, contact.id) is None
    # The company still advances — the call queue can work it by phone.
    assert company.pipeline_stage == "outreach"


async def test_empty_string_email_is_not_enrolled(db_session, verticals):
    """Scrapers write '' as readily as NULL."""
    company = await _company(db_session, "CNC Machining")
    contact = await _contact(db_session, company, "   ")

    assert await get_prospector()._auto_enroll(db_session, company) is False
    assert await _enrollment_for(db_session, contact.id) is None


async def test_emailable_contact_wins_over_primary_without_email(db_session, verticals):
    """Primary ordering must not drag an unemailable contact to the front."""
    company = await _company(db_session, "CNC Machining")
    primary = await _contact(db_session, company, None, is_primary=True)
    secondary = await _contact(db_session, company, "real@precisionparts.example.com", is_primary=False)

    assert await get_prospector()._auto_enroll(db_session, company) is True

    assert await _enrollment_for(db_session, primary.id) is None
    enrollment = await _enrollment_for(db_session, secondary.id)
    assert enrollment is not None
    assert enrollment.sequence_id == verticals["cnc"].id


async def test_primary_still_preferred_among_emailable_contacts(db_session, verticals):
    company = await _company(db_session, "CNC Machining")
    secondary = await _contact(db_session, company, "second@precisionparts.example.com", is_primary=False)
    primary = await _contact(db_session, company, "primary@precisionparts.example.com", is_primary=True)

    await get_prospector()._auto_enroll(db_session, company)

    assert await _enrollment_for(db_session, primary.id) is not None
    assert await _enrollment_for(db_session, secondary.id) is None


async def test_enrolling_twice_does_not_duplicate(db_session, verticals):
    company = await _company(db_session, "CNC Machining")
    contact = await _contact(db_session, company, "dale@precisionparts.example.com")

    assert await get_prospector()._auto_enroll(db_session, company) is True
    assert await get_prospector()._auto_enroll(db_session, company) is False

    rows = (
        await db_session.execute(
            select(OutreachEnrollment).where(OutreachEnrollment.contact_id == contact.id)
        )
    ).scalars().all()
    assert len(rows) == 1


# ── contacts we are not allowed to email ────────────────────────────────────
#
# The send path re-checks and would stop these enrollments as ``unsubscribed``
# on their first pass, so this is not what stops the mail. What it stops is the
# row: an enrollment we will never send lands in ``total_enrollments`` and in the
# conversion denominator, quietly diluting every campaign rate it touches. And
# discovery walks the same company repeatedly, so without this it re-enrols the
# same dead mailbox into whichever sequence it picks each time.


async def test_suppressed_contact_is_not_enrolled(db_session, verticals):
    company = await _company(db_session, "CNC Machining")
    contact = await _contact(db_session, company, "dead@precisionparts.example.com")
    contact.suppressed = True
    await db_session.flush()

    assert await get_prospector()._auto_enroll(db_session, company) is False
    assert await _enrollment_for(db_session, contact.id) is None
    # The company still advances — the call queue can work it by phone.
    assert company.pipeline_stage == "outreach"


async def test_unsubscribed_contact_is_not_enrolled(db_session, verticals):
    company = await _company(db_session, "CNC Machining")
    contact = await _contact(db_session, company, "optout@precisionparts.example.com")
    contact.unsubscribed = True
    await db_session.flush()

    assert await get_prospector()._auto_enroll(db_session, company) is False
    assert await _enrollment_for(db_session, contact.id) is None


async def test_an_address_on_the_do_not_contact_list_is_not_enrolled(
    db_session, verticals
):
    """The list is keyed by address, which is what catches a *re-imported*
    contact for a mailbox that bounced under a previous row — the new row's own
    flags are clean."""
    from app.services.suppression import add_entry

    company = await _company(db_session, "CNC Machining")
    contact = await _contact(db_session, company, "listed@precisionparts.example.com")
    await add_entry(db_session, contact.email, reason="bounce", source="auto")

    assert contact.suppressed is False  # the row itself looks fine
    assert await get_prospector()._auto_enroll(db_session, company) is False
    assert await _enrollment_for(db_session, contact.id) is None


async def test_a_mailable_contact_wins_over_a_suppressed_primary(db_session, verticals):
    """Primary ordering must not drag a suppressed contact to the front, for the
    same reason it must not drag one with no email."""
    company = await _company(db_session, "CNC Machining")
    primary = await _contact(db_session, company, "dead@precisionparts.example.com",
                             is_primary=True)
    primary.suppressed = True
    secondary = await _contact(db_session, company, "live@precisionparts.example.com",
                               is_primary=False)
    await db_session.flush()

    assert await get_prospector()._auto_enroll(db_session, company) is True
    assert await _enrollment_for(db_session, primary.id) is None
    assert await _enrollment_for(db_session, secondary.id) is not None


async def test_a_bounce_stops_a_re_imported_contact_being_enrolled(db_session, verticals):
    """End to end, the compounding failure this closes.

    Discovery mails an address, it hard-bounces, and a later run re-imports the
    same company — creating a **new** contact row for the same mailbox. The
    (contact, sequence) dedup cannot see it, because the contact id is different;
    the new row's own ``suppressed`` flag is False, because nothing ever set it.
    Only the address-level list remembers, and this is the assertion that it is
    consulted.
    """
    from app.services.bounce_handler import record_bounce

    company = await _company(db_session, "CNC Machining")
    original = await _contact(db_session, company, "dale@precisionparts.example.com")

    assert await get_prospector()._auto_enroll(db_session, company) is True
    await record_bounce(db_session, original.email, status_code="5.1.1")

    # The re-import: same address, fresh row, clean flags.
    reimported = await _contact(
        db_session, company, "dale@precisionparts.example.com", is_primary=False
    )
    assert reimported.suppressed is False

    assert await get_prospector()._auto_enroll(db_session, company) is False
    assert await _enrollment_for(db_session, reimported.id) is None
