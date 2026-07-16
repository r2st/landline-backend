"""Apollo.io enrichment service — the Apollo API client.

This module is a *client*: it talks to Apollo, spends credits, and returns
normalized records. It does **not** decide what to persist and it does **not**
own a transaction — ``app/services/ingestion.py`` handles the first and the
calling task/router handles the second. ``search_companies`` used to do all
three; its internal ``db.commit()`` committed the caller's half-built
``ProspectingRun`` mid-flight and its ``db.rollback()`` destroyed the run row on
error.

Every outbound call goes through :meth:`ApolloEnrichmentService._apollo_request`,
which authenticates with the ``X-Api-Key`` header, checks the credit budget
*before* spending, rate-limits, backs off on 429, and records the attempt in the
ledger whatever the outcome.

Apollo requires the key in that header and rejects it in the request body with
HTTP 422 ``INVALID_API_KEY_LOCATION`` — which is what every org search returned
until 2026-07-16.
"""

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.services import api_credits
from app.services.ingestion import (
    CandidateRecord,
    ContactRecord,
    get_ingestion_service,
)
from app.services.normalize import normalize_domain

logger = logging.getLogger(__name__)
settings = get_settings()

# Apollo caps a company search page at 25 results.
APOLLO_MAX_PER_PAGE = 25
# Retries for a 429 / 5xx before we call it rate-limited and stop.
APOLLO_MAX_ATTEMPTS = 3
# Base for exponential backoff, seconds. Scaled by 2**attempt plus jitter.
APOLLO_BACKOFF_BASE = 0.5
# How much of an error body to keep. Apollo's are one sentence; the cap is for
# the day it returns an HTML error page.
APOLLO_ERROR_BODY_LIMIT = 300

# Fallback title lists for contact discovery. Hardcoded to Apprend's
# manufacturing motion — moving these onto the ICP (`icp.job_titles`) is P1's
# job, at which point callers pass `titles=` and these stop being reached.
DEFAULT_CONTACT_TITLES = (
    "VP Manufacturing",
    "Director of Operations",
    "Plant Manager",
    "Quality Manager",
    "VP Operations",
    "Production Manager",
    "CTO",
    "VP Engineering",
)
DEFAULT_EXECUTIVE_TITLES = DEFAULT_CONTACT_TITLES + ("Owner", "President")


async def _sleep(seconds: float) -> None:
    """Indirection so tests can neutralize backoff without waiting on a clock."""
    await asyncio.sleep(seconds)


class _SlidingWindowLimiter:
    """A shared client-side request limiter.

    Module-level on purpose: a per-instance bucket would reset on every
    ``get_enrichment_service()`` call and limit nothing.
    """

    def __init__(self) -> None:
        self._times: deque[float] = deque()

    def reset(self) -> None:
        self._times.clear()

    async def acquire(self, per_minute: int) -> None:
        if per_minute <= 0:
            return
        while True:
            now = time.monotonic()
            while self._times and now - self._times[0] >= 60.0:
                self._times.popleft()
            if len(self._times) < per_minute:
                self._times.append(now)
                return
            await _sleep(max(0.0, 60.0 - (now - self._times[0])))


_limiter = _SlidingWindowLimiter()


def reset_rate_limiter() -> None:
    """Clear the shared limiter. For tests."""
    _limiter.reset()


@dataclass
class ApolloCallResult:
    """One attempted Apollo call."""

    data: Optional[dict] = None
    # ok | rate_limited | error | budget_exhausted | skipped
    status: str = "ok"
    attempts: int = 0
    credits: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.data is not None


@dataclass
class ApolloSearchResult:
    """The outcome of a (possibly paginated) company search."""

    records: list[CandidateRecord] = field(default_factory=list)
    # ok | rate_limited | budget_exhausted | error | skipped
    status: str = "ok"
    pages_fetched: int = 0
    credits_spent: int = 0
    # True when we stopped before reaching the requested volume. Never a silent
    # truncation: the reason rides along and the caller logs it.
    truncated: bool = False
    error: Optional[str] = None


