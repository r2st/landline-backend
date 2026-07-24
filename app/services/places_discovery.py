"""Google Places (New) SMB discovery — the long tail Apollo doesn't cover (P4).

Apollo's B2B database is thin on small international manufacturers; Places has
near-exhaustive local-business coverage. That is the whole case for this module,
and it comes with an honest caveat worth stating before anyone budgets against
it: **Places returns zero emails and zero people.** The chain to something a
cold-email agent can use is

    Places (name, phone, website)
      -> Hunter /domain-search  (emails by domain)          [paid, best]
      -> web_scraper.scrape_company(domain)                 [free, fallback]
        -> verify                                           [P3]
          -> contact

and every hop loses volume. Expect a low single-digit percentage of Places
results to become verified emailable contacts. Places' *phone* coverage, by
contrast, is excellent — feeding the cold-call queue is plausibly the better ROI
than feeding the sequence.

Three rules this module holds to, all inherited from the Apollo client next door:

* **The credit gate runs before the call.** Broad coverage means many narrow
  queries (a category x geography grid), each page billed. Ungated, a handful of
  weekly searches compounds quietly.
* **It owns no transaction and never raises.** Missing key -> ``skipped``. The
  task or router commits.
* **A rate-limited search is not an empty one.** ``rate_limited`` and
  ``budget_exhausted`` are distinct statuses, because conflating them reads an
  outage (or a spending cap) as "that market has no businesses in it".

**Field masks are mandatory and they set the price.** Google bills Places (New)
by SKU tier based on which fields you ask for, so the masks below are exactly
the fields this pipeline reads and nothing more — adding one is a pricing
decision, not a convenience. ``tests/test_places_discovery.py`` pins them.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.discovered_business import DiscoveredBusiness
from app.models.discovery_search import DiscoverySearch
from app.services import api_credits
# The sliding-window bucket the Apollo client already uses. Shared rather than
# re-implemented — a second copy is a second thing to fix when the algorithm is
# wrong. Each provider keeps its own *instance* (and so its own window), since
# Google and Apollo rate-limit independently.
from app.services.enrichment import _SlidingWindowLimiter
from app.services.ingestion import CandidateRecord, ContactRecord
from app.services.normalize import normalize_domain, normalize_phone

logger = logging.getLogger(__name__)

PROVIDER = "google_places"


async def _sleep(seconds: float) -> None:
    """Indirection so tests can neutralize backoff without waiting on a clock."""
    await asyncio.sleep(seconds)


# Retries for a 429 / 5xx before we call it rate-limited and stop.
PLACES_MAX_ATTEMPTS = 3
PLACES_BACKOFF_BASE = 0.5
PLACES_TIMEOUT = 30.0

# --- Field masks: these are the price tag. Do not widen casually. -------------
#
# searchText masks are prefixed ``places.``; the details mask is not (its fields
# are top-level on the response). Google rejects a request with no mask at all,
# and bills a request with a lavish one.
#
# Justification, field by field — every one of these has a consumer:
#   id                       -> place_id, the dedup anchor and the only field
#                               Google's terms let us cache indefinitely
#   displayName              -> business_name
#   formattedAddress         -> address
#   addressComponents        -> city / state / country
#   internationalPhoneNumber -> phone (the call queue; Places' strongest field)
#   websiteUri               -> domain, the bridge to an email. Without this the
#                               capability produces nothing emailable at all.
#   rating, userRatingCount  -> a weak legitimacy proxy; Places gives us no
#                               employee count or revenue to filter on
#   primaryType              -> category
_PLACE_FIELDS = (
    "id",
    "displayName",
    "formattedAddress",
    "addressComponents",
    "internationalPhoneNumber",
    "websiteUri",
    "rating",
    "userRatingCount",
    "primaryType",
)
SEARCH_FIELD_MASK = ",".join(f"places.{f}" for f in _PLACE_FIELDS) + ",nextPageToken"
DETAILS_FIELD_MASK = ",".join(_PLACE_FIELDS)

# Shared client-side token bucket. Module-level for the same reason Apollo's is:
# a per-instance bucket resets on every get_places_service() call and limits
# nothing.
_limiter = _SlidingWindowLimiter()


def reset_rate_limiter() -> None:
    """Clear the shared limiter. For tests."""
    _limiter.reset()


@dataclass
class PlacesCallResult:
    """One attempted Places call."""

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
class PlaceResult:
    """A Places business, normalized away from Google's response shape."""

    place_id: str
    name: str
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    domain: Optional[str] = None
    category: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    raw: dict = field(default_factory=dict)


