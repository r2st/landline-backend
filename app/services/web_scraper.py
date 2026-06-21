"""Website scraper — extracts company info from their website."""

import logging
import re
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class WebScraper:
    """Scrapes company websites for contact info, about text, and metadata."""

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; LandlineBot/1.0; +https://apprendtechnologies.com)",
    }

    async def scrape_company(self, domain: str) -> dict:
        """Scrape a company website for useful information."""
        if not domain:
            return {"error": "No domain provided"}

        url = f"https://{domain}" if not domain.startswith("http") else domain
        result = {
            "domain": domain,
            "title": None,
            "description": None,
            "about": None,
            "emails": [],
            "phones": [],
            "social_links": {},
            "technologies": [],
            "pages_checked": [],
        }

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, verify=False) as client:
                # Scrape homepage
                resp = await client.get(url, headers=self.HEADERS)
                if resp.status_code != 200:
                    return {"error": f"HTTP {resp.status_code}", "domain": domain}

                soup = BeautifulSoup(resp.text, "html.parser")
                result["pages_checked"].append(url)

                # Extract basic metadata
                title_tag = soup.find("title")
                result["title"] = title_tag.get_text(strip=True) if title_tag else None

                meta_desc = soup.find("meta", attrs={"name": "description"})
                if meta_desc:
                    result["description"] = meta_desc.get("content", "").strip()

                # Extract emails from page text
                emails = set(re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', resp.text))
                # Filter out common non-person emails
                skip_prefixes = ("noreply", "no-reply", "info@", "support@", "admin@", "webmaster@")
                result["emails"] = [e for e in emails if not any(e.lower().startswith(p) for p in skip_prefixes)][:10]

                # Extract phone numbers
                phones = set(re.findall(r'[\+]?[(]?[0-9]{3}[)]?[-\s\.]?[0-9]{3}[-\s\.]?[0-9]{4,6}', resp.text))
                result["phones"] = list(phones)[:5]

                # Extract social media links
                for link in soup.find_all("a", href=True):
                    href = link["href"].lower()
                    if "linkedin.com" in href:
                        result["social_links"]["linkedin"] = link["href"]
                    elif "twitter.com" in href or "x.com" in href:
                        result["social_links"]["twitter"] = link["href"]
                    elif "facebook.com" in href:
                        result["social_links"]["facebook"] = link["href"]

                # Try to find About page
                about_links = []
                for link in soup.find_all("a", href=True):
                    href = link["href"].lower()
                    text = link.get_text(strip=True).lower()
                    if "about" in href or "about" in text:
                        about_url = urljoin(url, link["href"])
                        about_links.append(about_url)

                if about_links:
                    try:
                        about_resp = await client.get(about_links[0], headers=self.HEADERS)
                        if about_resp.status_code == 200:
                            about_soup = BeautifulSoup(about_resp.text, "html.parser")
                            # Get main content text
                            for tag in about_soup(["script", "style", "nav", "header", "footer"]):
                                tag.decompose()
                            about_text = about_soup.get_text(" ", strip=True)
                            # Take first 500 chars as about summary
                            result["about"] = about_text[:500] if about_text else None
                            result["pages_checked"].append(about_links[0])

                            # Also grab emails from about page
                            more_emails = set(re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', about_resp.text))
                            result["emails"] = list(set(result["emails"]) | more_emails)[:10]
                    except Exception:
                        pass

                # Try contact page too
                contact_links = []
                for link in soup.find_all("a", href=True):
                    href = link["href"].lower()
                    text = link.get_text(strip=True).lower()
                    if "contact" in href or "contact" in text:
                        contact_url = urljoin(url, link["href"])
                        contact_links.append(contact_url)

                if contact_links:
                    try:
                        contact_resp = await client.get(contact_links[0], headers=self.HEADERS)
                        if contact_resp.status_code == 200:
                            result["pages_checked"].append(contact_links[0])
                            more_emails = set(re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', contact_resp.text))
                            more_phones = set(re.findall(r'[\+]?[(]?[0-9]{3}[)]?[-\s\.]?[0-9]{3}[-\s\.]?[0-9]{4,6}', contact_resp.text))
                            result["emails"] = list(set(result["emails"]) | more_emails)[:10]
                            result["phones"] = list(set(result["phones"]) | more_phones)[:5]
                    except Exception:
                        pass

        except Exception as e:
            logger.error("Scrape failed for %s: %s", domain, str(e))
            result["error"] = str(e)

        return result


def get_web_scraper() -> WebScraper:
    return WebScraper()