class ApolloEnrichmentService:
    """Wraps the Apollo.io API for company and contact enrichment."""

    def __init__(self):
        self.settings = get_settings()
        self.api_key = self.settings.apollo_api_key
        self.base_url = self.settings.apollo_base_url
        # The key is *not* baked in here: callers (and tests) reassign
        # ``self.api_key`` after construction, and a header snapshotted in
        # __init__ would silently keep authenticating as the old key.
        self.headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        }

    def _auth_headers(self) -> dict:
        """Request headers including Apollo's key header.

        Apollo rejects the key in the request body with HTTP 422
        ``INVALID_API_KEY_LOCATION``; it must travel in ``X-Api-Key``.
        """
        return {**self.headers, "X-Api-Key": self.api_key}

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """The status line plus Apollo's own explanation of it.

        Apollo says precisely what it dislikes in the response body — "API key
        must be passed in the X-Api-Key header", "not accessible with this
        api_key on a free plan". Recording a bare "HTTP 422" threw that away and
        sent us hunting a payload bug that never existed.
        """
        body = " ".join((response.text or "").split())
        if len(body) > APOLLO_ERROR_BODY_LIMIT:
            body = body[:APOLLO_ERROR_BODY_LIMIT] + "…"
        if not body:
            return f"HTTP {response.status_code}"
        return f"HTTP {response.status_code}: {body}"

    @staticmethod
    def _retry_after(response: httpx.Response) -> Optional[float]:
        """Honour the provider's own backoff hint when it sends one."""
        for header in ("Retry-After", "X-RateLimit-Reset"):
            raw = response.headers.get(header)
            if not raw:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return min(value, 60.0)
        return None

    async def _apollo_request(
        self,
        db: Optional[AsyncSession],
        path: str,
        params: dict,
        *,
        operation: str,
        run_id: Optional[UUID] = None,
        icp_profile_id: Optional[UUID] = None,
    ) -> ApolloCallResult:
        """POST to Apollo under the credit gate, rate limiter and backoff.

        The gate runs **before** the call, so an exhausted budget spends nothing.
        Every attempt that reaches Apollo is written to the ledger — including
        429s and errors, which may still be billed. A 429 that survives its
        retries returns ``rate_limited``, deliberately distinct from a successful
        search that found nothing: conflating them reads an outage as a bad ICP.

        ``db`` may be None for callers with no session (the ledger and gate are
        then skipped — the call still happens, it just isn't metered).
        """
        if not self.api_key:
            return ApolloCallResult(status="skipped", error="no_api_key")

        if db is not None:
            gate = await api_credits.check_budget(db, "apollo", cost=1)
            if not gate.allowed:
                return ApolloCallResult(status="budget_exhausted", error=gate.reason)

        await _limiter.acquire(self.settings.apollo_max_requests_per_minute)

        attempts = 0
        last_error: Optional[str] = None
        status = "error"
        data: Optional[dict] = None

        for attempt in range(1, APOLLO_MAX_ATTEMPTS + 1):
            attempts = attempt
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.post(
                        f"{self.base_url}{path}",
                        headers=self._auth_headers(),
                        json=params,
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = self._error_detail(response)
                    status = "rate_limited" if response.status_code == 429 else "error"
                    if attempt == APOLLO_MAX_ATTEMPTS:
                        break
                    delay = self._retry_after(response)
                    if delay is None:
                        # Exponential with jitter, so a fleet of workers doesn't
                        # retry in lockstep and re-trigger the limit.
                        delay = APOLLO_BACKOFF_BASE * (2 ** (attempt - 1))
                        delay *= 1 + random.random()
                    await _sleep(delay)
                    continue
                if response.status_code >= 400:
                    # Any other 4xx is a verdict on the request, not weather:
                    # a rejected key or an unaffordable endpoint answers the same
                    # way three times, so retrying only spends credits.
                    last_error = self._error_detail(response)
                    status = "error"
                    break
                data = response.json()
                status = "ok"
                break
            except Exception as e:
                last_error = str(e)
                status = "error"
                if attempt == APOLLO_MAX_ATTEMPTS:
                    break
                await _sleep(APOLLO_BACKOFF_BASE * (2 ** (attempt - 1)))
                continue

        # Apollo does not report a per-call credit cost, so charge 1 per
        # attempt that reached it. The ledger is a guardrail, not billing truth.
        credits = attempts
        if db is not None:
            await api_credits.record_usage(
                db,
                "apollo",
                operation,
                credits=credits,
                status=status if status in ("ok", "rate_limited") else "error",
                run_id=run_id,
                icp_profile_id=icp_profile_id,
                details={
                    "path": path,
                    "attempts": attempts,
                    "error": last_error,
                    # Request digest, minus the key.
                    "params": {k: v for k, v in params.items() if k != "api_key"},
                },
            )
        if status != "ok":
            logger.warning(
                "Apollo %s %s after %d attempt(s): %s",
                operation, status, attempts, last_error,
            )
        return ApolloCallResult(
            data=data, status=status, attempts=attempts, credits=credits, error=last_error
        )

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

        call = await self._apollo_request(
            db,
            "/organizations/enrich",
            {"domain": company.domain},
            operation="enrich",
        )
        if not call.ok:
            return {"status": call.status, "detail": call.error}

        org = (call.data or {}).get("organization", {})
        if not org:
            return {"status": "not_found"}

        # Update company fields if Apollo has better data.
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

    @staticmethod
    def _person_to_record(person: dict) -> Optional[ContactRecord]:
        """Map one Apollo person onto the shared contact shape."""
        first_name = person.get("first_name")
        last_name = person.get("last_name")
        if not (first_name or last_name):
            return None
        phone = None
        numbers = person.get("phone_numbers")
        if numbers:
            phone = numbers[0].get("sanitized_number")
        return ContactRecord(
            first_name=first_name,
            last_name=last_name,
            title=person.get("title"),
            email=(person.get("email") or "").strip() or None,
            phone=phone,
            linkedin_url=person.get("linkedin_url"),
            source="apollo",
            raw=person,
        )

    async def _search_people(
        self,
        db: AsyncSession,
        company: Company,
        titles: list[str],
        *,
        run_id: Optional[UUID] = None,
        icp_profile_id: Optional[UUID] = None,
    ) -> list[ContactRecord]:
        call = await self._apollo_request(
            db,
            "/mixed_people/search",
            {
                "q_organization_domains": company.domain,
                "person_titles": titles,
                "page": 1,
                "per_page": 10,
            },
            operation="people_search",
            run_id=run_id,
            icp_profile_id=icp_profile_id,
        )
        if not call.ok:
            return []
        records = []
        for person in (call.data or {}).get("people", []) or []:
            record = self._person_to_record(person)
            if record is not None:
                records.append(record)
        return records

    async def find_contacts(
        self,
        db: AsyncSession,
        company: Company,
        titles: Optional[list[str]] = None,
        run_id: Optional[UUID] = None,
        icp_profile_id: Optional[UUID] = None,
    ) -> list[dict]:
        """Search Apollo for contacts at a company by domain and title filters.

        Persistence and dedup are the ingestion service's, so a person who
        already exists at *another* company is recognized rather than duplicated.
        """
        if not self.api_key or not company.domain:
            return []

        if titles is None:
            titles = list(DEFAULT_CONTACT_TITLES)

        records = await self._search_people(
            db, company, titles, run_id=run_id, icp_profile_id=icp_profile_id
        )
        if not records:
            return []

        outcomes = await get_ingestion_service().ingest_contacts(
            db, company, records, default_source="apollo"
        )
        created = [
            {
                "name": f"{o.record.first_name or ''} {o.record.last_name or ''}".strip(),
                "title": o.record.title,
                "email": o.record.email,
            }
            for o in outcomes
            if o.created
        ]
        logger.info("Found %d contacts for %s via Apollo", len(created), company.name)
        return created



    async def discover_executives(
        self,
        db: AsyncSession,
        company: Company,
        titles: Optional[list[str]] = None,
        run_id: Optional[UUID] = None,
        icp_profile_id: Optional[UUID] = None,
    ) -> list[dict]:
        """Find decision-maker executives at a company and create de-duplicated
        contacts. Returns rich dicts (first/last/title/email/linkedin/seniority)
        for the Tactics view.

        Returns an empty list when Apollo isn't usable (no key / no domain) —
        the caller surfaces a friendly message in that case.
        """
        if not self.api_key or not company.domain:
            return []

        if titles is None:
            titles = list(DEFAULT_EXECUTIVE_TITLES)

        records = await self._search_people(
            db, company, titles, run_id=run_id, icp_profile_id=icp_profile_id
        )
        if not records:
            return []

        outcomes = await get_ingestion_service().ingest_contacts(
            db, company, records, default_source="apollo"
        )
        discovered = [
            {
                "first_name": o.record.first_name,
                "last_name": o.record.last_name,
                "title": o.record.title,
                "email": o.record.email,
                "linkedin_url": o.record.linkedin_url,
                "seniority": (o.record.raw or {}).get("seniority") or "Executive",
            }
            for o in outcomes
            if o.created
        ]
        logger.info("Discovered %d executives for %s", len(discovered), company.name)
        return discovered

    @staticmethod
    def _org_to_record(org: dict) -> Optional[CandidateRecord]:
        """Map one Apollo organization onto the shared candidate shape.

        The country is passed through **verbatim**. The old code coerced
        anything outside US/CA to ``"US"``, which silently turned an Indian
        company into an American one — data corruption dressed up as a default.
        Rejecting an unsupported country is now ingestion's job, and it fails
        loudly.
        """
        name = org.get("name")
        if not name:
            return None
        raw_country = org.get("country")
        return CandidateRecord(
            name=name,
            domain=normalize_domain(org.get("primary_domain") or org.get("website_url")),
            industry=org.get("industry"),
            employee_count=org.get("estimated_num_employees"),
            country=(raw_country[:2].upper() if raw_country else None),
            city=org.get("city"),
            state=org.get("state"),
            phone=org.get("phone"),
            website=org.get("website_url"),
            source="apollo-search",
            raw=org,
        )

    def _search_params(
        self,
        *,
        industry: str,
        location: str,
        min_employees: Optional[int],
        max_employees: Optional[int],
        keywords: Optional[list[str]],
        page: int,
        per_page: int,
    ) -> dict:
        """Build the Apollo org-search body.

        Note what is *absent*: ``organization_industry_tag_ids``. It used to be
        set to ``[]`` whenever an industry was given — an empty list that reads
        like an industry filter but matches nothing and applies nothing, so the
        ICP's top industry only ever reached Apollo as a keyword hint. Rather
        than ship a filter that lies, the industry travels honestly via
        ``q_organization_keyword_tags``. Mapping ICP industries onto real Apollo
        tag IDs needs a maintained lookup table and is deliberately out of scope.
        """
        params: dict = {"page": page, "per_page": min(per_page, APOLLO_MAX_PER_PAGE)}
        if keywords or industry:
            params["q_organization_keyword_tags"] = keywords or [industry]
        if location:
            params["organization_locations"] = [location]
        if min_employees is not None or max_employees is not None:
            params["organization_num_employees_ranges"] = [
                f"{min_employees or 1},{max_employees or 10000}"
            ]
        return params

    async def search_companies(
        self,
        db: Optional[AsyncSession],
        industry: str = "",
        location: str = "",
        min_employees: Optional[int] = None,
        max_employees: Optional[int] = None,
        keywords: Optional[list[str]] = None,
        per_page: int = 10,
        target_volume: Optional[int] = None,
        run_id: Optional[UUID] = None,
        icp_profile_id: Optional[UUID] = None,
    ) -> ApolloSearchResult:
        """Search Apollo for companies. **Returns records; imports nothing.**

        Pages until ``target_volume`` is reached, the results run out, or the
        credit budget is exhausted — whichever comes first. Persisting the
        results is :class:`~app.services.ingestion.IngestionService`'s job, and
        committing is the caller's.
        """
        result = ApolloSearchResult()
        if not self.api_key:
            result.status = "skipped"
            result.error = "no_api_key"
            return result

        wanted = target_volume or per_page
        page = 1
        while len(result.records) < wanted:
            remaining = wanted - len(result.records)
            call = await self._apollo_request(
                db,
                "/mixed_companies/search",
                self._search_params(
                    industry=industry,
                    location=location,
                    min_employees=min_employees,
                    max_employees=max_employees,
                    keywords=keywords,
                    page=page,
                    per_page=min(remaining, APOLLO_MAX_PER_PAGE),
                ),
                operation="org_search",
                run_id=run_id,
                icp_profile_id=icp_profile_id,
            )
            result.credits_spent += call.credits

            if not call.ok:
                # Stop, but say why. A partial page set plus 'rate_limited' is a
                # very different story from a clean 'ok' with few results.
                result.status = call.status
                result.error = call.error
                result.truncated = len(result.records) < wanted
                if result.truncated:
                    logger.warning(
                        "Apollo search stopped at %d/%d records (%s): %s",
                        len(result.records), wanted, call.status, call.error,
                    )
                return result

            result.pages_fetched += 1
            orgs = (call.data or {}).get("organizations", []) or []
            for org in orgs:
                record = self._org_to_record(org)
                if record is not None:
                    result.records.append(record)

            # Last page: Apollo returned fewer than we asked for.
            if len(orgs) < min(remaining, APOLLO_MAX_PER_PAGE):
                break
            page += 1

        result.status = "ok"
        return result


def get_enrichment_service() -> ApolloEnrichmentService:
    return ApolloEnrichmentService()