@dataclass
class PlacesSearchResult:
    """The outcome of a (possibly paginated) Places search."""

    results: list[PlaceResult] = field(default_factory=list)
    # ok | rate_limited | budget_exhausted | error | skipped
    status: str = "ok"
    pages_fetched: int = 0
    credits_spent: int = 0
    # True when we stopped before the requested limit. Never a silent
    # truncation: the reason rides along and the caller logs it.
    truncated: bool = False
    error: Optional[str] = None


@dataclass
class DiscoveryOutcome:
    """What one ``discover`` run did."""

    businesses: list[DiscoveredBusiness] = field(default_factory=list)
    created: int = 0
    duplicates: int = 0
    status: str = "ok"
    credits_spent: int = 0
    truncated: bool = False
    error: Optional[str] = None


def _address_part(components: list[dict], *wanted: str, short: bool = False) -> Optional[str]:
    """Pull one component out of a Places addressComponents list.

    Google orders components most-specific-first and types are a list, so a
    locality can also be a political division. Match on membership, take the
    first hit.
    """
    for component in components or []:
        types = component.get("types") or []
        if any(w in types for w in wanted):
            key = "shortText" if short else "longText"
            value = (component.get(key) or component.get("longText") or "").strip()
            if value:
                return value
    return None


def parse_place(payload: dict, *, default_country: Optional[str] = None) -> Optional[PlaceResult]:
    """Normalize one Places payload. Returns None if it has no usable identity.

    Defensive by design: Places omits fields freely (a business with no website,
    no phone, no rating is common and still worth staging), and a KeyError here
    would lose an entire page of results.
    """
    place_id = (payload.get("id") or "").strip()
    name = ((payload.get("displayName") or {}).get("text") or "").strip()
    if not place_id or not name:
        return None

    components = payload.get("addressComponents") or []
    # shortText for country: Google gives 'IN' there and 'India' in longText,
    # and everything downstream speaks ISO-3166-1 alpha-2.
    country = _address_part(components, "country", short=True)
    if country:
        country = country.strip().upper()[:2]
    website = (payload.get("websiteUri") or "").strip() or None
    rating = payload.get("rating")
    review_count = payload.get("userRatingCount")

    return PlaceResult(
        place_id=place_id,
        name=name,
        address=(payload.get("formattedAddress") or "").strip() or None,
        city=_address_part(components, "locality", "postal_town"),
        state=_address_part(components, "administrative_area_level_1", short=True),
        country=country or default_country,
        phone=normalize_phone(payload.get("internationalPhoneNumber")),
        website=website,
        domain=normalize_domain(website),
        category=(payload.get("primaryType") or "").strip() or None,
        rating=float(rating) if isinstance(rating, (int, float)) else None,
        review_count=int(review_count) if isinstance(review_count, int) else None,
        # A minimal digest, not a response dump: Google's terms restrict caching
        # Places content, so we keep only the masked fields we already read.
        raw={
            "types": payload.get("primaryType"),
            "formattedAddress": payload.get("formattedAddress"),
            "rating": rating,
            "userRatingCount": review_count,
        },
    )


