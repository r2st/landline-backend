"""Pull an existing book of business *in* from a CRM.

The integration marketplace has had real HubSpot and Salesforce clients since
feature 4.2, but only in one direction: ``sync_contact`` pushes a person out.
Nothing came back. That ordering is backwards for a prospecting agent — before
Dialtone can sensibly go find new companies, it needs to know which ones you
already have, or it will cold-email the customer you closed last quarter and the
deal your colleague is mid-negotiation on. Deduplicating against the CRM is the
cheapest safety this system can buy.

Three decisions worth stating, because each one is a place this could quietly go
wrong:

**It ingests, it does not enroll.** Records go through ``IngestionService`` with
no ICP, which routes every one of them to ``ROUTE_IMPORT``: persisted, scored on
request, and *not* put into a sequence. Importing your CRM must never be the
event that starts emailing it. If you want to prospect the imported book, that
is a separate, deliberate act.

**A CRM contact is a company plus a person, in that order.** The CRM's unit is a
person; ours is a company with contacts under it. One ``CandidateRecord`` per
person, carrying its own single ``ContactRecord``, lets the existing dedup do the
work: two HubSpot contacts at the same firm resolve to one company by domain on
the second pass. It costs a resolve per row and it means the CRM's own duplicate
records — which every CRM has — collapse here rather than propagate.

**The domain is derived from the email when the CRM has no website.** A CRM row
often has ``jane@acme.com`` and a blank Website field, and domain is the only
dedup signal strong enough to auto-merge (``ingestion.resolve_company``, rule 1).
Falling back to the email's domain turns a name-only medium match — which
deliberately does *not* merge — into a certain one. Free-mail domains are
excluded: everyone at gmail.com is not one company.

Paging is a loop here rather than inside the connector, so ``max_records`` is a
real budget: a 40k-contact HubSpot account imports in bounded slices instead of
one request that times out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ingestion import (
    CandidateRecord,
    ContactRecord,
    IngestionResult,
    IngestionService,
)
from app.services.normalize import normalize_domain

logger = logging.getLogger(__name__)

# Providers whose connector implements ``fetch_contacts``.
CRM_PULL_PROVIDERS = ("hubspot", "salesforce")

# Never treat a free-mail host as a company domain: it would merge every
# consumer-addressed contact in the CRM into one enormous fake company, and the
# merge is silent because a domain match is ``certain``.
FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "gmx.com", "mail.com",
    "zoho.com", "yandex.com", "comcast.net", "verizon.net", "sbcglobal.net",
})

# One connector call per page; this bounds the loop even if a provider returns a
# cursor that never resolves to an empty page.
MAX_PAGES = 50


def domain_from_email(email: Optional[str]) -> Optional[str]:
    """The company domain implied by a work address, or None.

    Returns None for a free-mail host — see ``FREE_EMAIL_DOMAINS``.
    """
    if not email or "@" not in email:
        return None
    host = normalize_domain(email.rsplit("@", 1)[-1])
    if not host or host in FREE_EMAIL_DOMAINS:
        return None
    return host


def _clean(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_candidate_record(row: dict, *, provider: str) -> Optional[CandidateRecord]:
    """Map one provider-neutral CRM row to a ``CandidateRecord``.

    Returns None when the row names no company we could file the person under —
    a CRM contact with neither a company name nor a work-email domain is a
    personal address in somebody's address book, not a lead.
    """
    email = _clean(row.get("email"))
    website = _clean(row.get("website"))
    domain = normalize_domain(website) or domain_from_email(email)
    company_name = _clean(row.get("company")) or domain
    if not company_name:
        return None

    employees = row.get("employee_count")
    try:
        employees = int(employees) if employees not in (None, "") else None
    except (TypeError, ValueError):
        employees = None

    source = f"crm_{provider}"
    contacts: list[ContactRecord] = []
    if email or _clean(row.get("first_name")) or _clean(row.get("last_name")):
        contacts.append(ContactRecord(
            first_name=_clean(row.get("first_name")),
            last_name=_clean(row.get("last_name")),
            email=email,
            title=_clean(row.get("title")),
            phone=_clean(row.get("phone")),
            source=source,
            raw={"external_id": _clean(row.get("external_id")), "provider": provider},
        ))

    return CandidateRecord(
        name=company_name,
        domain=domain,
        website=website,
        employee_count=employees,
        city=_clean(row.get("city")),
        state=_clean(row.get("state")),
        # Country is validated against market policy by the ingester, which
        # raises for an unsupported one rather than relabelling it. A CRM export
        # is full of two-letter codes, full names and blanks, and a rejected row
        # is a worse outcome here than an unset country the operator can fix —
        # so the CRM's value rides along in ``raw`` and does not gate the import.
        country=None,
        contacts=contacts,
        source=source,
        raw={"crm": row, "crm_country": _clean(row.get("country"))},
    )


@dataclass
class CrmImportSummary:
    """What a pull actually did, in the terms an operator asks about."""

    provider: str
    fetched: int = 0
    skipped_unusable: int = 0
    pages: int = 0
    cursor: Optional[str] = None
    dry_run: bool = False
    companies_created: int = 0
    companies_matched: int = 0
    contacts_created: int = 0
    contacts_skipped: int = 0
    needs_review: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "fetched": self.fetched,
            "skipped_unusable": self.skipped_unusable,
            "pages": self.pages,
            "next_cursor": self.cursor,
            "dry_run": self.dry_run,
            "companies_created": self.companies_created,
            "companies_matched": self.companies_matched,
            "contacts_created": self.contacts_created,
            "contacts_skipped": self.contacts_skipped,
            "needs_review": self.needs_review,
            "errors": self.errors,
        }

    def absorb(self, result: IngestionResult) -> None:
        self.companies_created += result.companies_created
        self.companies_matched += result.companies_matched
        self.contacts_created += result.contacts_created
        self.contacts_skipped += sum(o.contacts_skipped for o in result.outcomes)
        self.needs_review += result.duplicates
        self.errors.extend(result.errors)


class CrmImporter:
    """Pull contacts from a configured CRM integration into Landline."""

    def __init__(self, ingester: Optional[IngestionService] = None) -> None:
        self.ingester = ingester or IngestionService()

    async def import_contacts(
        self,
        db: AsyncSession,
        connector,
        config: dict,
        credentials: dict,
        *,
        provider: str,
        max_records: int = 500,
        page_size: int = 100,
        cursor: Optional[str] = None,
        dry_run: bool = False,
    ) -> CrmImportSummary:
        """Page through the CRM and ingest what comes back.

        Owns no transaction: ``ingest`` flushes, the caller commits — the same
        contract every other ingestion source keeps, so an import can be rolled
        back whole if the caller decides to.

        A page that fails stops the loop and records the reason rather than
        raising. Half an import is a real outcome (the cursor comes back so it
        can be resumed); a 500 that loses the pages already ingested is not.
        """
        summary = CrmImportSummary(provider=provider, dry_run=dry_run, cursor=cursor)

        while summary.fetched < max_records and summary.pages < MAX_PAGES:
            remaining = max_records - summary.fetched
            result = await connector.execute(
                "fetch_contacts",
                {"cursor": summary.cursor, "limit": min(page_size, remaining)},
                config,
                credentials,
            )
            summary.pages += 1
            if not result.ok:
                summary.errors.append(result.message)
                break

            rows = result.data.get("records") or []
            summary.fetched += len(rows)

            records: list[CandidateRecord] = []
            for row in rows:
                record = to_candidate_record(row, provider=provider)
                if record is None:
                    summary.skipped_unusable += 1
                    continue
                records.append(record)

            if records:
                # No ICP: every record routes to ROUTE_IMPORT — persisted, never
                # enrolled. See the module docstring.
                ingested = await self.ingester.ingest(
                    db, records, icp=None, enroll_budget=0, dry_run=dry_run
                )
                summary.absorb(ingested)

            summary.cursor = result.data.get("cursor")
            if not summary.cursor or not rows:
                break

        logger.info(
            "CRM import (%s): fetched=%d created=%d matched=%d contacts=%d%s",
            provider, summary.fetched, summary.companies_created,
            summary.companies_matched, summary.contacts_created,
            " [dry run]" if dry_run else "",
        )
        return summary


_importer: Optional[CrmImporter] = None


def get_crm_importer() -> CrmImporter:
    global _importer
    if _importer is None:
        _importer = CrmImporter()
    return _importer
