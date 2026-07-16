"""Tests for P4 — Google Places SMB discovery.

Covers the properties that decide whether this capability is safe and honest:

* **The credit gate blocks before the call.** Coverage means many narrow queries
  and each page is billed; a gate checked after the fact is accounting, not a
  budget.
* **Rate-limited is not empty.** A 429 that outlives its retries must report
  ``rate_limited``, never a bare zero that reads as "no businesses there".
* **Field masks are pinned.** Google bills Places (New) by which fields you
  request, so a widened mask is a silent price rise — the test is a cost
  regression guard.
* **Pagination stops honestly** — at the limit, at Google's 60-result ceiling,
  or at the budget, and says which.
* **Places yields no people.** A business with no email must not produce an
  un-emailable contact.
* **An unsupported country is rejected, not relabelled** — the old paths coerced
  anything non-US/CA to ``US``, quietly turning an Indian company American.

All network is faked; no test touches Google, Hunter, or a real website.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.api_credit import ApiCreditLedger
from app.models.company import Company
from app.models.contact import Contact
from app.models.discovered_business import DiscoveredBusiness
from app.models.discovery_search import DiscoverySearch
from app.services import places_discovery
from app.services.places_discovery import (
    DETAILS_FIELD_MASK,
    SEARCH_FIELD_MASK,
    PlacesDiscoveryService,
    get_places_service,
    parse_place,
)


# ─── fakes ───────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code=200, data=None, headers=None):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._data


class _FakeClient:
    """Stands in for httpx.AsyncClient: records calls, replays scripted responses.

    ``responses`` is consumed left-to-right; the final entry repeats, so one
    scripted response serves any number of calls.
    """

    calls: list = []
    responses: list = []
    raise_exc: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def _next(self):
        if _FakeClient.raise_exc is not None:
            raise _FakeClient.raise_exc
        if not _FakeClient.responses:
            return _FakeResponse(200, {"places": []})
        if len(_FakeClient.responses) > 1:
            return _FakeClient.responses.pop(0)
        return _FakeClient.responses[0]

    async def post(self, url, headers=None, json=None):
        _FakeClient.calls.append(
            {"method": "POST", "url": url, "headers": headers or {}, "json": json or {}}
        )
        return self._next()

    async def get(self, url, headers=None):
        _FakeClient.calls.append({"method": "GET", "url": url, "headers": headers or {}})
        return self._next()


def _place(
    place_id="place-1",
    name="Shree Precision Molding",
    *,
    website="https://shreemolding.in",
    phone="+91 20 1234 5678",
    country="IN",
    city="Pune",
    state="MH",
    rating=4.4,
    reviews=37,
):
    """A Places payload shaped the way the API actually returns one."""
    payload = {
        "id": place_id,
        "displayName": {"text": name, "languageCode": "en"},
        "formattedAddress": f"12 MIDC Rd, {city}, {state}, India",
        "addressComponents": [
            {"longText": city, "shortText": city, "types": ["locality", "political"]},
            {
                "longText": "Maharashtra",
                "shortText": state,
                "types": ["administrative_area_level_1", "political"],
            },
            {"longText": "India", "shortText": country, "types": ["country", "political"]},
        ],
        "primaryType": "manufacturer",
        "rating": rating,
        "userRatingCount": reviews,
    }
    if website:
        payload["websiteUri"] = website
    if phone:
        payload["internationalPhoneNumber"] = phone
    return payload


def _page(places, next_token=None):
    body = {"places": places}
    if next_token:
        body["nextPageToken"] = next_token
    return _FakeResponse(200, body)


@pytest.fixture(autouse=True)
def _fake_http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.responses = []
    _FakeClient.raise_exc = None
    monkeypatch.setattr(places_discovery.httpx, "AsyncClient", _FakeClient)
    # Keep retry backoff from making the suite slow.
    monkeypatch.setattr(places_discovery, "PLACES_BACKOFF_BASE", 0.001)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(places_discovery, "_sleep", _no_sleep)
    # The token bucket is module-level and shared, so without a reset a long
    # suite would eventually make tests wait on a real clock.
    places_discovery.reset_rate_limiter()
    yield
    _FakeClient.calls = []
    _FakeClient.responses = []
    _FakeClient.raise_exc = None
    places_discovery.reset_rate_limiter()


@pytest.fixture
def places_on(monkeypatch):
    """Configure the capability on.

    The service reads the cached Settings singleton, so patch that rather than
    an instance.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "google_places_api_key", "test-places-key")
    monkeypatch.setattr(settings, "google_places_base_url", "https://places.googleapis.com/v1")
    monkeypatch.setattr(settings, "google_places_daily_budget", 100)
    monkeypatch.setattr(settings, "google_places_monthly_budget", 2000)
    monkeypatch.setattr(settings, "google_places_max_requests_per_minute", 0)
    monkeypatch.setattr(settings, "google_places_page_size", 20)
    monkeypatch.setattr(settings, "google_places_max_results", 60)
    monkeypatch.setattr(settings, "supported_countries", ["US", "CA", "IN"])
    return settings


