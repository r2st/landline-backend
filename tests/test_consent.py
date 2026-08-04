"""Tests for CASL / CAN-SPAM consent service."""

import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.consent import ConsentRecord
from app.services.consent import check_consent, record_consent, revoke_consent


@pytest.mark.asyncio
async def test_us_can_send_by_default(
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    """CAN-SPAM: US contacts can be emailed without prior consent."""
    result = await check_consent(db_session, sample_contact, sample_company)
    assert result.can_email is True
    assert result.regime == "CAN-SPAM"


@pytest.mark.asyncio
async def test_us_blocked_after_revocation(
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    """CAN-SPAM: US contacts cannot be emailed after opting out."""
    record = ConsentRecord(
        contact_id=sample_contact.id,
        consent_type="express",
        basis="Opted in via website",
        granted_at=datetime.now(timezone.utc) - timedelta(days=30),
        revoked_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(record)
    await db_session.flush()

    result = await check_consent(db_session, sample_contact, sample_company)
    assert result.can_email is False
    assert "revoked" in result.reason.lower()


@pytest.mark.asyncio
async def test_ca_blocked_without_consent(
    db_session: AsyncSession,
    canadian_company: Company,
    canadian_contact: Contact,
):
    """CASL: Canadian contacts CANNOT be emailed without consent."""
    result = await check_consent(db_session, canadian_contact, canadian_company)
    assert result.can_email is False
    assert result.regime == "CASL"
    assert "opt-in" in result.reason.lower() or "No consent" in result.reason


@pytest.mark.asyncio
async def test_ca_allowed_with_express_consent(
    db_session: AsyncSession,
    canadian_company: Company,
    canadian_contact: Contact,
):
    """CASL: Canadian contacts CAN be emailed with express consent."""
    record = ConsentRecord(
        contact_id=canadian_contact.id,
        consent_type="express",
        basis="Signed consent form at trade show",
        granted_at=datetime.now(timezone.utc) - timedelta(days=10),
    )
    db_session.add(record)
    await db_session.flush()

    result = await check_consent(db_session, canadian_contact, canadian_company)
    assert result.can_email is True
    assert result.regime == "CASL"
    assert result.consent_type == "express"


@pytest.mark.asyncio
async def test_ca_allowed_with_implied_consent_not_expired(
    db_session: AsyncSession,
    canadian_company: Company,
    canadian_contact: Contact,
):
    """CASL: Implied consent is valid if not expired."""
    record = ConsentRecord(
        contact_id=canadian_contact.id,
        consent_type="implied",
        basis="Existing business relationship",
        granted_at=datetime.now(timezone.utc) - timedelta(days=30),
        expires_at=datetime.now(timezone.utc) + timedelta(days=700),
    )
    db_session.add(record)
    await db_session.flush()

    result = await check_consent(db_session, canadian_contact, canadian_company)
    assert result.can_email is True
    assert result.consent_type == "implied"


@pytest.mark.asyncio
async def test_ca_blocked_with_expired_implied_consent(
    db_session: AsyncSession,
    canadian_company: Company,
    canadian_contact: Contact,
):
    """CASL: Expired implied consent should block sending."""
    record = ConsentRecord(
        contact_id=canadian_contact.id,
        consent_type="implied",
        basis="Trade show meeting",
        granted_at=datetime.now(timezone.utc) - timedelta(days=800),
        expires_at=datetime.now(timezone.utc) - timedelta(days=10),
    )
    db_session.add(record)
    await db_session.flush()

    result = await check_consent(db_session, canadian_contact, canadian_company)
    assert result.can_email is False


@pytest.mark.asyncio
async def test_ca_blocked_with_revoked_express_consent(
    db_session: AsyncSession,
    canadian_company: Company,
    canadian_contact: Contact,
):
    """CASL: Revoked express consent should block sending."""
    record = ConsentRecord(
        contact_id=canadian_contact.id,
        consent_type="express",
        basis="Website form",
        granted_at=datetime.now(timezone.utc) - timedelta(days=60),
        revoked_at=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db_session.add(record)
    await db_session.flush()

    result = await check_consent(db_session, canadian_contact, canadian_company)
    assert result.can_email is False


@pytest.mark.asyncio
async def test_record_consent(
    db_session: AsyncSession,
    sample_contact: Contact,
):
    """Test creating a consent record."""
    record = await record_consent(
        db_session,
        contact_id=sample_contact.id,
        consent_type="express",
        basis="Email opt-in form",
    )
    assert record.consent_type == "express"
    assert record.contact_id == sample_contact.id
    assert record.revoked_at is None


@pytest.mark.asyncio
async def test_revoke_consent(
    db_session: AsyncSession,
    sample_contact: Contact,
):
    """Test revoking a consent record."""
    record = await record_consent(
        db_session,
        contact_id=sample_contact.id,
        consent_type="express",
        basis="Test",
    )
    revoked = await revoke_consent(db_session, record.id)
    assert revoked.revoked_at is not None


@pytest.mark.asyncio
async def test_revoke_nonexistent_consent(db_session: AsyncSession):
    """Test that revoking a non-existent record raises ValueError."""
    import uuid
    with pytest.raises(ValueError, match="not found"):
        await revoke_consent(db_session, uuid.uuid4())


# --- Every supported market has a regime -----------------------------------
#
# ``check_consent`` runs at the wire, and a refusal there *pauses the
# enrollment* (``tasks.outreach.send_sequence_step``) — permanently, since
# nothing re-examines a paused enrollment's consent. So a country the gate
# doesn't recognise is not a soft degradation; it is the whole market's outreach
# silently dying at the last hop, one contact at a time, after sourcing,
# enrichment and enrollment all succeeded.
#
# That is what happened: the schema allowlist grew to nine countries and the
# gate still branched on the ``US``/``CA`` literals, so GB, IN, AU, DE, FR, SG
# and AE each answered "Unknown jurisdiction: XX" and never sent.

import uuid

from app.constants import (
    ALLOWED_COUNTRIES,
    COUNTRY_CONSENT_REGIMES,
    OPT_IN,
    OPT_OUT,
)
from app.services.consent import regime_for


def test_every_allowed_country_has_a_regime():
    """The drift guard. Widening ``ALLOWED_COUNTRIES`` without adding a regime
    here is indistinguishable from working code until mail stops going out."""
    missing = [c for c in ALLOWED_COUNTRIES if c not in COUNTRY_CONSENT_REGIMES]
    assert not missing, (
        f"{missing} are insertable but have no consent regime — every send to "
        f"them will refuse and pause the enrollment"
    )


def test_the_regime_table_claims_no_country_the_schema_rejects():
    """The other direction: a regime for a country no row can hold is dead
    config that reads as coverage."""
    extra = [c for c in COUNTRY_CONSENT_REGIMES if c not in ALLOWED_COUNTRIES]
    assert not extra, f"{extra} have regimes but fail the companies CHECK constraint"


def test_every_regime_declares_a_model_the_gate_implements():
    for country, (name, model) in COUNTRY_CONSENT_REGIMES.items():
        assert model in (OPT_IN, OPT_OUT), f"{country} has unknown model {model!r}"
        assert name and name != "unknown", f"{country} has no regime label"


@pytest_asyncio.fixture
async def company_in(db_session: AsyncSession):
    """A company in an arbitrary supported country."""

    async def _make(country: str) -> Company:
        company = Company(
            id=uuid.uuid4(),
            name=f"Global Stamping {country}",
            domain=f"stamping-{country.lower()}.example",
            country=country,
            source="test",
        )
        db_session.add(company)
        await db_session.flush()
        return company

    return _make


@pytest_asyncio.fixture
async def contact_at(db_session: AsyncSession):
    async def _make(company: Company) -> Contact:
        contact = Contact(
            id=uuid.uuid4(),
            company_id=company.id,
            first_name="Ada",
            last_name="Byron",
            email=f"ada@{company.domain}",
            source="test",
        )
        db_session.add(contact)
        await db_session.flush()
        return contact

    return _make


@pytest.mark.parametrize("country", sorted(ALLOWED_COUNTRIES))
@pytest.mark.asyncio
async def test_no_supported_country_is_an_unknown_jurisdiction(
    db_session: AsyncSession, company_in, contact_at, country
):
    """The regression. Every allowed country must reach a real regime — whether
    it then permits the send is the regime's business, but 'unknown' is not an
    answer any supported market may get."""
    company = await company_in(country)
    contact = await contact_at(company)

    result = await check_consent(db_session, contact, company)

    assert result.regime != "unknown", f"{country} fell through to no regime"
    assert "Unknown jurisdiction" not in result.reason


@pytest.mark.parametrize(
    "country",
    sorted(c for c, (_, m) in COUNTRY_CONSENT_REGIMES.items() if m == OPT_OUT),
)
@pytest.mark.asyncio
async def test_opt_out_markets_can_send_cold(
    db_session: AsyncSession, company_in, contact_at, country
):
    """An opt-out regime permits the first touch — that is what makes it
    opt-out. Before the fix only ``US`` did, and GB/IN/FR/SG/AE were dead."""
    company = await company_in(country)
    contact = await contact_at(company)

    result = await check_consent(db_session, contact, company)

    assert result.can_email is True, result.reason
    assert result.regime == COUNTRY_CONSENT_REGIMES[country][0]


@pytest.mark.parametrize(
    "country",
    sorted(c for c, (_, m) in COUNTRY_CONSENT_REGIMES.items() if m == OPT_IN),
)
@pytest.mark.asyncio
async def test_opt_in_markets_refuse_without_a_consent_record(
    db_session: AsyncSession, company_in, contact_at, country
):
    """Widening the gate must not loosen the strict markets. DE and AU join CA
    on the opt-in side and refuse a cold send for the *stated* reason — not as
    an unrecognised country, which is the same verdict for the wrong cause."""
    company = await company_in(country)
    contact = await contact_at(company)

    result = await check_consent(db_session, contact, company)

    assert result.can_email is False
    assert "requires opt-in" in result.reason
    assert result.regime == COUNTRY_CONSENT_REGIMES[country][0]


@pytest.mark.parametrize(
    "country",
    sorted(c for c, (_, m) in COUNTRY_CONSENT_REGIMES.items() if m == OPT_IN),
)
@pytest.mark.asyncio
async def test_opt_in_markets_send_with_express_consent(
    db_session: AsyncSession, company_in, contact_at, country
):
    company = await company_in(country)
    contact = await contact_at(company)
    db_session.add(
        ConsentRecord(
            contact_id=contact.id,
            consent_type="express",
            basis="Signed at trade show",
            granted_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
    )
    await db_session.flush()

    result = await check_consent(db_session, contact, company)

    assert result.can_email is True, result.reason
    assert result.consent_type == "express"


@pytest.mark.asyncio
async def test_suppression_outranks_every_regime(
    db_session: AsyncSession, company_in, contact_at
):
    """The compliance floor is not a regime question, and the refusal still
    names the jurisdiction that applied — an operator reading the pause reason
    needs both halves."""
    company = await company_in("GB")
    contact = await contact_at(company)
    contact.suppressed = True
    await db_session.flush()

    result = await check_consent(db_session, contact, company)

    assert result.can_email is False
    assert result.regime == "PECR"
    assert "suppressed" in result.reason.lower()


@pytest.mark.asyncio
async def test_an_unrecognised_country_still_fails_closed(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """The fail-closed default is the point of the gate and must survive the
    widening. ``ZZ`` can't reach the database (the CHECK constraint refuses it),
    so this pins the in-memory branch directly."""
    sample_company.country = "ZZ"

    result = await check_consent(db_session, sample_contact, sample_company)

    assert result.can_email is False
    assert result.regime == "unknown"


def test_regime_lookup_tolerates_casing_and_padding():
    """``country`` is written by scrapers and enrichment, not only by our own
    schema-validated inserts."""
    assert regime_for("gb") == COUNTRY_CONSENT_REGIMES["GB"]
    assert regime_for(" DE ") == COUNTRY_CONSENT_REGIMES["DE"]
    assert regime_for(None) is None
    assert regime_for("") is None