class PlacesDiscoveryService:
    """Google Places API (New) client + the discovery/enrichment/import chain."""

    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def api_key(self) -> str:
        # Read through rather than cached at __init__: tests (and a settings
        # reload) change the key after the singleton exists.
        return get_settings().google_places_api_key

    @property
    def base_url(self) -> str:
        return get_settings().google_places_base_url

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ── HTTP ────────────────────────────────────────────────────────────────

    @staticmethod
    def _retry_after(response: httpx.Response) -> Optional[float]:
        """Honour Google's own backoff hint when it sends one, clamped so a
        hostile or absurd value can't stall the worker."""
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return min(value, 60.0) if value >= 0 else None

    async def _request(
        self,
        db: Optional[AsyncSession],
        path: str,
        *,
        field_mask: str,
        operation: str,
        body: Optional[dict] = None,
        run_id: Optional[UUID] = None,
    ) -> PlacesCallResult:
        """Call Places under the credit gate, rate limiter and backoff.

        POSTs when ``body`` is given (searchText), GETs otherwise (details).

        The gate runs **before** the call, so an exhausted budget spends
        nothing. Every attempt that reaches Google is written to the ledger,
        including 429s and errors — a gate that ignores failed calls is a gate
        an outage walks straight through.

        ``db`` may be None for callers with no session; the ledger and gate are
        then skipped and the call is simply unmetered.
        """
        if not self.api_key:
            return PlacesCallResult(status="skipped", error="no_api_key")

        if db is not None:
            gate = await api_credits.check_budget(db, PROVIDER, cost=1)
            if not gate.allowed:
                return PlacesCallResult(status="budget_exhausted", error=gate.reason)

        await _limiter.acquire(self.settings.google_places_max_requests_per_minute)

        headers = {
            "Content-Type": "application/json",
            # The key rides in a header, never a query string.
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": field_mask,
        }
        url = f"{self.base_url}{path}"

        attempts = 0
        last_error: Optional[str] = None
        status = "error"
        data: Optional[dict] = None

        for attempt in range(1, PLACES_MAX_ATTEMPTS + 1):
            attempts = attempt
            try:
                async with httpx.AsyncClient(timeout=PLACES_TIMEOUT) as client:
                    if body is not None:
                        response = await client.post(url, headers=headers, json=body)
                    else:
                        response = await client.get(url, headers=headers)

                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    status = "rate_limited" if response.status_code == 429 else "error"
                    if attempt == PLACES_MAX_ATTEMPTS:
                        break
                    delay = self._retry_after(response)
                    if delay is None:
                        # Exponential with jitter, so a fleet of workers doesn't
                        # retry in lockstep and re-trigger the limit.
                        delay = PLACES_BACKOFF_BASE * (2 ** (attempt - 1))
                        delay *= 1 + random.random()
                    await _sleep(delay)
                    continue

                # Other 4xx won't improve on retry (bad key, bad mask, malformed
                # query). Fail fast rather than spend the retries.
                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}"
                    status = "error"
                    break

                data = response.json()
                status = "ok"
                break
            except Exception as e:
                last_error = str(e) or e.__class__.__name__
                status = "error"
                if attempt == PLACES_MAX_ATTEMPTS:
                    break
                await _sleep(PLACES_BACKOFF_BASE * (2 ** (attempt - 1)))
                continue

        # Google bills per request, not per result, and reports no per-call cost
        # in the response — so charge 1 per attempt that reached it. The ledger
        # is a guardrail, not billing truth.
        credits = attempts
        if db is not None:
            await api_credits.record_usage(
                db,
                PROVIDER,
                operation,
                credits=credits,
                status=status if status in ("ok", "rate_limited") else "error",
                run_id=run_id,
                details={
                    "path": path,
                    "attempts": attempts,
                    "error": last_error,
                    "field_mask": field_mask,
                },
            )
        if status != "ok":
            logger.warning(
                "Places %s %s after %d attempt(s): %s",
                operation, status, attempts, last_error,
            )
        return PlacesCallResult(
            data=data, status=status, attempts=attempts, credits=credits, error=last_error
        )

    # ── search ──────────────────────────────────────────────────────────────

    def _build_body(
        self,
        query: str,
        *,
        location: Optional[str],
        radius_km: Optional[float],
        lat: Optional[float],
        lng: Optional[float],
        business_type: Optional[str],
        page_size: int,
        page_token: Optional[str],
    ) -> dict:
        """Build a searchText request body.

        Location goes into the text query rather than through a geocoder:
        Places resolves "Pune, India" better than we would, and it costs no
        extra call. A lat/lng pair additionally biases a circle.
        """
        text_query = query.strip()
        if location and location.strip():
            text_query = f"{text_query} in {location.strip()}"
        body: dict = {"textQuery": text_query, "pageSize": page_size}
        if business_type:
            body["includedType"] = business_type
        if lat is not None and lng is not None:
            # Radius without a centre is meaningless to Places — it ignores it.
            # Default rather than pretend a bare radius filtered anything.
            radius_m = float(radius_km) * 1000.0 if radius_km else 5000.0
            body["locationBias"] = {
                "circle": {
                    "center": {"latitude": float(lat), "longitude": float(lng)},
                    # Places rejects a circle radius outside (0, 50000].
                    "radius": max(1.0, min(radius_m, 50000.0)),
                }
            }
        if page_token:
            # Google requires the original query alongside the token.
            body["pageToken"] = page_token
        return body

    async def search_businesses(
        self,
        db: Optional[AsyncSession],
        query: str,
        *,
        location: Optional[str] = None,
        radius_km: Optional[float] = None,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
        business_type: Optional[str] = None,
        limit: Optional[int] = None,
        country: Optional[str] = None,
        run_id: Optional[UUID] = None,
    ) -> PlacesSearchResult:
        """Text-search Places, following ``nextPageToken`` up to ``limit``.

        Places returns 20 results/page and **60 per query, hard**. Broad
        coverage therefore needs many narrow queries rather than one deep one —
        which is exactly why every page is metered. Stops at the limit, the
        ceiling, token exhaustion, or the budget, whichever comes first, and
        says which in ``status``/``truncated``.
        """
        s = self.settings
        ceiling = s.google_places_max_results
        limit = min(limit or ceiling, ceiling)
        result = PlacesSearchResult()

        if not self.api_key:
            result.status = "skipped"
            result.error = "no_api_key"
            return result
        if not (query or "").strip():
            result.status = "error"
            result.error = "query is required"
            return result

        # Bound the loop by pages as well as by results. The result count alone
        # can't terminate it: an empty page carrying a nextPageToken would spin
        # forever, paying for every lap.
        max_pages = -(-ceiling // max(1, s.google_places_page_size))
        page_token: Optional[str] = None
        while len(result.results) < limit and result.pages_fetched < max_pages:
            remaining = limit - len(result.results)
            body = self._build_body(
                query,
                location=location,
                radius_km=radius_km,
                lat=lat,
                lng=lng,
                business_type=business_type,
                page_size=min(remaining, s.google_places_page_size),
                page_token=page_token,
            )
            call = await self._request(
                db,
                "/places:searchText",
                field_mask=SEARCH_FIELD_MASK,
                operation="text_search",
                body=body,
                run_id=run_id,
            )
            result.credits_spent += call.credits
            if not call.ok:
                # Budget or rate limit mid-pagination: keep the pages we already
                # paid for, and report *why* we stopped rather than returning a
                # short list that reads as a complete one.
                result.status = call.status
                result.error = call.error
                result.truncated = True
                if result.results:
                    logger.warning(
                        "Places search '%s' stopped after %d result(s): %s",
                        query, len(result.results), call.status,
                    )
                return result

            result.pages_fetched += 1
            payload = call.data or {}
            for raw in payload.get("places") or []:
                place = parse_place(raw, default_country=country)
                if place is not None:
                    result.results.append(place)

            page_token = payload.get("nextPageToken")
            if not page_token:
                return result

        # We hit the limit with pages still available.
        result.truncated = bool(page_token)
        if result.truncated:
            logger.info(
                "Places search '%s' stopped at limit %d with more pages available",
                query, limit,
            )
        return result

    async def get_place_details(
        self, db: Optional[AsyncSession], place_id: str
    ) -> Optional[PlaceResult]:
        """Fetch one place by ID.

        Text search already returns every masked field, so this is for
        refreshing a staged business rather than a required second hop — a
        details call per search result would double the bill for nothing.
        """
        place_id = (place_id or "").strip()
        if not place_id:
            return None
        call = await self._request(
            db,
            f"/places/{place_id}",
            field_mask=DETAILS_FIELD_MASK,
            operation="place_details",
        )
        if not call.ok:
            return None
        return parse_place(call.data or {})

    # ── discovery ───────────────────────────────────────────────────────────

    async def discover(
        self,
        db: AsyncSession,
        search: DiscoverySearch,
        *,
        limit: Optional[int] = None,
    ) -> DiscoveryOutcome:
        """Run a saved search and stage what it finds. Flushes, never commits.

        Dedup is on ``place_id``: a weekly re-run of the same search re-surfaces
        the same businesses, and re-staging them would flood the review list
        with rows an operator already dispositioned.
        """
        outcome = DiscoveryOutcome()
        found = await self.search_businesses(
            db,
            search.query,
            location=search.location_text,
            radius_km=search.radius_km,
            lat=search.lat,
            lng=search.lng,
            business_type=search.business_type,
            limit=limit,
            country=search.country,
        )
        outcome.status = found.status
        outcome.credits_spent = found.credits_spent
        outcome.truncated = found.truncated
        outcome.error = found.error

        for place in found.results:
            existing = (
                await db.execute(
                    select(DiscoveredBusiness).where(
                        DiscoveredBusiness.place_id == place.place_id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                outcome.duplicates += 1
                outcome.businesses.append(existing)
                continue
            business = self._stage(place, search)
            db.add(business)
            outcome.businesses.append(business)
            outcome.created += 1

        if outcome.created:
            await db.flush()
        return outcome

    def _stage(self, place: PlaceResult, search: Optional[DiscoverySearch]) -> DiscoveredBusiness:
        return DiscoveredBusiness(
            place_id=place.place_id,
            business_name=place.name,
            address=place.address,
            city=place.city,
            state=place.state,
            country=place.country or (search.country if search else None),
            phone=place.phone,
            website=place.website,
            domain=place.domain,
            category=place.category or (search.business_type if search else None),
            rating=place.rating,
            review_count=place.review_count,
            status="new",
            discovered_via=PROVIDER,
            search_query=search.query if search else None,
            search_id=search.id if search else None,
            raw=place.raw,
        )

    # ── email extraction ────────────────────────────────────────────────────

    async def extract_email(
        self, db: AsyncSession, business: DiscoveredBusiness
    ) -> dict:
        """Recover an email for a business that has a website but no address.

        Hunter's domain-search first (it returns ranked, scored, role-aware
        addresses), then the existing scraper as a free fallback. Both are
        best-effort — a business we can't find an email for stays staged with a
        phone number, which is still worth something to the call queue.

        The address this returns is a *candidate*: it still has to survive P3's
        verification before anyone sends to it.
        """
        if business.email:
            return {"status": "skipped", "reason": "already_has_email"}
        if not business.domain:
            return {"status": "skipped", "reason": "no_website"}

        from app.services.hunter_verification import get_hunter_service

        hunter = get_hunter_service()
        if hunter.enabled:
            found = await hunter.domain_search(db, business.domain)
            if found.get("email"):
                business.email = found["email"]
                business.email_source = "hunter"
                business.email_confidence = found.get("score")
                business.status = "enriched"
                # Keep the person, not just the address. Hunter ranks a
                # *personal* address first and returns the name attached to it;
                # dropping that is what left every Places-sourced contact
                # nameless and every cold email opening "Hi there,". The name
                # belongs to the winning address, so read it off the same entry
                # rather than the top of the unranked list.
                best = next(
                    (e for e in (found.get("emails") or []) if e.get("email") == found["email"]),
                    None,
                )
                if best:
                    business.contact_first_name = (best.get("first_name") or None)
                    business.contact_last_name = (best.get("last_name") or None)
                    business.contact_title = (best.get("position") or None)
                await db.flush()
                return {
                    "status": "enriched",
                    "source": "hunter",
                    "email": business.email,
                    "score": found.get("score"),
                    "named": bool(business.contact_first_name),
                }

        # Free fallback. The scraper drops only automated/technical inboxes
        # (noreply, postmaster, …) and ranks any personal-looking address ahead
        # of a generic one, so ``emails[0]`` is the best sendable candidate it
        # found — which for most small shops is a monitored ``info@``/``sales@``
        # box, and one of those beats importing a business with no contact at
        # all.
        from app.services.web_scraper import get_web_scraper

        try:
            scraped = await get_web_scraper().scrape_company(business.domain)
        except Exception as e:  # pragma: no cover - scraper is defensive already
            logger.warning("Scrape failed for %s: %s", business.domain, e)
            return {"status": "error", "reason": str(e)}

        emails = [e for e in (scraped.get("emails") or []) if e and "@" in e]
        if not emails:
            return {"status": "not_found", "reason": scraped.get("error") or "no_emails"}

        business.email = emails[0].strip().lower()
        business.email_source = "scrape"
        business.status = "enriched"
        await db.flush()
        return {"status": "enriched", "source": "scrape", "email": business.email}

    # ── import ──────────────────────────────────────────────────────────────

    def to_candidate(self, business: DiscoveredBusiness) -> CandidateRecord:
        """Shape a staged business into the record every source ingests through.

        A contact is attached **only** when there's an email. Places gives us no
        people, so a business without one has nobody to be — creating a contact
        anyway would put an un-emailable row into the sequence's line of sight.
        """
        contacts: list[ContactRecord] = []
        if business.email:
            contacts.append(
                ContactRecord(
                    email=business.email,
                    phone=business.phone,
                    # Present only when the enrichment hop named a person (in
                    # practice, Hunter). None here is honest — it becomes a
                    # nameless contact, which the copy layer renders as a
                    # generic greeting rather than a broken merge field.
                    first_name=business.contact_first_name,
                    last_name=business.contact_last_name,
                    title=business.contact_title,
                    source=PROVIDER,
                    raw={"email_source": business.email_source},
                )
            )
        return CandidateRecord(
            name=business.business_name,
            domain=business.domain,
            website=business.website,
            country=business.country,
            city=business.city,
            state=business.state,
            phone=business.phone,
            # The search query, not the Places category, is the industry signal.
            # Places speaks its own taxonomy ('manufacturer', 'hardware_store')
            # and none of those strings match an ICP segment like 'CNC
            # Machining', so scoring off `category` scores every business 0/40
            # for industry and caps it at 38 — under the review line, which is
            # why nothing Places found ever got looked at. The query is what
            # surfaced this business, and it is an ICP term by construction, so
            # it is both the truer signal and the better `industry_segment`.
            industry=business.search_query or business.category,
            # Places has no employee count or revenue — the two things the ICP
            # scores hardest on. Left None so `qualify()` treats size as
            # unknown rather than as zero.
            employee_count=None,
            contacts=contacts,
            source=PROVIDER,
            raw={
                "place_id": business.place_id,
                # Kept because `industry` no longer carries it.
                "places_category": business.category,
                **(business.raw or {}),
            },
        )

    async def import_business(
        self,
        db: AsyncSession,
        business: DiscoveredBusiness,
        *,
        icp=None,
        enroll_budget: int = 0,
        score: bool = True,
    ):
        """Import a staged business through the one shared ingestion pipeline.

        Company-level dedup, contact-level dedup, suppression, scoring and
        routing all belong to ``IngestionService`` — this only maps the staged
        row onto a ``CandidateRecord`` and records where it landed.

        Goes through the batch ``ingest`` rather than ``ingest_one`` for its
        error handling: ``ingest_one`` lets an ``IngestionError`` propagate, and
        the most likely one here is entirely expected — a business in a country
        outside ``settings.supported_countries``. That's a routine "can't import
        this yet", not a 500, and it must surface as an outcome the caller can
        record on the row.
        """
        from app.services.ingestion import get_ingestion_service

        result = await get_ingestion_service().ingest(
            db,
            [self.to_candidate(business)],
            icp=icp,
            enroll_budget=enroll_budget,
            score=score,
        )
        return result.outcomes[0]


_places: Optional[PlacesDiscoveryService] = None


def get_places_service() -> PlacesDiscoveryService:
    global _places
    if _places is None:
        _places = PlacesDiscoveryService()
    return _places
