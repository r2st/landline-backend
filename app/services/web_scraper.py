"""Website scraper — extracts company info from their website."""

import asyncio
import html as html_mod
import ipaddress
import logging
import re
import socket
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Maximum redirects we follow manually (validating each hop) so a redirect to
# an internal address can't bypass the SSRF check.
_MAX_REDIRECTS = 5

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Local-parts of addresses no human reads — automated/technical inboxes. These
# are dropped outright: sending to them is a wasted touch at best and a
# spam-complaint magnet at worst.
_UNSENDABLE_EMAIL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
    "webmaster", "hostmaster", "abuse", "mailer-daemon", "mailerdaemon",
    "bounce", "bounces", "notifications", "notification", "newsletter",
    "unsubscribe", "privacy", "legal", "dmarc", "spf",
)

# Generic-but-monitored inboxes. Not a named person, but for a small
# manufacturer these are frequently the ONLY address on the site and a real
# human reads them — so they are RANKED behind any personal-looking address
# rather than discarded. Discarding them (the old behaviour) stranded every
# shop that lists only info@ / sales@ / contact@, which is most of them, and is
# why Places-sourced businesses kept importing with no emailable contact.
_GENERIC_EMAIL_PREFIXES = (
    "sales", "contact", "info", "hello", "hi", "enquiries", "enquiry",
    "inquiries", "inquiry", "office", "admin", "support", "mail", "team",
    "reception", "general", "customerservice", "service",
)

# Image/asset false positives the email regex sometimes catches (e.g. a
# "logo@2x.png" sprite reference).
_ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")


def _deobfuscate(text: str) -> str:
    """Undo the common human-readable email obfuscations before regex.

    Only bracketed forms — ``name [at] domain [dot] com`` — are rewritten, so
    ordinary prose ("meet us at the shop") is never touched. HTML entities are
    also unescaped first so ``&#64;``-encoded @ signs become real ones.
    """
    text = html_mod.unescape(text)
    text = re.sub(r"\s*[\[(]\s*at\s*[\])]\s*", "@", text, flags=re.I)
    text = re.sub(r"\s*[\[(]\s*dot\s*[\])]\s*", ".", text, flags=re.I)
    return text


def extract_emails(html_text: str) -> set[str]:
    """Pull every email-shaped string out of a chunk of HTML/text."""
    return set(_EMAIL_RE.findall(_deobfuscate(html_text or "")))


def _email_rank(email: str) -> int:
    """Sort key — 0 = personal-looking, 1 = generic inbox. Lower is preferred."""
    local = email.split("@", 1)[0].lower()
    if any(local == p or local.startswith(p) for p in _GENERIC_EMAIL_PREFIXES):
        return 1
    return 0


def usable_emails(raw: Iterable[str], limit: int = 10) -> list[str]:
    """Filter, de-dupe and rank scraped emails for cold outreach.

    Technical/automated inboxes are dropped; everything else is kept with
    personal-looking addresses ranked ahead of generic ones. A generic inbox is
    a real fallback, not junk — for an SMB the ``info@`` box is often the only
    door there is, and one monitored generic address beats no contact at all.
    """
    seen: set[str] = set()
    out: list[str] = []
    for e in raw:
        el = (e or "").strip().lower().strip(".")
        if not el or "@" not in el or el in seen:
            continue
        if el.endswith(_ASSET_SUFFIXES):
            continue
        local = el.split("@", 1)[0]
        if any(local == p or local.startswith(p) for p in _UNSENDABLE_EMAIL_PREFIXES):
            continue
        seen.add(el)
        out.append(el)
    out.sort(key=_email_rank)
    return out[:limit]


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

                # Accumulate emails across every page we touch, then filter and
                # rank once at the end (see ``usable_emails``). Generic inboxes
                # are kept as a ranked fallback, not thrown away.
                all_emails: set[str] = extract_emails(resp.text)

                # Extract phone numbers (finalized after sub-pages are scanned).
                phones = set(re.findall(r'[\+]?[(]?[0-9]{3}[)]?[-\s\.]?[0-9]{3}[-\s\.]?[0-9]{4,6}', resp.text))

                # Extract social media links
                for link in soup.find_all("a", href=True):
                    href = link["href"].lower()
                    if "linkedin.com" in href:
                        result["social_links"]["linkedin"] = link["href"]
                    elif "twitter.com" in href or "x.com" in href:
                        result["social_links"]["twitter"] = link["href"]
                    elif "facebook.com" in href:
                        result["social_links"]["facebook"] = link["href"]

                # Sub-pages worth a look for emails/people. Contact and team
                # pages are where a small manufacturer actually lists an address
                # or a name; about is mostly for the summary text. We fetch the
                # first matching link in each category (deduped) so one scrape
                # can't fan out into an unbounded crawl.
                phone_re = re.compile(
                    r'[\+]?[(]?[0-9]{3}[)]?[-\s\.]?[0-9]{3}[-\s\.]?[0-9]{4,6}'
                )

                def _first_link(keywords: tuple[str, ...]) -> Optional[str]:
                    for link in soup.find_all("a", href=True):
                        href = link["href"].lower()
                        text = link.get_text(strip=True).lower()
                        if any(k in href or k in text for k in keywords):
                            return urljoin(url, link["href"])
                    return None

                sub_pages: list[str] = []
                for keywords in (
                    ("about",),
                    ("contact", "get-in-touch", "reach-us"),
                    ("team", "staff", "leadership", "people", "our-team", "management"),
                ):
                    link = _first_link(keywords)
                    if link and link not in sub_pages and link not in result["pages_checked"]:
                        sub_pages.append(link)

                for i, page_url in enumerate(sub_pages):
                    try:
                        sub_resp = await self._safe_get(client, page_url)
                    except Exception:
                        continue
                    if sub_resp.status_code != 200:
                        continue
                    result["pages_checked"].append(page_url)
                    all_emails |= extract_emails(sub_resp.text)
                    phones |= set(phone_re.findall(sub_resp.text))
                    # The first sub-page is the About page — use it for the summary.
                    if i == 0 and result["about"] is None:
                        sub_soup = BeautifulSoup(sub_resp.text, "html.parser")
                        for tag in sub_soup(["script", "style", "nav", "header", "footer"]):
                            tag.decompose()
                        about_text = sub_soup.get_text(" ", strip=True)
                        result["about"] = about_text[:500] if about_text else None

                result["phones"] = list(phones)[:5]
                result["emails"] = usable_emails(all_emails)

        except Exception as e:
            logger.error("Scrape failed for %s: %s", domain, str(e))
            result["error"] = str(e)

        return result


def get_web_scraper() -> WebScraper:
    return WebScraper()
