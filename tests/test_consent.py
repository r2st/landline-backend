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