@pytest.fixture
def places_off(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_places_api_key", "")
    return settings


async def _mk_search(db, **kw) -> DiscoverySearch:
    search = DiscoverySearch(
        id=uuid.uuid4(),
        name=kw.pop("name", "Pune molders"),
        query=kw.pop("query", "injection molding manufacturers"),
        location_text=kw.pop("location_text", "Pune, India"),
        country=kw.pop("country", "IN"),
        schedule=kw.pop("schedule", "weekly"),
        **kw,
    )
    db.add(search)
    await db.commit()
    return search


async def _mk_business(db, **kw) -> DiscoveredBusiness:
    business = DiscoveredBusiness(
        id=uuid.uuid4(),
        place_id=kw.pop("place_id", f"place-{uuid.uuid4().hex[:8]}"),
        business_name=kw.pop("business_name", "Shree Precision Molding"),
        country=kw.pop("country", "IN"),
        city=kw.pop("city", "Pune"),
        state=kw.pop("state", "MH"),
        phone=kw.pop("phone", "+912012345678"),
        website=kw.pop("website", "https://shreemolding.in"),
        domain=kw.pop("domain", "shreemolding.in"),
        category=kw.pop("category", "manufacturer"),
        status=kw.pop("status", "new"),
        discovered_via="google_places",
        **kw,
    )
    db.add(business)
    await db.commit()
    return business


# ─── field masks: the cost regression guard ──────────────────────────────────

def test_search_field_mask_is_exactly_the_fields_we_read():
    """Google bills Places (New) by SKU tier, and the tier is set by the mask.

    Pinned rather than sanity-checked: a field added here is a price rise, so it
    should have to be a deliberate edit to this test too.
    """
    assert SEARCH_FIELD_MASK.split(",") == [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.addressComponents",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.primaryType",
        "nextPageToken",
    ]


def test_details_field_mask_is_unprefixed():
    """searchText masks are prefixed ``places.``; the details mask is not.

    Getting this backwards is a 400 from Google, not a silent overcharge — but
    it's the kind of thing that only shows up against the live API.
    """
    assert "places." not in DETAILS_FIELD_MASK
    assert DETAILS_FIELD_MASK.startswith("id,displayName")
    assert "nextPageToken" not in DETAILS_FIELD_MASK


def test_field_mask_excludes_expensive_unused_fields():
    """Fields we don't read must not be billed for."""
    for pricey in ("reviews", "photos", "editorialSummary", "regularOpeningHours",
                   "priceLevel", "servesBeer", "currentOpeningHours"):
        assert pricey not in SEARCH_FIELD_MASK
        assert pricey not in DETAILS_FIELD_MASK


@pytest.mark.asyncio
async def test_field_mask_is_sent_as_a_header(places_on):
    _FakeClient.responses = [_page([_place()])]
    await get_places_service().search_businesses(None, "molders")
    headers = _FakeClient.calls[0]["headers"]
    assert headers["X-Goog-FieldMask"] == SEARCH_FIELD_MASK
    # The key rides in a header, never a query string.
    assert headers["X-Goog-Api-Key"] == "test-places-key"
    assert "test-places-key" not in _FakeClient.calls[0]["url"]


# ─── parsing ─────────────────────────────────────────────────────────────────

def test_parse_place_extracts_core_fields():
    place = parse_place(_place())
    assert place.place_id == "place-1"
    assert place.name == "Shree Precision Molding"
    assert place.city == "Pune"
    assert place.category == "manufacturer"
    assert place.rating == 4.4
    assert place.review_count == 37


def test_parse_place_takes_iso_country_from_short_text():
    """Google puts 'IN' in shortText and 'India' in longText; everything
    downstream speaks ISO-3166-1 alpha-2."""
    place = parse_place(_place())
    assert place.country == "IN"
    assert place.state == "MH"


def test_parse_place_normalizes_website_to_domain():
    place = parse_place(_place(website="https://www.ShreeMolding.in/contact"))
    assert place.domain == "shreemolding.in"


def test_parse_place_without_id_or_name_is_dropped():
    """A place we can't identify is worse than no place at all."""
    assert parse_place({"displayName": {"text": "No ID"}}) is None
    assert parse_place({"id": "x"}) is None
    assert parse_place({}) is None


def test_parse_place_tolerates_missing_optional_fields():
    """Places omits fields freely. A KeyError here would lose a whole page."""
    place = parse_place({"id": "p", "displayName": {"text": "Bare Co"}})
    assert place.name == "Bare Co"
    assert place.website is None and place.domain is None
    assert place.phone is None and place.rating is None
    assert place.country is None


def test_parse_place_falls_back_to_search_country():
    """Places doesn't always return a country component."""
    payload = {"id": "p", "displayName": {"text": "Bare Co"}}
    assert parse_place(payload, default_country="IN").country == "IN"


def test_parse_place_raw_is_a_minimal_digest():
    """Google's terms restrict caching Places *content*, so we keep a digest of
    the masked fields we already read — not a response dump."""
    place = parse_place(_place())
    assert set(place.raw) == {"types", "formattedAddress", "rating", "userRatingCount"}


# ─── search: disabled / invalid ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_api_key_skips_without_calling(places_off):
    result = await get_places_service().search_businesses(None, "molders")
    assert result.status == "skipped"
    assert result.error == "no_api_key"
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_empty_query_is_an_error_not_a_call(places_on):
    result = await get_places_service().search_businesses(None, "   ")
    assert result.status == "error"
    assert _FakeClient.calls == []


# ─── search: request shape ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_location_is_folded_into_the_text_query(places_on):
    _FakeClient.responses = [_page([_place()])]
    await get_places_service().search_businesses(
        None, "injection molders", location="Pune, India"
    )
    assert _FakeClient.calls[0]["json"]["textQuery"] == "injection molders in Pune, India"


@pytest.mark.asyncio
async def test_business_type_becomes_included_type(places_on):
    _FakeClient.responses = [_page([_place()])]
    await get_places_service().search_businesses(None, "molders", business_type="manufacturer")
    assert _FakeClient.calls[0]["json"]["includedType"] == "manufacturer"


@pytest.mark.asyncio
async def test_lat_lng_builds_a_location_bias_circle(places_on):
    _FakeClient.responses = [_page([_place()])]
    await get_places_service().search_businesses(
        None, "molders", lat=18.52, lng=73.85, radius_km=10
    )
    circle = _FakeClient.calls[0]["json"]["locationBias"]["circle"]
    assert circle["center"] == {"latitude": 18.52, "longitude": 73.85}
    assert circle["radius"] == 10000.0


@pytest.mark.asyncio
async def test_radius_without_a_centre_is_not_sent(places_on):
    """Places ignores a radius with no centre. Sending one anyway would let the
    caller believe it filtered."""
    _FakeClient.responses = [_page([_place()])]
    await get_places_service().search_businesses(None, "molders", radius_km=10)
    assert "locationBias" not in _FakeClient.calls[0]["json"]


@pytest.mark.asyncio
async def test_circle_radius_is_clamped_to_the_api_maximum(places_on):
    _FakeClient.responses = [_page([_place()])]
    await get_places_service().search_businesses(
        None, "molders", lat=18.52, lng=73.85, radius_km=500
    )
    circle = _FakeClient.calls[0]["json"]["locationBias"]["circle"]
    assert circle["radius"] == 50000.0  # Places rejects anything above this


# ─── search: results & pagination ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_successful_search_returns_normalized_results(places_on):
    _FakeClient.responses = [_page([_place("a"), _place("b")])]
    result = await get_places_service().search_businesses(None, "molders")
    assert result.status == "ok"
    assert [p.place_id for p in result.results] == ["a", "b"]
    assert result.pages_fetched == 1
    assert result.truncated is False


@pytest.mark.asyncio
async def test_pagination_follows_next_page_token(places_on):
    _FakeClient.responses = [
        _page([_place(f"p{i}") for i in range(20)], next_token="tok-1"),
        _page([_place(f"q{i}") for i in range(5)]),
    ]
    result = await get_places_service().search_businesses(None, "molders")
    assert len(result.results) == 25
    assert result.pages_fetched == 2
    # Google requires the original query alongside the token.
    assert _FakeClient.calls[1]["json"]["pageToken"] == "tok-1"
    assert _FakeClient.calls[1]["json"]["textQuery"] == "molders"


@pytest.mark.asyncio
async def test_pagination_stops_at_the_60_result_ceiling(places_on):
    """Places hard-caps a query at 60. Broad coverage needs many narrow queries,
    not one deep one."""
    _FakeClient.responses = [
        _page([_place(f"a{i}") for i in range(20)], next_token="t1"),
        _page([_place(f"b{i}") for i in range(20)], next_token="t2"),
        _page([_place(f"c{i}") for i in range(20)], next_token="t3"),
    ]
    result = await get_places_service().search_businesses(None, "molders")
    assert len(result.results) == 60
    assert result.pages_fetched == 3
    # More pages were on offer; the drop is reported, not silent.
    assert result.truncated is True


@pytest.mark.asyncio
async def test_pagination_stops_at_the_requested_limit(places_on):
    _FakeClient.responses = [_page([_place(f"p{i}") for i in range(5)], next_token="t1")]
    result = await get_places_service().search_businesses(None, "molders", limit=5)
    assert len(result.results) == 5
    assert _FakeClient.calls[0]["json"]["pageSize"] == 5
    assert len(_FakeClient.calls) == 1


@pytest.mark.asyncio
async def test_no_next_page_token_ends_pagination(places_on):
    _FakeClient.responses = [_page([_place("a")])]
    result = await get_places_service().search_businesses(None, "molders", limit=60)
    assert len(_FakeClient.calls) == 1
    assert result.truncated is False


@pytest.mark.asyncio
async def test_empty_page_with_a_token_cannot_loop_forever(places_on):
    """The result count alone can't terminate pagination: an empty page that
    still carries a nextPageToken would spin, paying for every lap."""
    _FakeClient.responses = [_page([], next_token="always-more")]
    result = await get_places_service().search_businesses(None, "molders", limit=60)
    assert result.results == []
    # Bounded by pages: 60-result ceiling / 20 per page.
    assert len(_FakeClient.calls) == 3


@pytest.mark.asyncio
async def test_unparseable_place_is_skipped_not_fatal(places_on):
    _FakeClient.responses = [_page([_place("good"), {"garbage": True}])]
    result = await get_places_service().search_businesses(None, "molders")
    assert [p.place_id for p in result.results] == ["good"]


# ─── search: rate limits & errors ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_429_retries_then_succeeds(places_on):
    _FakeClient.responses = [
        _FakeResponse(429, headers={"Retry-After": "0"}),
        _page([_place("a")]),
    ]
    result = await get_places_service().search_businesses(None, "molders")
    assert result.status == "ok"
    assert len(result.results) == 1
    assert len(_FakeClient.calls) == 2


@pytest.mark.asyncio
async def test_exhausted_429_is_rate_limited_not_empty(places_on):
    """The distinction this whole status field exists for: an outage must not
    read as 'that market has no businesses in it'."""
    _FakeClient.responses = [_FakeResponse(429, headers={"Retry-After": "0"})]
    result = await get_places_service().search_businesses(None, "molders")
    assert result.status == "rate_limited"
    assert result.results == []
    assert result.truncated is True
    assert len(_FakeClient.calls) == places_discovery.PLACES_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_4xx_fails_fast_without_burning_retries(places_on):
    """A bad key or a bad mask won't improve on retry."""
    _FakeClient.responses = [_FakeResponse(403, {"error": {"message": "bad key"}})]
    result = await get_places_service().search_businesses(None, "molders")
    assert result.status == "error"
    assert len(_FakeClient.calls) == 1


@pytest.mark.asyncio
async def test_5xx_retries_then_reports_error(places_on):
    _FakeClient.responses = [_FakeResponse(503)]
    result = await get_places_service().search_businesses(None, "molders")
    assert result.status == "error"
    assert len(_FakeClient.calls) == places_discovery.PLACES_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_network_exception_never_raises(places_on):
    _FakeClient.raise_exc = RuntimeError("connection reset")
    result = await get_places_service().search_businesses(None, "molders")
    assert result.status == "error"
    assert "connection reset" in (result.error or "")


@pytest.mark.asyncio
async def test_pages_already_paid_for_survive_a_mid_pagination_failure(places_on):
    """Stopping early must keep what we bought and say why we stopped."""
    _FakeClient.responses = [
        _page([_place(f"a{i}") for i in range(20)], next_token="t1"),
        _FakeResponse(429, headers={"Retry-After": "0"}),
    ]
    result = await get_places_service().search_businesses(None, "molders")
    assert len(result.results) == 20
    assert result.status == "rate_limited"
    assert result.truncated is True


# ─── the credit gate ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_credit_gate_blocks_before_the_call(places_on, db_session: AsyncSession, monkeypatch):
    """The single most important test here: an exhausted budget costs zero.

    Debiting after the fact is accounting; refusing beforehand is a budget.
    """
    monkeypatch.setattr(get_settings(), "google_places_daily_budget", 5)
    db_session.add(
        ApiCreditLedger(
            id=uuid.uuid4(), provider="google_places", operation="text_search",
            credits=5, status="ok",
        )
    )
    await db_session.commit()

    _FakeClient.responses = [_page([_place()])]
    result = await get_places_service().search_businesses(db_session, "molders")

    assert result.status == "budget_exhausted"
    assert _FakeClient.calls == []  # nothing spent
    assert result.credits_spent == 0


@pytest.mark.asyncio
async def test_places_budget_is_actually_wired_to_the_gate(places_on, db_session, monkeypatch):
    """A provider with no branch in _budgets_for is silently unlimited — which
    would make every other gate test here pass against a no-op."""
    from app.services import api_credits

    monkeypatch.setattr(get_settings(), "google_places_daily_budget", 7)
    monkeypatch.setattr(get_settings(), "google_places_monthly_budget", 70)
    status = await api_credits.get_budget_status(db_session, "google_places", cost=1)
    assert status.daily_budget == 7
    assert status.monthly_budget == 70


@pytest.mark.asyncio
async def test_ledger_debits_on_success(places_on, db_session: AsyncSession):
    _FakeClient.responses = [_page([_place()])]
    await get_places_service().search_businesses(db_session, "molders")
    rows = (
        await db_session.execute(
            select(ApiCreditLedger).where(ApiCreditLedger.provider == "google_places")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].operation == "text_search"
    assert rows[0].status == "ok"
    assert rows[0].credits == 1


@pytest.mark.asyncio
async def test_ledger_debits_on_rate_limit(places_on, db_session: AsyncSession):
    """A 429 may still be billed, and a gate that ignores failed calls is a gate
    an outage walks straight through."""
    _FakeClient.responses = [_FakeResponse(429, headers={"Retry-After": "0"})]
    await get_places_service().search_businesses(db_session, "molders")
    row = (
        await db_session.execute(
            select(ApiCreditLedger).where(ApiCreditLedger.provider == "google_places")
        )
    ).scalars().one()
    assert row.status == "rate_limited"
    assert row.credits == places_discovery.PLACES_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_budget_gate_is_checked_per_page_not_per_search(
    places_on, db_session: AsyncSession, monkeypatch
):
    """Pagination multiplies spend, so the gate has to bound pages, not runs."""
    monkeypatch.setattr(get_settings(), "google_places_daily_budget", 2)
    _FakeClient.responses = [
        _page([_place(f"a{i}") for i in range(20)], next_token="t1"),
        _page([_place(f"b{i}") for i in range(20)], next_token="t2"),
        _page([_place(f"c{i}") for i in range(20)], next_token="t3"),
    ]
    result = await get_places_service().search_businesses(db_session, "molders")
    # Two pages fit the budget; the third is refused before it costs anything.
    assert result.pages_fetched == 2
    assert len(result.results) == 40
    assert result.status == "budget_exhausted"
    assert result.truncated is True
    assert len(_FakeClient.calls) == 2


# ─── place details ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_place_details_returns_a_parsed_place(places_on, db_session):
    _FakeClient.responses = [_FakeResponse(200, _place("place-9", "Detail Co"))]
    place = await get_places_service().get_place_details(db_session, "place-9")
    assert place.place_id == "place-9"
    assert place.name == "Detail Co"
    call = _FakeClient.calls[0]
    assert call["method"] == "GET"
    assert call["url"].endswith("/places/place-9")
    assert call["headers"]["X-Goog-FieldMask"] == DETAILS_FIELD_MASK


@pytest.mark.asyncio
async def test_get_place_details_empty_id_is_not_a_call(places_on, db_session):
    assert await get_places_service().get_place_details(db_session, "  ") is None
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_get_place_details_failure_returns_none(places_on, db_session):
    _FakeClient.responses = [_FakeResponse(404, {})]
    assert await get_places_service().get_place_details(db_session, "gone") is None


# ─── discover: staging & dedup ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discover_stages_businesses(places_on, db_session: AsyncSession):
    search = await _mk_search(db_session)
    _FakeClient.responses = [_page([_place("a"), _place("b", "Second Co")])]
    outcome = await get_places_service().discover(db_session, search)
    await db_session.commit()

    assert outcome.created == 2
    assert outcome.status == "ok"
    rows = (await db_session.execute(select(DiscoveredBusiness))).scalars().all()
    assert len(rows) == 2
    staged = {r.place_id: r for r in rows}
    assert staged["a"].search_id == search.id
    assert staged["a"].search_query == search.query
    assert staged["a"].status == "new"
    assert staged["a"].discovered_via == "google_places"
    assert staged["a"].domain == "shreemolding.in"


@pytest.mark.asyncio
async def test_place_id_dedup_prevents_restaging_on_rerun(
    places_on, db_session: AsyncSession
):
    """A weekly re-run re-surfaces the same businesses. Re-staging them would
    flood the list with rows an operator already dispositioned."""
    search = await _mk_search(db_session)
    _FakeClient.responses = [_page([_place("a"), _place("b", "Second Co")])]
    await get_places_service().discover(db_session, search)
    await db_session.commit()

    _FakeClient.responses = [_page([_place("a"), _place("c", "Third Co")])]
    outcome = await get_places_service().discover(db_session, search)
    await db_session.commit()

    assert outcome.created == 1       # only 'c' is new
    assert outcome.duplicates == 1    # 'a' was already staged
    rows = (await db_session.execute(select(DiscoveredBusiness))).scalars().all()
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_discover_propagates_rate_limited_status(places_on, db_session: AsyncSession):
    search = await _mk_search(db_session)
    _FakeClient.responses = [_FakeResponse(429, headers={"Retry-After": "0"})]
    outcome = await get_places_service().discover(db_session, search)
    assert outcome.status == "rate_limited"
    assert outcome.created == 0


# ─── email extraction ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_extract_email_skips_business_without_website(places_on, db_session):
    """No website, no bridge to an email. Places gave us a phone number instead."""
    business = await _mk_business(db_session, website=None, domain=None)
    result = await get_places_service().extract_email(db_session, business)
    assert result["status"] == "skipped"
    assert result["reason"] == "no_website"


@pytest.mark.asyncio
async def test_extract_email_skips_business_that_already_has_one(places_on, db_session):
    business = await _mk_business(db_session, email="owner@shreemolding.in")
    result = await get_places_service().extract_email(db_session, business)
    assert result["status"] == "skipped"
    assert result["reason"] == "already_has_email"


@pytest.mark.asyncio
async def test_extract_email_prefers_hunter(places_on, db_session, monkeypatch):
    from app.services import hunter_verification

    monkeypatch.setattr(get_settings(), "hunter_api_key", "hk")

    async def _fake_domain_search(self, db, domain, **kw):
        return {"status": "ok", "email": "priya@shreemolding.in", "score": 92, "emails": []}

    monkeypatch.setattr(
        hunter_verification.HunterVerificationService, "domain_search", _fake_domain_search
    )
    business = await _mk_business(db_session)
    result = await get_places_service().extract_email(db_session, business)

    assert result["status"] == "enriched"
    assert result["source"] == "hunter"
    assert business.email == "priya@shreemolding.in"
    assert business.email_source == "hunter"
    assert business.email_confidence == 92
    assert business.status == "enriched"


@pytest.mark.asyncio
async def test_extract_email_falls_back_to_the_scraper(places_on, db_session, monkeypatch):
    """Hunter is the better source but it's paid and often empty for an SMB.
    The free scraper already exists — use it rather than give up."""
    from app.services import hunter_verification, web_scraper

    monkeypatch.setattr(get_settings(), "hunter_api_key", "hk")

    async def _empty_domain_search(self, db, domain, **kw):
        return {"status": "not_found", "email": None, "emails": []}

    async def _fake_scrape(self, domain):
        return {"emails": ["Owner@ShreeMolding.in"], "phones": []}

    monkeypatch.setattr(
        hunter_verification.HunterVerificationService, "domain_search", _empty_domain_search
    )
    monkeypatch.setattr(web_scraper.WebScraper, "scrape_company", _fake_scrape)

    business = await _mk_business(db_session)
    result = await get_places_service().extract_email(db_session, business)

    assert result["status"] == "enriched"
    assert result["source"] == "scrape"
    assert business.email == "owner@shreemolding.in"  # lowercased
    assert business.email_source == "scrape"


@pytest.mark.asyncio
async def test_extract_email_works_without_a_hunter_key(places_on, db_session, monkeypatch):
    """Hunter off must degrade to the scraper, not disable extraction."""
    from app.services import web_scraper

    monkeypatch.setattr(get_settings(), "hunter_api_key", "")

    async def _fake_scrape(self, domain):
        return {"emails": ["owner@shreemolding.in"], "phones": []}

    monkeypatch.setattr(web_scraper.WebScraper, "scrape_company", _fake_scrape)
    business = await _mk_business(db_session)
    result = await get_places_service().extract_email(db_session, business)
    assert result["status"] == "enriched"
    assert result["source"] == "scrape"


@pytest.mark.asyncio
async def test_extract_email_not_found_leaves_business_alone(places_on, db_session, monkeypatch):
    from app.services import web_scraper

    monkeypatch.setattr(get_settings(), "hunter_api_key", "")

    async def _empty_scrape(self, domain):
        return {"emails": [], "phones": []}

    monkeypatch.setattr(web_scraper.WebScraper, "scrape_company", _empty_scrape)
    business = await _mk_business(db_session)
    result = await get_places_service().extract_email(db_session, business)
    assert result["status"] == "not_found"
    assert business.email is None
    assert business.status == "new"


@pytest.mark.asyncio
async def test_hunter_domain_search_prefers_personal_over_generic(db_session, monkeypatch):
    """Hunter scores a catch-all info@ higher than a real person almost every
    time. Sorting on confidence alone would pick the shared inbox nobody reads —
    and it's exactly the address an accept_all domain bounces later."""
    from app.services import hunter_verification

    monkeypatch.setattr(get_settings(), "hunter_api_key", "hk")
    monkeypatch.setattr(get_settings(), "hunter_daily_credit_budget", 0)

    async def _fake_get(self, path, params):
        return (
            {
                "data": {
                    "emails": [
                        {"value": "info@shreemolding.in", "type": "generic", "confidence": 97},
                        {"value": "priya@shreemolding.in", "type": "personal", "confidence": 71},
                    ]
                }
            },
            None,
        )

    monkeypatch.setattr(hunter_verification.HunterVerificationService, "_get", _fake_get)
    result = await hunter_verification.get_hunter_service().domain_search(
        db_session, "shreemolding.in"
    )
    assert result["email"] == "priya@shreemolding.in"
    assert result["score"] == 71


# ─── candidate mapping ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_business_without_email_produces_no_contact(places_on, db_session):
    """Places gives us no people. A business with no email has nobody to be —
    creating a contact anyway puts an un-emailable row in the sequence's path."""
    business = await _mk_business(db_session, email=None)
    candidate = get_places_service().to_candidate(business)
    assert candidate.contacts == []


@pytest.mark.asyncio
async def test_business_with_email_produces_one_contact(places_on, db_session):
    business = await _mk_business(db_session, email="owner@shreemolding.in", email_source="hunter")
    candidate = get_places_service().to_candidate(business)
    assert len(candidate.contacts) == 1
    assert candidate.contacts[0].email == "owner@shreemolding.in"
    assert candidate.contacts[0].source == "google_places"


@pytest.mark.asyncio
async def test_candidate_leaves_employee_count_unknown(places_on, db_session):
    """Places has no employee count. None (unknown) rather than 0, which the ICP
    would score as a real, tiny company."""
    business = await _mk_business(db_session)
    candidate = get_places_service().to_candidate(business)
    assert candidate.employee_count is None
    assert candidate.source == "google_places"
    assert candidate.raw["place_id"] == business.place_id


# ─── import through the shared ingestion pipeline ────────────────────────────

@pytest.mark.asyncio
async def test_import_creates_an_indian_company(places_on, db_session: AsyncSession):
    """The whole point of the capability: an IN company inserts."""
    business = await _mk_business(db_session, email="owner@shreemolding.in")
    outcome = await get_places_service().import_business(db_session, business, score=False)
    await db_session.commit()

    assert outcome.error is None
    assert outcome.created is True
    company = (
        await db_session.execute(select(Company).where(Company.id == outcome.company.id))
    ).scalar_one()
    assert company.country == "IN"
    assert company.source == "google_places"
    assert company.domain == "shreemolding.in"


@pytest.mark.asyncio
async def test_import_rejects_unsupported_country_without_relabelling(
    places_on, db_session: AsyncSession
):
    """The old paths coerced anything non-US/CA to 'US', quietly turning a
    German company American. A rejected row is recoverable; a mislabelled one
    is not."""
    business = await _mk_business(db_session, country="DE", place_id="de-1")
    outcome = await get_places_service().import_business(db_session, business, score=False)

    assert outcome.error is not None
    assert "DE" in outcome.error
    assert outcome.company is None
    companies = (await db_session.execute(select(Company))).scalars().all()
    assert companies == []  # emphatically not created as a US company


@pytest.mark.asyncio
async def test_import_dedups_onto_an_existing_company_by_domain(
    places_on, db_session: AsyncSession, sample_company: Company
):
    """A business Apollo already found must not become a second company."""
    business = await _mk_business(
        db_session,
        business_name="Acme Stamping",
        website="https://acmestamp.com",
        domain="acmestamp.com",
        country="US",
        state="OH",
    )
    outcome = await get_places_service().import_business(db_session, business, score=False)
    await db_session.commit()

    assert outcome.created is False
    assert outcome.dedup.confidence == "certain"
    assert outcome.dedup.matched_on == "domain"
    assert outcome.company.id == sample_company.id


@pytest.mark.asyncio
async def test_import_of_emailless_business_creates_company_but_no_contact(
    places_on, db_session: AsyncSession
):
    business = await _mk_business(db_session, email=None)
    outcome = await get_places_service().import_business(db_session, business, score=False)
    await db_session.commit()

    assert outcome.company is not None
    assert outcome.contacts_created == 0
    contacts = (
        await db_session.execute(
            select(Contact).where(Contact.company_id == outcome.company.id)
        )
    ).scalars().all()
    assert contacts == []


# ─── API ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_endpoint_without_key_reports_skipped(client, places_off):
    resp = await client.post(
        "/api/v1/dialtone/discovery/search", json={"query": "injection molders"}
    )
    assert resp.status_code == 200  # not a 5xx: the request was fine, we're just off
    body = resp.json()
    assert body["status"] == "skipped"
    assert body["found"] == 0


@pytest.mark.asyncio
async def test_search_endpoint_stages_results(client, places_on):
    _FakeClient.responses = [_page([_place("a"), _place("b", "Second Co")])]
    resp = await client.post(
        "/api/v1/dialtone/discovery/search",
        json={"query": "injection molders", "location": "Pune, India", "country": "IN"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["created"] == 2
    assert body["search_id"] is not None
    assert len(body["results"]) == 2


@pytest.mark.asyncio
async def test_search_endpoint_preview_persists_nothing(client, places_on, db_session):
    _FakeClient.responses = [_page([_place("a")])]
    resp = await client.post(
        "/api/v1/dialtone/discovery/search",
        json={"query": "molders", "save": False},
    )
    assert resp.status_code == 200
    assert resp.json()["found"] == 1
    assert resp.json()["created"] == 0
    rows = (await db_session.execute(select(DiscoveredBusiness))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_search_endpoint_reports_rate_limited(client, places_on):
    _FakeClient.responses = [_FakeResponse(429, headers={"Retry-After": "0"})]
    resp = await client.post("/api/v1/dialtone/discovery/search", json={"query": "molders"})
    body = resp.json()
    assert body["status"] == "rate_limited"
    assert body["found"] == 0  # ...but status says why, so it can't read as empty


@pytest.mark.asyncio
async def test_search_endpoint_rejects_a_too_short_query(client, places_on):
    resp = await client.post("/api/v1/dialtone/discovery/search", json={"query": "x"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_results_endpoint_filters(client, places_on, db_session):
    await _mk_business(db_session, place_id="r1", business_name="Alpha Molding", status="new")
    await _mk_business(
        db_session, place_id="r2", business_name="Beta Casting", status="imported",
        email="b@beta.in",
    )
    await _mk_business(
        db_session, place_id="r3", business_name="Gamma Co", country="US", status="new",
        website=None, domain=None,
    )

    resp = await client.get("/api/v1/dialtone/discovery/results?status=new")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    resp = await client.get("/api/v1/dialtone/discovery/results?country=US")
    assert resp.json()["total"] == 1

    resp = await client.get("/api/v1/dialtone/discovery/results?has_email=true")
    assert resp.json()["total"] == 1

    resp = await client.get("/api/v1/dialtone/discovery/results?has_website=false")
    assert resp.json()["total"] == 1

    resp = await client.get("/api/v1/dialtone/discovery/results?q=alpha")
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_import_endpoint_imports_a_business(client, places_on, db_session):
    business = await _mk_business(db_session, email="owner@shreemolding.in")
    resp = await client.post(
        f"/api/v1/dialtone/discovery/results/{business.id}/import", json={}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "imported"
    assert body["company_id"] is not None
    assert body["contact_id"] is not None
    assert body["created_company"] is True


@pytest.mark.asyncio
async def test_import_endpoint_is_idempotent(client, places_on, db_session):
    business = await _mk_business(db_session, email="owner@shreemolding.in")
    first = await client.post(
        f"/api/v1/dialtone/discovery/results/{business.id}/import", json={}
    )
    assert first.json()["status"] == "imported"

    second = await client.post(
        f"/api/v1/dialtone/discovery/results/{business.id}/import", json={}
    )
    assert second.json()["status"] == "already_imported"
    assert second.json()["company_id"] == first.json()["company_id"]

    companies = (await db_session.execute(select(Company))).scalars().all()
    assert len(companies) == 1


@pytest.mark.asyncio
async def test_import_endpoint_records_unsupported_country_as_skipped(
    client, places_on, db_session
):
    business = await _mk_business(db_session, country="DE", place_id="de-2")
    resp = await client.post(
        f"/api/v1/dialtone/discovery/results/{business.id}/import", json={}
    )
    assert resp.status_code == 200  # a routine "can't import this yet", not a 500
    body = resp.json()
    assert body["status"] == "skipped"
    assert "DE" in body["error"]
    assert body["business"]["status_reason"] is not None


@pytest.mark.asyncio
async def test_import_endpoint_404s_for_unknown_business(client, places_on):
    resp = await client.post(
        f"/api/v1/dialtone/discovery/results/{uuid.uuid4()}/import", json={}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_and_list_saved_searches(client, places_on):
    resp = await client.post(
        "/api/v1/dialtone/discovery/searches",
        json={
            "name": "Pune molders",
            "query": "injection molding manufacturers",
            "location_text": "Pune, India",
            "country": "IN",
            "schedule": "weekly",
        },
    )
    assert resp.status_code == 201
    created = resp.json()
    assert created["schedule"] == "weekly"
    assert created["is_active"] is True
    assert created["results_count"] == 0

    listed = await client.get("/api/v1/dialtone/discovery/searches")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    filtered = await client.get("/api/v1/dialtone/discovery/searches?schedule=daily")
    assert filtered.json() == []


@pytest.mark.asyncio
async def test_saved_search_rejects_a_bad_schedule(client, places_on):
    resp = await client.post(
        "/api/v1/dialtone/discovery/searches",
        json={"name": "x", "query": "molders", "schedule": "hourly"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_saved_search_is_soft(client, places_on, db_session):
    search = await _mk_search(db_session)
    resp = await client.delete(f"/api/v1/dialtone/discovery/searches/{search.id}")
    assert resp.status_code == 204

    # Soft: deleting the row would erase the provenance of every company it found.
    await db_session.refresh(search)
    assert search.is_active is False


@pytest.mark.asyncio
async def test_run_saved_search_endpoint(client, places_on, db_session):
    search = await _mk_search(db_session)
    _FakeClient.responses = [_page([_place("a")])]
    resp = await client.post(f"/api/v1/dialtone/discovery/searches/{search.id}/run")
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] == 1

    await db_session.refresh(search)
    assert search.last_run_at is not None
    assert search.last_status == "ok"
    assert search.results_count == 1


@pytest.mark.asyncio
async def test_stats_endpoint_reports_the_funnel(client, places_on, db_session):
    await _mk_business(db_session, place_id="s1", status="new")
    await _mk_business(
        db_session, place_id="s2", status="enriched", email="a@x.in", email_source="hunter"
    )
    await _mk_business(
        db_session, place_id="s3", status="imported", email="b@y.in", email_source="scrape"
    )
    await _mk_business(db_session, place_id="s4", status="new", website=None, domain=None)

    resp = await client.get("/api/v1/dialtone/discovery/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 4
    assert body["by_status"] == {"new": 2, "enriched": 1, "imported": 1}
    assert body["with_website"] == 3
    assert body["with_email"] == 2
    assert body["imported"] == 1
    assert body["by_email_source"] == {"hunter": 1, "scrape": 1}
    assert body["email_yield"] == 0.5
    assert body["import_yield"] == 0.25


@pytest.mark.asyncio
async def test_stats_on_an_empty_table_does_not_divide_by_zero(client, places_on):
    resp = await client.get("/api/v1/dialtone/discovery/stats")
    assert resp.status_code == 200
    assert resp.json()["email_yield"] == 0.0
    assert resp.json()["import_yield"] == 0.0


@pytest.mark.asyncio
async def test_discovery_endpoints_require_auth(unauth_client):
    resp = await unauth_client.get("/api/v1/dialtone/discovery/results")
    assert resp.status_code in (401, 403)


# ─── the scheduled task ──────────────────────────────────────────────────────

def test_is_due_never_run():
    from app.tasks.places_discovery import _is_due

    now = datetime.now(timezone.utc)
    search = DiscoverySearch(name="x", query="q", schedule="weekly", is_active=True)
    assert _is_due(search, now) is True


def test_is_due_respects_inactive():
    from app.tasks.places_discovery import _is_due

    now = datetime.now(timezone.utc)
    search = DiscoverySearch(name="x", query="q", schedule="weekly", is_active=False)
    assert _is_due(search, now) is False


def test_one_time_search_never_reruns():
    """Its last_run_at is the record that it already happened."""
    from app.tasks.places_discovery import _is_due

    now = datetime.now(timezone.utc)
    search = DiscoverySearch(
        name="x", query="q", schedule="one_time", is_active=True,
        last_run_at=now - timedelta(days=365),
    )
    assert _is_due(search, now) is False


def test_weekly_search_waits_a_week():
    """Cadence is measured from the last run, not assumed from the beat
    interval, so a re-fired beat can't double-charge."""
    from app.tasks.places_discovery import _is_due

    now = datetime.now(timezone.utc)
    search = DiscoverySearch(
        name="x", query="q", schedule="weekly", is_active=True,
        last_run_at=now - timedelta(days=2),
    )
    assert _is_due(search, now) is False
    search.last_run_at = now - timedelta(days=8)
    assert _is_due(search, now) is True


def test_daily_search_waits_a_day():
    from app.tasks.places_discovery import _is_due

    now = datetime.now(timezone.utc)
    search = DiscoverySearch(
        name="x", query="q", schedule="daily", is_active=True,
        last_run_at=now - timedelta(hours=2),
    )
    assert _is_due(search, now) is False
    search.last_run_at = now - timedelta(days=1, hours=1)
    assert _is_due(search, now) is True


def test_is_due_handles_a_naive_timestamp():
    """SQLite hands back naive datetimes; comparing one to an aware now raises."""
    from app.tasks.places_discovery import _is_due

    now = datetime.now(timezone.utc)
    search = DiscoverySearch(
        name="x", query="q", schedule="weekly", is_active=True,
        last_run_at=(now - timedelta(days=8)).replace(tzinfo=None),
    )
    assert _is_due(search, now) is True


@pytest.mark.asyncio
async def test_task_no_ops_without_a_key(places_off):
    from app.tasks.places_discovery import _run_scheduled_discoveries

    result = await _run_scheduled_discoveries()
    assert result["status"] == "skipped"
    assert result["reason"] == "no_api_key"
    assert _FakeClient.calls == []


@pytest.mark.asyncio
async def test_enrich_sweep_no_ops_without_a_key(places_off):
    from app.tasks.places_discovery import _enrich_discovered_emails

    result = await _enrich_discovered_emails()
    assert result["status"] == "skipped"
    assert result["checked"] == 0


@pytest.mark.asyncio
async def test_enrich_sweep_keeps_scraping_after_hunter_budget_runs_out(
    places_on, db_engine, db_session, monkeypatch
):
    """An exhausted Hunter budget must not end the sweep.

    extract_email falls through to the free scraper, which is still worth
    running for every remaining row — bailing out here would strand businesses
    over a paid lookup we didn't even need.
    """
    from app.services import hunter_verification, web_scraper
    from app.tasks import places_discovery as task_mod

    monkeypatch.setattr(get_settings(), "hunter_api_key", "hk")

    async def _broke(self, db, domain, **kw):
        return {"status": "skipped", "reason": "budget_exhausted", "email": None, "emails": []}

    async def _fake_scrape(self, domain):
        return {"emails": [f"owner@{domain}"], "phones": []}

    monkeypatch.setattr(
        hunter_verification.HunterVerificationService, "domain_search", _broke
    )
    monkeypatch.setattr(web_scraper.WebScraper, "scrape_company", _fake_scrape)

    for i in range(3):
        await _mk_business(
            db_session, place_id=f"e{i}", domain=f"co{i}.in", website=f"https://co{i}.in"
        )

    # Point the task's session factory at the test engine.
    from contextlib import asynccontextmanager
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def _session():
        async with factory() as s:
            yield s

    monkeypatch.setattr(task_mod, "celery_session", _session)

    result = await task_mod._enrich_discovered_emails()
    assert result["checked"] == 3
    assert result["enriched"] == 3          # all three, despite Hunter being broke
    assert result["by_source"] == {"scrape": 3}
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_enrich_sweep_skips_dispositioned_and_websiteless_rows(
    places_on, db_engine, db_session, monkeypatch
):
    """Enriching a business nobody will import is pure credit burn."""
    from app.services import web_scraper
    from app.tasks import places_discovery as task_mod

    monkeypatch.setattr(get_settings(), "hunter_api_key", "")
    scraped: list[str] = []

    async def _fake_scrape(self, domain):
        scraped.append(domain)
        return {"emails": [f"owner@{domain}"], "phones": []}

    monkeypatch.setattr(web_scraper.WebScraper, "scrape_company", _fake_scrape)

    await _mk_business(db_session, place_id="k1", domain="new.in", status="new")
    await _mk_business(db_session, place_id="k2", domain="skip.in", status="skipped")
    await _mk_business(db_session, place_id="k3", domain="imp.in", status="imported")
    await _mk_business(db_session, place_id="k4", domain=None, website=None, status="new")

    from contextlib import asynccontextmanager
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def _session():
        async with factory() as s:
            yield s

    monkeypatch.setattr(task_mod, "celery_session", _session)

    result = await task_mod._enrich_discovered_emails()
    assert result["checked"] == 1
    assert scraped == ["new.in"]


def test_places_tasks_are_registered_on_the_enrichment_queue():
    from app.tasks.celery_app import celery_app

    assert "app.tasks.places_discovery.run_scheduled_discoveries" in celery_app.tasks
    assert "app.tasks.places_discovery.enrich_discovered_emails" in celery_app.tasks
    assert celery_app.conf.task_routes["app.tasks.places_discovery.*"] == {
        "queue": "enrichment"
    }
    beat = celery_app.conf.beat_schedule
    assert "run-places-discoveries" in beat
    assert "enrich-discovered-emails" in beat
