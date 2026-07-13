"""Website scraper — extracts company info from their website."""

import asyncio
import ipaddress
import logging
import re
import socket
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Maximum redirects we follow manually (validating each hop) so a redirect to
# an internal address can't bypass the SSRF check.
_MAX_REDIRECTS = 5


class UnsafeURLError(ValueError):
    """Raised when a URL resolves to a private / disallowed address (SSRF guard)."""


def _ip_is_disallowed(ip: ipaddress._BaseAddress) -> bool:
    """Return True for any address an outbound scrape must never reach.

    Covers loopback (127.0.0.0/8, ::1), private RFC1918 (10/8, 172.16/12,
    192.168/16), link-local (169.254/16 — includes the 169.254.169.254 cloud
    metadata endpoint — and fe80::/10), plus unspecified, multicast, and other
    reserved ranges.
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_host_ips(host: str) -> list[ipaddress._BaseAddress]:
    """Resolve *host* to the set of IPs it points at (literal or via DNS)."""
    # A literal IP address — parse directly, no DNS.
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    # A hostname — resolve every A/AAAA record; a host is only safe if *all*
    # of its addresses are safe (defends against DNS rebinding to a public +
    # private pair).
    infos = socket.getaddrinfo(host, None)
    ips: list[ipaddress._BaseAddress] = []
    for family, _type, _proto, _canon, sockaddr in infos:
        ips.append(ipaddress.ip_address(sockaddr[0]))
    return ips


def validate_public_url(url: str) -> str:
    """Validate *url* is a fetchable public http(s) URL, or raise UnsafeURLError.

    Rejects non-http(s) schemes, missing hosts, and any host that resolves to a
    private, loopback, link-local, or otherwise reserved address (including the
    169.254.169.254 cloud metadata IP).
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Unsupported URL scheme: {parts.scheme or '(none)'}")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    try:
        ips = _resolve_host_ips(host)
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeURLError(f"Could not resolve host {host!r}: {exc}") from exc

    if not ips:
        raise UnsafeURLError(f"Host {host!r} did not resolve to any address")

    for ip in ips:
        if _ip_is_disallowed(ip):
            raise UnsafeURLError(
                f"Host {host!r} resolves to disallowed address {ip}"
            )
    return url


async def _validate_public_url_async(url: str) -> str:
    """Async wrapper — DNS resolution is blocking, so run it off the loop."""
    return await asyncio.to_thread(validate_public_url, url)


class WebScraper:
    """Scrapes company websites for contact info, about text, and metadata."""

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (compatible; LandlineBot/1.0; +https://apprendtechnologies.com)",
    }

    async def _safe_get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """GET *url*, validating the SSRF guard on the initial URL and every
        redirect hop. Redirects are followed manually so an internal Location
        can't slip past validation."""
        for _ in range(_MAX_REDIRECTS + 1):
            await _validate_public_url_async(url)
            resp = await client.get(url, headers=self.HEADERS, follow_redirects=False)
            if resp.is_redirect and "location" in resp.headers:
                url = urljoin(url, resp.headers["location"])
                continue
            return resp
        raise UnsafeURLError("Too many redirects")

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
            # follow_redirects is False on the client: _safe_get follows them
            # manually so each hop is SSRF-validated.
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                # Scrape homepage
                try:
                    resp = await self._safe_get(client, url)
                except UnsafeURLError as e:
                    logger.warning("Blocked scrape of %s: %s", domain, e)
                    return {"error": "URL rejected (private or disallowed host)", "domain": domain}
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
                        about_resp = await self._safe_get(client, about_links[0])
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
                        contact_resp = await self._safe_get(client, contact_links[0])
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
