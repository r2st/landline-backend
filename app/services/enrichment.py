"""Apollo.io enrichment service — enrich company and contact data."""

import logging
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.services.normalize import normalize_domain

logger = logging.getLogger(__name__)
settings = get_settings()


async def _existing_contact_emails(db: AsyncSession, company_id) -> set[str]:
    """Return the lowercased set of emails already on a company's contacts,
    used to skip duplicate inserts during enrichment/discovery."""
    from sqlalchemy import select as _select
    result = await db.execute(
        _select(Contact.email).where(
            Contact.company_id == company_id, Contact.email.isnot(None)
        )
    )
    return {e.lower() for (e,) in result.all() if e}


class ApolloEnrichmentService:
    """Wraps the Apollo.io API for company and contact enrichment."""

    def __init__(self):
        self.api_key = settings.apollo_api_key
        self.base_url = settings.apollo_base_url
        self.headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        }

    async def enrich_company(
        self, db: AsyncSession, company: Company
    ) -> dict:
        """Enrich a company using Apollo's organization endpoint."""
        if not self.api_key:
            logger.warning("Apollo API key not configured — skipping enrichment")
            return {"status": "skipped", "reason": "no_api_key"}

        if not company.domain:
            logger.warning("Company %s has no domain — cannot enrich", company.name)
            return {"status": "skipped", "reason": "no_domain"}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/organizations/enrich",
                    headers=self.headers,
                    json={
                        "api_key": self.api_key,
                        "domain": company.domain,
                    },
                )
                response.raise_for_status()
                data = response.json()

            org = data.get("organization", {})
            if not org:
                return {"status": "not_found"}

            # Update company fields if Apollo has better data
            if org.get("estimated_num_employees") and not company.employee_count:
                company.employee_count = org["estimated_num_employees"]
            if org.get("phone") and not company.phone:
                company.phone = org["phone"]
            if org.get("website_url") and not company.website:
                company.website = org["website_url"]
            if org.get("industry"):
                company.industry_segment = org["industry"]

            await db.flush()
            logger.info("Enriched company %s via Apollo", company.name)
            return {"status": "enriched", "fields_updated": True}

        except httpx.HTTPStatusError as e:
            logger.error("Apollo API error for %s: %s", company.domain, str(e))
            return {"status": "error", "detail": str(e)}
        except Exception as e:
            logger.error("Enrichment failed for %s: %s", company.name, str(e))
            return {"status": "error", "detail": str(e)}

    async def find_contacts(
        self,
        db: AsyncSession,
        company: Company,
        titles: Optional[list[str]] = None,
    ) -> list[dict]:
        """Search Apollo for contacts at a company by domain and title filters."""
        if not self.api_key or not company.domain:
            return []

        if titles is None:
            titles = [
                "VP Manufacturing",
                "Director of Operations",
                "Plant Manager",
                "Quality Manager",
                "VP Operations",
                "Production Manager",
                "CTO",
                "VP Engineering",
            ]

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/mixed_people/search",
                    headers=self.headers,
                    json={
                        "api_key": self.api_key,
                        "q_organization_domains": company.domain,
                        "person_titles": titles,
                        "page": 1,
                        "per_page": 10,
                    },
                )
                response.raise_for_status()
                data = response.json()

            people = data.get("people", [])
            created_contacts = []
            existing_emails = await _existing_contact_emails(db, company.id)

            for person in people:
                email = (person.get("email") or "").strip() or None
                first_name = person.get("first_name")
                last_name = person.get("last_name")

                if not (first_name or last_name):
                    continue

                # Dedup: don't re-create a contact we already have for this company.
                if email and email.lower() in existing_emails:
                    continue
                if email:
                    existing_emails.add(email.lower())

                contact = Contact(
                    company_id=company.id,
                    first_name=first_name,
                    last_name=last_name,
                    title=person.get("title"),
                    email=email,
                    phone=person.get("phone_numbers", [{}])[0].get("sanitized_number") if person.get("phone_numbers") else None,
                    linkedin_url=person.get("linkedin_url"),
                    source="apollo",
                )
                db.add(contact)
                created_contacts.append({
                    "name": f"{first_name} {last_name}",
                    "title": person.get("title"),
                    "email": email,
                })

            await db.flush()
            logger.info(
                "Found %d contacts for %s via Apollo",
                len(created_contacts), company.name,
            )
            return created_contacts

        except Exception as e:
            logger.error("Apollo contact search failed for %s: %s", company.name, str(e))
            return []



    async def discover_executives(
        self,
        db: AsyncSession,
        company: Company,
        titles: Optional[list[str]] = None,
    ) -> list[dict]:
        """Find decision-maker executives at a company and create de-duplicated
        contacts. Returns rich dicts (first/last/title/email/linkedin/seniority)
        for the Tactics view. Skips people whose email already exists on the
        company so repeat runs don't pile up duplicates.

        Returns an empty list when Apollo isn't usable (no key / no domain) —
        the caller surfaces a friendly message in that case.
        """
        if not self.api_key or not company.domain:
            return []

        if titles is None:
            titles = [
                "VP Manufacturing", "Director of Operations", "Plant Manager",
                "Quality Manager", "VP Operations", "Production Manager",
                "CTO", "VP Engineering", "Owner", "President",
            ]

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/mixed_people/search",
                    headers=self.headers,
                    json={
                        "api_key": self.api_key,
                        "q_organization_domains": company.domain,
                        "person_titles": titles,
                        "page": 1,
                        "per_page": 10,
                    },
                )
                response.raise_for_status()
                data = response.json()
        except Exception as e:
            logger.error("Apollo executive search failed for %s: %s", company.name, str(e))
            return []

        existing_emails = await _existing_contact_emails(db, company.id)
        people = data.get("people", [])
        discovered: list[dict] = []

        for person in people:
            first_name = person.get("first_name")
            last_name = person.get("last_name")
            email = (person.get("email") or "").strip() or None
            if not (first_name or last_name):
                continue
            # Dedup: skip anyone whose email we already track for this company.
            if email and email.lower() in existing_emails:
                continue

            phone = None
            if person.get("phone_numbers"):
                phone = person["phone_numbers"][0].get("sanitized_number")

            contact = Contact(
                company_id=company.id,
                first_name=first_name,
                last_name=last_name,
                title=person.get("title"),
                email=email,
                phone=phone,
                linkedin_url=person.get("linkedin_url"),
                source="apollo",
            )
            db.add(contact)
            if email:
                existing_emails.add(email.lower())
            discovered.append({
                "first_name": first_name,
                "last_name": last_name,
                "title": person.get("title"),
                "email": email,
                "linkedin_url": person.get("linkedin_url"),
                "seniority": person.get("seniority") or "Executive",
            })

        await db.flush()
        logger.info("Discovered %d executives for %s", len(discovered), company.name)
        return discovered

    async def search_companies(
        self,
        db: AsyncSession,
        industry: str = "",
        location: str = "",
        min_employees: int = None,
        max_employees: int = None,
        keywords: list[str] = None,
        per_page: int = 10,
    ) -> list[dict]:
        """Search Apollo for companies matching criteria and import them."""
        if not self.api_key:
            return []

        try:
            params = {
                "api_key": self.api_key,
                "page": 1,
                "per_page": min(per_page, 25),
            }
            if industry:
                params["organization_industry_tag_ids"] = []
            if keywords or industry:
                params["q_organization_keyword_tags"] = keywords or [industry]
            if location:
                params["organization_locations"] = [location]
            if min_employees is not None:
                params["organization_num_employees_ranges"] = [f"{min_employees or 1},{max_employees or 10000}"]

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/mixed_companies/search",
                    headers=self.headers,
                    json=params,
                )
                response.raise_for_status()
                data = response.json()

            orgs = data.get("organizations", [])
            imported = []

            for org in orgs:
                domain = normalize_domain(org.get("primary_domain") or org.get("website_url"))
                name = org.get("name")
                if not name:
                    continue

                from sqlalchemy import func as sqla_func
                from sqlalchemy import select
                existing = None
                if domain:
                    result = await db.execute(
                        select(Company).where(Company.domain == domain)
                    )
                    existing = result.scalar_one_or_none()
                if not existing:
                    result = await db.execute(
                        select(Company).where(sqla_func.lower(Company.name) == name.lower())
                    )
                    existing = result.scalar_one_or_none()

                if existing:
                    imported.append({"name": name, "status": "exists", "id": str(existing.id)})
                    continue

                company = Company(
                    name=name,
                    domain=domain,
                    city=org.get("city"),
                    state_province=org.get("state"),
                    country=org.get("country", "US")[:2].upper() if org.get("country") else "US",
                    employee_count=org.get("estimated_num_employees"),
                    industry_segment=org.get("industry"),
                    website=org.get("website_url"),
                    phone=org.get("phone"),
                    source="apollo-search",
                    stage="new",
                )
                if company.country not in ("US", "CA"):
                    company.country = "US"
                db.add(company)
                await db.flush()

                imported.append({
                    "name": name,
                    "domain": domain,
                    "status": "imported",
                    "id": str(company.id),
                    "industry": org.get("industry"),
                    "employee_count": org.get("estimated_num_employees"),
                    "city": org.get("city"),
                    "state": org.get("state"),
                })

            await db.commit()
            return imported

        except Exception as e:
            logger.error("Apollo company search failed: %s", str(e))
            await db.rollback()
            return []


def get_enrichment_service() -> ApolloEnrichmentService:
    return ApolloEnrichmentService()
