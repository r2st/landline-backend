"""ThomasNet scraper — discovers manufacturing companies from ThomasNet.com.

ThomasNet is a major B2B directory of North American manufacturers and
industrial suppliers. This scraper searches for companies by keyword
and location, extracting company profiles.
"""

import re
from typing import Optional
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.scrapers.base import BaseScraper, ScraperResult


class ThomasNetScraper(BaseScraper):
    """Scrapes company listings from ThomasNet search results."""

    BASE_URL = "https://www.thomasnet.com"
    SEARCH_URL = "https://www.thomasnet.com/nsearch.html"
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self):
        super().__init__("thomasnet")

    async def scrape(
        self,
        keyword: str = "press manufacturer",
        state: Optional[str] = None,
        max_pages: int = 3,
        **kwargs,
    ) -> ScraperResult:
        """Search ThomasNet for manufacturing companies.

        Args:
            keyword: Search term (e.g., "stamping press", "metal fabrication")
            state: Optional US state to filter (e.g., "Ohio", "Michigan")
            max_pages: Maximum number of result pages to scrape
        """
        result = ScraperResult()

        headers = {
            "User-Agent": self.USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, headers=headers
        ) as client:
            for page in range(1, max_pages + 1):
                try:
                    params = {
                        "cov": "NA",
                        "what": keyword,
                        "pg": str(page),
                    }
                    if state:
                        params["where"] = state

                    self.logger.info(
                        "Scraping ThomasNet page %d for '%s'", page, keyword
                    )
                    response = await client.get(self.SEARCH_URL, params=params)
                    response.raise_for_status()

                    companies = self._parse_search_results(response.text)
                    if not companies:
                        self.logger.info("No more results at page %d", page)
                        break

                    for company_data in companies:
                        result.add_company(company_data)

                except httpx.HTTPStatusError as e:
                    result.add_error(f"HTTP {e.response.status_code} on page {page}")
                    break
                except Exception as e:
                    result.add_error(f"Error on page {page}: {str(e)}")
                    break

        return result

    def _parse_search_results(self, html: str) -> list[dict]:
        """Parse a ThomasNet search results page."""
        soup = BeautifulSoup(html, "html.parser")
        companies = []

        # ThomasNet uses supplier-card or similar listing elements
        listings = soup.select(".profile-card, .supplier-card, .company-card, [data-supplier]")

        # Fallback: look for h2 links within result containers
        if not listings:
            listings = soup.select(".result-item, .search-result, .listing")

        for listing in listings:
            try:
                company = self._extract_company(listing)
                if company and company.get("name"):
                    companies.append(company)
            except Exception as e:
                self.logger.debug("Failed to parse listing: %s", str(e))

        return companies

    def _extract_company(self, element) -> Optional[dict]:
        """Extract company data from a listing element."""
        data = {}

        # Company name — usually in an h2 or h3 link
        name_el = element.select_one("h2 a, h3 a, .company-name a, .profile-name a")
        if not name_el:
            name_el = element.select_one("h2, h3, .company-name, .profile-name")
        if name_el:
            data["name"] = name_el.get_text(strip=True)
            href = name_el.get("href", "")
            if href:
                if href.startswith("/"):
                    href = self.BASE_URL + href
                data["source_url"] = href
        else:
            return None

        # Location
        loc_el = element.select_one(".location, .geo-text, .company-location, .city-state")
        if loc_el:
            loc_text = loc_el.get_text(strip=True)
            parsed = self._parse_location(loc_text)
            data.update(parsed)

        # Phone
        phone_el = element.select_one(".phone, .company-phone, [data-phone]")
        if phone_el:
            data["phone"] = phone_el.get_text(strip=True)

        # Website / domain
        website_el = element.select_one("a[data-action='website'], .website-link")
        if website_el:
            data["website"] = website_el.get("href", "")

        # Description / segment
        desc_el = element.select_one(".description, .supplier-description, .company-desc")
        if desc_el:
            data["description"] = desc_el.get_text(strip=True)[:255]

        data["source"] = "thomasnet"
        data["country"] = data.get("country", "US")

        return data

    def _parse_location(self, location_text: str) -> dict:
        """Parse 'City, ST' or 'City, State' into components."""
        result = {}
        if not location_text:
            return result

        # Pattern: "City, ST" or "City, ST 12345"
        match = re.match(
            r"^(.+?),\s*([A-Z]{2})\s*(\d{5})?",
            location_text.strip(),
        )
        if match:
            result["city"] = match.group(1).strip()
            result["state_province"] = match.group(2)
            result["country"] = "US"
        else:
            # Try "City, Province, Country" pattern
            parts = [p.strip() for p in location_text.split(",")]
            if len(parts) >= 2:
                result["city"] = parts[0]
                result["state_province"] = parts[1]
            if len(parts) >= 3 and parts[2].upper() in ("CANADA", "CA"):
                result["country"] = "CA"

        return result

    async def save_results(
        self, db: AsyncSession, result: ScraperResult
    ) -> dict:
        """Persist ThomasNet results, skipping duplicates by domain or name."""
        saved = 0
        skipped = 0

        for data in result.companies:
            name = data.get("name", "").strip()
            if not name:
                skipped += 1
                continue

            # Check for duplicate by name (ThomasNet often lacks domains)
            existing = await db.execute(
                select(Company).where(Company.name == name).limit(1)
            )
            if existing.scalar_one_or_none():
                skipped += 1
                continue

            company = Company(
                name=name,
                domain=data.get("domain"),
                city=data.get("city"),
                state_province=data.get("state_province"),
                country=data.get("country", "US"),
                source="thomasnet",
                source_url=data.get("source_url"),
                website=data.get("website"),
                phone=data.get("phone"),
                industry_segment=data.get("description"),
                naics_code="33",  # Default manufacturing NAICS prefix
            )
            db.add(company)
            saved += 1

        await db.flush()
        self.logger.info("Saved %d companies, skipped %d duplicates", saved, skipped)
        return {"saved": saved, "skipped": skipped}
