"""Apollo.io enrichment service — enrich company and contact data."""

import logging
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact

logger = logging.getLogger(__name__)
settings = get_settings()


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

            for person in people:
                email = person.get("email")
                first_name = person.get("first_name")
                last_name = person.get("last_name")

                if not (first_name or last_name):
                    continue

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


def get_enrichment_service() -> ApolloEnrichmentService:
    return ApolloEnrichmentService()
