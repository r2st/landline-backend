"""CASL / CAN-SPAM consent verification service."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.consent import ConsentRecord

logger = logging.getLogger(__name__)


class ConsentCheckResult:
    """Result of a consent check for a contact."""

    def __init__(
        self,
        can_email: bool,
        reason: str,
        regime: str,
        consent_type: str = "none",
    ):
        self.can_email = can_email
        self.reason = reason
        self.regime = regime
        self.consent_type = consent_type

    def to_dict(self) -> dict:
        return {
            "can_email": self.can_email,
            "reason": self.reason,
            "regime": self.regime,
            "consent_type": self.consent_type,
        }


async def check_consent(
    db: AsyncSession,
    contact: Contact,
    company: Company,
) -> ConsentCheckResult:
    """Check whether we can email a contact based on their country's regulations.

    CASL (Canada): Must have express or valid implied consent BEFORE sending.
    CAN-SPAM (US): Can send without prior consent, but must include unsubscribe.
    """
    country = company.country

    if country == "US":
        # CAN-SPAM: opt-out model — check they haven't unsubscribed
        result = await db.execute(
            select(ConsentRecord)
            .where(ConsentRecord.contact_id == contact.id)
            .where(ConsentRecord.consent_type == "none")
            .where(ConsentRecord.revoked_at.is_not(None))
            .order_by(ConsentRecord.created_at.desc())
            .limit(1)
        )
        unsubscribed = result.scalar_one_or_none()

        if unsubscribed:
            return ConsentCheckResult(
                can_email=False,
                reason="Contact has unsubscribed (CAN-SPAM opt-out)",
                regime="CAN-SPAM",
                consent_type="none",
            )

        # Also check for explicit revocation
        result = await db.execute(
            select(ConsentRecord)
            .where(ConsentRecord.contact_id == contact.id)
            .where(ConsentRecord.revoked_at.is_not(None))
            .order_by(ConsentRecord.revoked_at.desc())
            .limit(1)
        )
        revoked = result.scalar_one_or_none()
        if revoked:
            return ConsentCheckResult(
                can_email=False,
                reason="Contact has revoked consent",
                regime="CAN-SPAM",
                consent_type="none",
            )

        return ConsentCheckResult(
            can_email=True,
            reason="CAN-SPAM allows sending with unsubscribe option",
            regime="CAN-SPAM",
            consent_type="implied",
        )

    elif country == "CA":
        # CASL: opt-in model — must have valid express or implied consent
        now = datetime.now(timezone.utc)

        result = await db.execute(
            select(ConsentRecord)
            .where(ConsentRecord.contact_id == contact.id)
            .where(ConsentRecord.revoked_at.is_(None))
            .order_by(ConsentRecord.created_at.desc())
        )
        records = list(result.scalars().all())

        if not records:
            return ConsentCheckResult(
                can_email=False,
                reason="No consent record found — CASL requires opt-in before sending",
                regime="CASL",
                consent_type="none",
            )

        # Check for valid express consent (no expiry, or not yet expired)
        for record in records:
            if record.consent_type == "express":
                if record.expires_at is None or record.expires_at > now:
                    return ConsentCheckResult(
                        can_email=True,
                        reason=f"Express consent on record (basis: {record.basis or 'N/A'})",
                        regime="CASL",
                        consent_type="express",
                    )

        # Check for valid implied consent (must not be expired)
        for record in records:
            if record.consent_type == "implied":
                if record.expires_at is not None and record.expires_at > now:
                    return ConsentCheckResult(
                        can_email=True,
                        reason=f"Implied consent valid until {record.expires_at.isoformat()} (basis: {record.basis or 'N/A'})",
                        regime="CASL",
                        consent_type="implied",
                    )
                elif record.expires_at is None:
                    # Implied consent without expiry — CASL implied consent
                    # typically has a 2-year window, but we trust the record
                    return ConsentCheckResult(
                        can_email=True,
                        reason=f"Implied consent on record (basis: {record.basis or 'N/A'}) — verify expiry",
                        regime="CASL",
                        consent_type="implied",
                    )

        return ConsentCheckResult(
            can_email=False,
            reason="No valid consent — all records expired or insufficient",
            regime="CASL",
            consent_type="none",
        )

    else:
        return ConsentCheckResult(
            can_email=False,
            reason=f"Unknown jurisdiction: {country}",
            regime="unknown",
            consent_type="none",
        )


async def record_consent(
    db: AsyncSession,
    contact_id,
    consent_type: str,
    basis: str = None,
    evidence_url: str = None,
    granted_at: datetime = None,
    expires_at: datetime = None,
) -> ConsentRecord:
    """Create a new consent record for a contact."""
    record = ConsentRecord(
        contact_id=contact_id,
        consent_type=consent_type,
        basis=basis,
        evidence_url=evidence_url,
        granted_at=granted_at or datetime.now(timezone.utc),
        expires_at=expires_at,
    )
    db.add(record)
    await db.flush()
    logger.info(
        "Recorded %s consent for contact %s (basis: %s)",
        consent_type, contact_id, basis,
    )
    return record


async def revoke_consent(
    db: AsyncSession,
    consent_id,
) -> ConsentRecord:
    """Revoke an existing consent record."""
    result = await db.execute(
        select(ConsentRecord).where(ConsentRecord.id == consent_id)
    )
    record = result.scalar_one_or_none()
    if not record:
        raise ValueError(f"Consent record {consent_id} not found")

    record.revoked_at = datetime.now(timezone.utc)
    await db.flush()
    logger.info("Revoked consent record %s", consent_id)
    return record
