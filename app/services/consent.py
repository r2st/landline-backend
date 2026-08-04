"""Consent verification — the per-jurisdiction send gate.

Two consent models cover every market the schema admits, and
``constants.COUNTRY_CONSENT_REGIMES`` says which one each country takes:

* **opt-out** (CAN-SPAM's model) — a cold send is permitted; the duty is an
  honoured unsubscribe, which every send discharges via the footer link and the
  RFC 8058 header.
* **opt-in** (CASL's model) — express or unexpired implied consent must be on
  record *before* the first send.

An unrecognised country is still refused, because failing closed is the right
default for a compliance gate. What was wrong was *which* countries were
unrecognised: the branch tested ``US`` and ``CA`` literals while the schema
allowlist had grown to nine countries, so a company in GB, IN, AU, DE, FR, SG or
AE reached the wire, answered "Unknown jurisdiction", and
``tasks.outreach.send_sequence_step`` **paused the enrollment** — permanently,
since nothing re-examines a paused enrollment's consent. Sourcing, enrichment
and enrollment all worked for those markets; only the last hop refused, once,
quietly, per contact. The regime table is now the single list, and a drift test
requires every allowed country to appear in it.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import COUNTRY_CONSENT_REGIMES, OPT_IN, OPT_OUT
from app.models.company import Company
from app.models.contact import Contact
from app.models.consent import ConsentRecord

logger = logging.getLogger(__name__)


def regime_for(country: str | None) -> tuple[str, str] | None:
    """``(regime name, consent model)`` for a country, or None if unrecognised."""
    if not country:
        return None
    return COUNTRY_CONSENT_REGIMES.get(country.strip().upper())


def _regime_name(country: str | None) -> str:
    """The regime label to stamp on a *refusal*, which happens before dispatch.

    A suppressed contact is refused whatever the jurisdiction, and the operator
    still wants to know which one applied.
    """
    mapped = regime_for(country)
    return mapped[0] if mapped else "unknown"


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

    Opt-in regimes (CASL, and the others marked ``OPT_IN``): express or unexpired
    implied consent must exist BEFORE sending. Opt-out regimes (CAN-SPAM and
    friends): send without prior consent, honour the unsubscribe.
    """
    country = company.country
    mapped = regime_for(country)

    # Hard suppression (single source of truth). A contact suppressed via an
    # unsubscribe reply (Feature 1) is never emailed again, regardless of regime.
    # Checked before everything else so it can never be bypassed.
    if getattr(contact, "suppressed", False):
        return ConsentCheckResult(
            can_email=False,
            reason="Contact is suppressed (unsubscribe/compliance) — never email",
            regime=_regime_name(country),
            consent_type="none",
        )

    # Global check: if contact has explicitly unsubscribed via link, block all sends
    if getattr(contact, "unsubscribed", False):
        return ConsentCheckResult(
            can_email=False,
            reason="Contact has unsubscribed via email link",
            regime=_regime_name(country),
            consent_type="none",
        )

    if mapped is None:
        # Fail closed — but this is now reachable only for a country the schema
        # allowlist itself rejects, not for a market we deliberately support.
        return ConsentCheckResult(
            can_email=False,
            reason=f"Unknown jurisdiction: {country}",
            regime="unknown",
            consent_type="none",
        )

    regime, model = mapped

    if model == OPT_OUT:
        # Opt-out model — check they haven't unsubscribed
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
                reason=f"Contact has unsubscribed ({regime} opt-out)",
                regime=regime,
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
                regime=regime,
                consent_type="none",
            )

        return ConsentCheckResult(
            can_email=True,
            reason=f"{regime} allows sending with unsubscribe option",
            regime=regime,
            consent_type="implied",
        )

    else:
        # Opt-in model (CASL and friends) — need valid express or implied consent
        assert model == OPT_IN, f"unhandled consent model {model!r}"
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
                reason=f"No consent record found — {regime} requires opt-in before sending",
                regime=regime,
                consent_type="none",
            )

        # Check for valid express consent (no expiry, or not yet expired)
        for record in records:
            if record.consent_type == "express":
                if record.expires_at is None or record.expires_at > now:
                    return ConsentCheckResult(
                        can_email=True,
                        reason=f"Express consent on record (basis: {record.basis or 'N/A'})",
                        regime=regime,
                        consent_type="express",
                    )

        # Check for valid implied consent (must not be expired)
        for record in records:
            if record.consent_type == "implied":
                if record.expires_at is not None and record.expires_at > now:
                    return ConsentCheckResult(
                        can_email=True,
                        reason=f"Implied consent valid until {record.expires_at.isoformat()} (basis: {record.basis or 'N/A'})",
                        regime=regime,
                        consent_type="implied",
                    )
                elif record.expires_at is None:
                    # Implied consent without expiry — CASL implied consent
                    # typically has a 2-year window, but we trust the record
                    return ConsentCheckResult(
                        can_email=True,
                        reason=f"Implied consent on record (basis: {record.basis or 'N/A'}) — verify expiry",
                        regime=regime,
                        consent_type="implied",
                    )

        return ConsentCheckResult(
            can_email=False,
            reason="No valid consent — all records expired or insufficient",
            regime=regime,
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
