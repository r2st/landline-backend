"""Google Places as the prospector's second company source.

Apollo's free plan 403s both search endpoints, which left the prospector with no
source at all. These cover the fallback: when Apollo yields nothing, Places is
queried, staged and handed to the same ingestion path — and when Apollo works,
Places is never touched and never billed.

Only the HTTP boundary is faked. ``discover``, ``_stage`` and ``to_candidate``
run for real, so a change to the Places → CandidateRecord mapping fails here.
"""

from datetime import date

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.discovered_business import DiscoveredBusiness
from app.models.prospecting import IcpProfile
from app.services import places_discovery
from app.services.enrichment import ApolloSearchResult
from app.services.ingestion import CandidateRecord
from app.services.places_discovery import PROVIDER, PlaceResult, PlacesSearchResult
from app.services.autonomous_prospector import (
    PLACES_METRO_AREAS,
    PLACES_SOURCE,
    get_prospector,
)

pytestmark = pytest.mark.asyncio


def _place(place_id: str, name: str, **kw) -> PlaceResult:
    return PlaceResult(
        place_id=place_id,
        name=name,
        city=kw.get("city", "Pune"),
        state=kw.get("state", "Maharashtra"),
        country=kw.get("country", "IN"),
        phone=kw.get("phone", "+91 20 1234 5678"),
        website=kw.get("website", f"https://{place_id}.example.com"),
        domain=kw.get("domain", f"{place_id}.example.com"),
        category=kw.get("category", "Injection Molding"),
        raw={"id": place_id},
    )


def _apollo_record(name: str = "Apollo Co") -> CandidateRecord:
    return CandidateRecord(
        name=name, domain="apollo-co.example.com", country="US", source="apollo"
    )


@pytest.fixture
def places_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_places_api_key", "test-places-key")
    monkeypatch.setattr(settings, "google_places_max_requests_per_minute", 0)
    monkeypatch.setattr(settings, "supported_countries", ["US", "CA", "IN"])
    return settings


@pytest.fixture
def no_enrichment(monkeypatch):
    """Most tests here are about sourcing, not email recovery. Off by default so
    they can't reach Hunter or the scraper."""
    monkeypatch.setattr(get_settings(), "prospecting_places_enrich_max", 0)


def _fake_extract_email(monkeypatch, by_domain: dict):
    """Stub extract_email; ``by_domain`` maps domain -> email (or None to miss)."""
    calls = []

    async def _extract(self, db, business):
        calls.append(business.domain)
        email = by_domain.get(business.domain)
        if email is None:
            return {"status": "not_found", "reason": "no_emails"}
        business.email = email
        business.email_source = "scrape"
        business.status = "enriched"
        return {"status": "enriched", "source": "scrape", "email": email}

    monkeypatch.setattr(
        places_discovery.PlacesDiscoveryService, "extract_email", _extract
    )
    return calls


@pytest.fixture
def apollo_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "apollo_api_key", "test-apollo-key")


def _fake_apollo(monkeypatch, result: ApolloSearchResult):
    """Point the prospector's lazy ``get_enrichment_service`` at a stub."""
    calls = []

    class _Svc:
        async def search_companies(self, db, **kw):
            calls.append(kw)
            return result

    import app.services.enrichment as enrichment

    monkeypatch.setattr(enrichment, "get_enrichment_service", lambda: _Svc())
    return calls


def _fake_places_search(monkeypatch, result: PlacesSearchResult):
    """Fake only the HTTP-backed search; the rest of Places runs for real."""
    calls = []

    async def _search(self, db, query, **kw):
        calls.append({"query": query, **kw})
        return result

    monkeypatch.setattr(
        places_discovery.PlacesDiscoveryService, "search_businesses", _search
    )
    return calls


# ── the constant the lazy import can't check ────────────────────────────────


async def test_places_source_constant_matches_the_provider():
    # The prospector pins the string rather than importing the service at module
    # scope; if PROVIDER is ever renamed, this is what catches it.
    assert PLACES_SOURCE == PROVIDER


# ── Apollo still wins when it works ─────────────────────────────────────────


async def test_apollo_results_short_circuit_places(db_session, apollo_on, places_on, monkeypatch):
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[_apollo_record()], status="ok"))
    places_calls = _fake_places_search(monkeypatch, PlacesSearchResult())

    run = _StubRun()
    records, sources, notes = await prospector._source_candidates(db_session, icp, 10, run)

    assert [r.source for r in records] == ["apollo"]
    assert sources == ["apollo"]
    # Places bills per request — a working Apollo must not cost a Places call.
    assert places_calls == []
    assert "places_status" not in notes


# ── the 403 that started this ───────────────────────────────────────────────


async def test_apollo_403_falls_back_to_places(db_session, apollo_on, places_on, no_enrichment, monkeypatch):
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(
        monkeypatch,
        ApolloSearchResult(records=[], status="error", error="HTTP 403"),
    )
    _fake_places_search(
        monkeypatch,
        PlacesSearchResult(results=[_place("p1", "Pune Precision Molders")], pages_fetched=1),
    )

    records, sources, notes = await prospector._source_candidates(
        db_session, icp, 10, _StubRun()
    )

    assert [r.name for r in records] == ["Pune Precision Molders"]
    assert [r.source for r in records] == [PROVIDER]
    assert sources == ["apollo", PROVIDER]
    # Both stories survive: why Apollo gave nothing, and what Places did.
    assert notes["apollo_status"] == "error"
    assert notes["apollo_error"] == "HTTP 403"
    assert notes["places_status"] == "ok"
    assert notes["places_candidates"] == 1
    assert notes["places_staged"] == 1


async def test_apollo_empty_ok_result_falls_back_to_places(
    db_session, apollo_on, places_on, no_enrichment, monkeypatch
):
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    # Not an error — Apollo simply matched nothing. Still worth a second source.
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch, PlacesSearchResult(results=[_place("p2", "Nashik Toolworks")])
    )

    records, sources, _ = await prospector._source_candidates(
        db_session, icp, 10, _StubRun()
    )

    assert [r.name for r in records] == ["Nashik Toolworks"]
    assert sources == ["apollo", PROVIDER]


async def test_no_apollo_key_still_reaches_places(db_session, places_on, no_enrichment, monkeypatch):
    monkeypatch.setattr(get_settings(), "apollo_api_key", "")
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_places_search(
        monkeypatch, PlacesSearchResult(results=[_place("p3", "Chakan Castings")])
    )

    records, sources, notes = await prospector._source_candidates(
        db_session, icp, 10, _StubRun()
    )

    assert [r.name for r in records] == ["Chakan Castings"]
    # Apollo was never queried, so it is not a source this run queried.
    assert sources == [PROVIDER]
    assert notes["apollo_status"] == "skipped"


# ── graceful degradation ────────────────────────────────────────────────────


async def test_places_service_disabled_403_degrades_quietly(
    db_session, apollo_on, places_on, monkeypatch
):
    """The live prod failure: key valid, Places API not enabled in the project."""
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="error", error="HTTP 403"))
    _fake_places_search(
        monkeypatch,
        PlacesSearchResult(results=[], status="error", error="HTTP 403", truncated=True),
    )

    records, sources, notes = await prospector._source_candidates(
        db_session, icp, 10, _StubRun()
    )

    assert records == []
    assert sources == ["apollo", PROVIDER]
    assert notes["places_status"] == "error"
    assert notes["places_error"] == "HTTP 403"
    # An outage must not read as an empty market.
    assert notes["places_candidates"] == 0


async def test_places_budget_exhausted_is_reported_not_raised(
    db_session, apollo_on, places_on, monkeypatch
):
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch,
        PlacesSearchResult(status="budget_exhausted", error="daily budget spent", truncated=True),
    )

    records, _, notes = await prospector._source_candidates(db_session, icp, 10, _StubRun())

    assert records == []
    assert notes["places_status"] == "budget_exhausted"
    assert notes["places_truncated"] is True


async def test_places_without_a_key_is_skipped_not_queried(
    db_session, apollo_on, monkeypatch
):
    monkeypatch.setattr(get_settings(), "google_places_api_key", "")
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="error", error="HTTP 403"))
    places_calls = _fake_places_search(monkeypatch, PlacesSearchResult())

    records, sources, notes = await prospector._source_candidates(
        db_session, icp, 10, _StubRun()
    )

    assert records == []
    assert places_calls == []
    assert sources == ["apollo"]
    assert notes["places_status"] == "skipped"
    assert notes["places_error"] == "no_api_key"


# ── staging, dedup and the ICP-derived query ────────────────────────────────


async def test_already_imported_businesses_are_not_resourced(
    db_session, apollo_on, places_on, no_enrichment, monkeypatch
):
    """A business a previous run imported must not crowd out unseen ones."""
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch,
        PlacesSearchResult(results=[_place("dup", "Old News Ltd"), _place("new", "Fresh Co")]),
    )

    # Stage "dup" as already imported, exactly as a prior run would have left it.
    import uuid as _uuid

    db_session.add(
        DiscoveredBusiness(
            place_id="dup",
            business_name="Old News Ltd",
            country="IN",
            status="imported",
            discovered_via=PROVIDER,
            company_id=_uuid.uuid4(),
            raw={},
        )
    )
    await db_session.flush()

    records, _, notes = await prospector._source_candidates(
        db_session, icp, 10, _StubRun()
    )

    assert [r.name for r in records] == ["Fresh Co"]
    assert notes["places_duplicates"] == 1
    assert notes["places_candidates"] == 1


async def test_places_query_rotation_starts_from_the_icp(db_session, places_on):
    # The ICP's own segments lead the rotation (weight desc), the built-in
    # breadth queries follow, and a day where the rotation index lands on 0
    # gets the ICP's strongest segment.
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    icp.industry_weights = {"Injection Molding": 0.9, "Woodworking": 0.1}
    icp.location_preferences = {"countries": ["IN"], "states": ["Maharashtra"]}

    queries = prospector._places_query_rotation(icp)
    assert queries[0] == "Injection Molding"
    assert queries[1] == "Woodworking"
    # "Injection Molding" is also a built-in query; it must not repeat.
    assert len(queries) == len({q.lower() for q in queries})

    day0 = date.fromordinal(len(queries) * 7)  # index 0 in the rotation
    search = prospector._places_search_from_icp(icp, today=day0)
    assert search.query == "Injection Molding"
    # Non-US ICP: the US metro list doesn't apply; the ICP's states rotate.
    assert search.location_text == "Maharashtra"
    assert search.country == "IN"
    # Transient on purpose: saved searches are the operator's, not the agent's.
    assert search.id is None


async def test_places_query_falls_back_to_country_without_states(db_session, places_on):
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    icp.industry_weights = {"CNC Machining": 1.0}
    icp.location_preferences = {"countries": ["IN"], "states": []}

    search = prospector._places_search_from_icp(icp)

    assert search.location_text == "IN"
    assert search.country == "IN"


async def test_us_places_search_rotates_metros(db_session, places_on):
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    icp.industry_weights = {"CNC Machining": 1.0}
    icp.location_preferences = {"countries": ["US"], "states": ["OH"]}

    search = prospector._places_search_from_icp(icp)

    # US runs always target a specific manufacturing metro, never a whole state:
    # each (query, metro) pair is a distinct textQuery with its own 60-result cap.
    assert search.location_text in PLACES_METRO_AREAS
    assert search.country == "US"


async def test_places_rotation_changes_query_daily_and_walks_the_grid(
    db_session, places_on
):
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    icp.industry_weights = {"CNC Machining": 1.0}
    icp.location_preferences = {"countries": ["US"], "states": []}

    queries = prospector._places_query_rotation(icp)
    n_days = len(queries) * len(PLACES_METRO_AREAS)
    seen: set[tuple[str, str]] = set()
    prev = None
    for ordinal in range(1000, 1000 + n_days):
        s = prospector._places_search_from_icp(icp, today=date.fromordinal(ordinal))
        assert (s.query, s.location_text) not in seen  # no repeats within a cycle
        seen.add((s.query, s.location_text))
        if prev is not None:
            assert s.query != prev  # consecutive days never run the same query
        prev = s.query
    # The full query × metro grid is covered before any pair repeats.
    assert len(seen) == n_days


async def test_places_rotation_is_deterministic_for_a_given_day(db_session, places_on):
    # A same-day retry must re-fetch the same textQuery (a cache-friendly no-op
    # against the dedup), not burn budget on a fresh 60-result grid cell.
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    d = date.fromordinal(739000)

    a = prospector._places_search_from_icp(icp, today=d)
    b = prospector._places_search_from_icp(icp, today=d)

    assert (a.query, a.location_text) == (b.query, b.location_text)


async def test_places_candidate_carries_no_invented_firmographics(
    db_session, apollo_on, places_on, no_enrichment, monkeypatch
):
    """Places has no employee count. qualify() must see unknown, not zero."""
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch, PlacesSearchResult(results=[_place("p9", "Sangli Plastics")])
    )

    records, _, _ = await prospector._source_candidates(db_session, icp, 10, _StubRun())

    record = records[0]
    assert record.employee_count is None
    assert record.icp_profile_id == icp.id
    assert record.phone == "+91 20 1234 5678"
    assert record.domain == "p9.example.com"
    # No email from Places, so no contact to attach.
    assert record.contacts == []


# ── end to end ──────────────────────────────────────────────────────────────


async def test_run_completes_via_places_when_apollo_is_dead(
    db_session, apollo_on, places_on, no_enrichment, monkeypatch
):
    prospector = get_prospector()
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="error", error="HTTP 403"))
    _fake_places_search(
        monkeypatch,
        PlacesSearchResult(results=[_place("e2e", "End To End Molding")], pages_fetched=1),
    )

    run = await prospector.run(db_session, target_volume=5, trigger="test")

    assert run.status == "completed"
    assert PROVIDER in run.sources_queried
    assert run.companies_found == 1
    assert run.results["places_status"] == "ok"
    assert run.results["apollo_error"] == "HTTP 403"


# ── email enrichment: Places gives businesses, not people ───────────────────


async def test_places_businesses_are_enriched_into_contacts(
    db_session, apollo_on, places_on, monkeypatch
):
    """Without this, every Places candidate reaches ingestion with no contact
    and _auto_enroll has nobody to enrol however well the company fits."""
    monkeypatch.setattr(get_settings(), "prospecting_places_enrich_max", 10)
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch, PlacesSearchResult(results=[_place("e1", "Emailable Molders")])
    )
    calls = _fake_extract_email(monkeypatch, {"e1.example.com": "owner@e1.example.com"})

    records, _, notes = await prospector._source_candidates(
        db_session, icp, 10, _StubRun()
    )

    assert calls == ["e1.example.com"]
    assert [c.email for c in records[0].contacts] == ["owner@e1.example.com"]
    assert notes["places_enrich_attempted"] == 1
    assert notes["places_enriched"] == 1
    assert notes["places_with_email"] == 1


async def test_enrichment_miss_still_yields_a_candidate(
    db_session, apollo_on, places_on, monkeypatch
):
    """No email found is not a failure — the phone still feeds the call queue."""
    monkeypatch.setattr(get_settings(), "prospecting_places_enrich_max", 10)
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch, PlacesSearchResult(results=[_place("m1", "No Email Ltd")])
    )
    _fake_extract_email(monkeypatch, {"m1.example.com": None})

    records, _, notes = await prospector._source_candidates(
        db_session, icp, 10, _StubRun()
    )

    assert [r.name for r in records] == ["No Email Ltd"]
    assert records[0].contacts == []
    assert records[0].phone == "+91 20 1234 5678"
    assert notes["places_enriched"] == 0
    assert notes["places_with_email"] == 0


async def test_enrichment_is_capped_per_run(
    db_session, apollo_on, places_on, monkeypatch
):
    """POST /prospecting/run awaits the run, so enrichment can't be unbounded."""
    monkeypatch.setattr(get_settings(), "prospecting_places_enrich_max", 2)
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch,
        PlacesSearchResult(results=[_place(f"c{i}", f"Co {i}") for i in range(5)]),
    )
    calls = _fake_extract_email(monkeypatch, {})

    records, _, notes = await prospector._source_candidates(
        db_session, icp, 10, _StubRun()
    )

    assert len(calls) == 2
    assert notes["places_enrich_attempted"] == 2
    assert notes["places_enrich_capped"] is True
    # Capped enrichment must not drop candidates — they just have no email yet.
    assert len(records) == 5


async def test_enrichment_failure_does_not_fail_the_run(
    db_session, apollo_on, places_on, monkeypatch
):
    monkeypatch.setattr(get_settings(), "prospecting_places_enrich_max", 10)
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch,
        PlacesSearchResult(results=[_place("boom", "Exploding Co"), _place("ok", "Fine Co")]),
    )

    async def _explode(self, db, business):
        if business.domain == "boom.example.com":
            raise RuntimeError("site unreachable")
        business.email = "hi@ok.example.com"
        return {"status": "enriched", "source": "scrape", "email": business.email}

    monkeypatch.setattr(
        places_discovery.PlacesDiscoveryService, "extract_email", _explode
    )

    records, _, notes = await prospector._source_candidates(
        db_session, icp, 10, _StubRun()
    )

    # One unreachable site must not cost us the other business.
    assert len(records) == 2
    assert notes["places_enriched"] == 1


async def test_business_without_a_domain_is_not_enrichment_budget(
    db_session, apollo_on, places_on, monkeypatch
):
    monkeypatch.setattr(get_settings(), "prospecting_places_enrich_max", 1)
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch,
        PlacesSearchResult(results=[
            _place("nosite", "Phone Only Co", website=None, domain=None),
            _place("hassite", "Web Co"),
        ]),
    )
    calls = _fake_extract_email(monkeypatch, {"hassite.example.com": "a@hassite.example.com"})

    _, _, notes = await prospector._source_candidates(db_session, icp, 10, _StubRun())

    # The domain-less business costs no call, so the budget reaches the real one.
    assert calls == ["hassite.example.com"]
    assert notes["places_enrich_attempted"] == 1


# ── the writeback that makes the dedup filter real ──────────────────────────


async def test_imported_places_business_is_written_back(
    db_session, apollo_on, places_on, no_enrichment, monkeypatch
):
    """Nothing else on this path sets company_id, so without the writeback the
    next run re-sources and re-enriches the same business forever."""
    prospector = get_prospector()
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch, PlacesSearchResult(results=[_place("wb", "Writeback Molding")])
    )

    run = await prospector.run(db_session, target_volume=5, trigger="test")
    assert run.status == "completed"

    business = (
        await db_session.execute(
            select(DiscoveredBusiness).where(DiscoveredBusiness.place_id == "wb")
        )
    ).scalar_one()
    assert business.status in ("imported", "skipped")
    if business.status == "imported":
        assert business.company_id is not None
        assert business.imported_at is not None
    else:
        # Routed to discard — recorded so it isn't re-sourced next run.
        assert business.status_reason


async def test_second_run_does_not_resource_an_imported_business(
    db_session, apollo_on, places_on, no_enrichment, monkeypatch
):
    prospector = get_prospector()
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch, PlacesSearchResult(results=[_place("once", "Only Once Ltd")])
    )

    first = await prospector.run(db_session, target_volume=5, trigger="test")
    assert first.companies_found == 1

    second = await prospector.run(db_session, target_volume=5, trigger="test")

    # Same query, same business — but it has already been dispositioned.
    assert second.companies_found == 0
    assert second.results["places_duplicates"] == 1
    assert second.results["places_candidates"] == 0


class _StubRun:
    """``_source_candidates`` only ever reads ``run.id`` (for Apollo's ledger)."""

    id = None


# ── Places taxonomy vs ICP taxonomy ─────────────────────────────────────────
#
# Places labels a machine shop 'manufacturer'; the ICP scores segments like
# 'CNC Machining'. Nothing matches across that gap, so industry scored 0/40 and
# capped every Places business at 38 — under the 50 review line, which is why a
# source that was finding businesses was never landing any. The query that found
# the business is an ICP term by construction, and is the signal used instead.


def _learned_icp(**kw) -> IcpProfile:
    """An ICP learned from won deals concentrated on one vertical.

    ``learn_icp`` normalizes the winning segment to weight 1.0; the seeded
    defaults it blends in only reach 0.8. Built explicitly so the scores below
    are the real production arithmetic and not an artifact of the seed.
    """
    return IcpProfile(
        name="test-icp",
        is_active=True,
        industry_weights=kw.get("industry_weights", {"CNC Machining": 1.0, "Plastics": 0.7}),
        size_range={"min": 50, "max": 500, "ideal": 150},
        location_preferences={"countries": ["US"], "states": []},
        exclusion_criteria={
            "industries": [], "countries": [], "max_employees": 2000, "min_employees": 5,
        },
    )


def _places_candidate(industry: str) -> dict:
    """What a Places business looks like to `qualify`: no employee count, ever."""
    return {
        "name": "Acme Precision",
        "domain": "acme-precision.example.com",
        "country": "US",
        "industry": industry,
    }


async def test_places_category_alone_reaches_review_but_not_auto_enroll():
    # "manufacturer" isn't a learned ICP key, but it *is* manufacturing by
    # taxonomy, so it now gets the 0.6 fallback weight: 24 industry + 10
    # unknown-size + 20 location + 8 completeness = 62. Enough for the review
    # queue (>= 50), deliberately short of auto-enroll (>= 65) — taxonomy-only
    # matches get a human look, learned segments auto-enroll.
    q = get_prospector().qualify(_places_candidate("manufacturer"), _learned_icp())

    assert q["components"]["industry"] == 24.0
    assert q["fit_score"] == 62
    assert 50 <= q["fit_score"] < 65


async def test_unrelated_industry_still_scores_below_the_review_line():
    # The fallback is taxonomy-gated, not a blanket floor: an off-ICP industry
    # keeps the original failing arithmetic (0 + 10 + 20 + 8 = 38).
    q = get_prospector().qualify(_places_candidate("Restaurants"), _learned_icp())

    assert q["components"]["industry"] == 0.0
    assert q["fit_score"] == 38


async def test_rotated_query_outside_learned_keys_reaches_review():
    # A rotation query like "Tool and Die" that the learned ICP has no key for
    # must not re-create the structurally-disqualified bug: taxonomy fallback
    # puts it in the review queue.
    q = get_prospector().qualify(_places_candidate("Tool and Die"), _learned_icp())

    assert q["components"]["industry"] == 24.0
    assert q["fit_score"] >= 50


async def test_search_query_as_industry_clears_the_auto_enroll_line():
    # Same business, same missing employee count — only the industry signal
    # changes, and it is worth the full 40: 40 + 10 + 20 + 8 = 78, past 75.
    q = get_prospector().qualify(_places_candidate("CNC Machining"), _learned_icp())

    assert q["components"]["industry"] == 40.0
    assert q["fit_score"] == 78
    assert q["fit_score"] >= 75


async def test_to_candidate_prefers_the_search_query_over_the_places_category():
    business = DiscoveredBusiness(
        place_id="tc-1",
        business_name="Acme Precision",
        domain="acme-precision.example.com",
        country="US",
        category="manufacturer",
        search_query="CNC Machining",
        raw={},
    )

    record = places_discovery.get_places_service().to_candidate(business)

    assert record.industry == "CNC Machining"
    # The Places label is still worth keeping — it just isn't the ICP signal.
    assert record.raw["places_category"] == "manufacturer"


async def test_to_candidate_falls_back_to_the_category_without_a_query():
    # Rows staged before the query was carried, and any future source that has a
    # category but no query, must still get their best available signal.
    business = DiscoveredBusiness(
        place_id="tc-2",
        business_name="Nashik Toolworks",
        domain="nashik.example.com",
        category="Injection Molding",
        search_query=None,
        raw={},
    )

    record = places_discovery.get_places_service().to_candidate(business)

    assert record.industry == "Injection Molding"


async def test_sourced_records_carry_the_icp_query_as_their_industry(
    db_session, places_on, no_enrichment, monkeypatch
):
    # End to end through the real discover/_stage/to_candidate chain: what Places
    # calls the business is irrelevant, what the ICP asked for is what scores.
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    _fake_places_search(
        monkeypatch,
        PlacesSearchResult(results=[_place("icp-q", "Acme Precision", category="manufacturer")]),
    )

    records, notes = await prospector._source_from_places(db_session, icp, 10)

    # The query rotates daily, but within a day it's deterministic — whatever
    # today's query is, that's the industry the record must carry.
    todays_query = prospector._places_search_from_icp(icp).query
    assert [r.industry for r in records] == [todays_query]
    assert records[0].industry != "manufacturer"
    assert notes["places_query"] == todays_query


# ── dispositioned rows must not come back ───────────────────────────────────
#
# A rejected row keeps company_id NULL and records its verdict only in `status`,
# so the old `company_id is None` filter re-picked the same 25 rejects every run
# and never reached the businesses behind them.


def _staged(place_id: str, name: str, **kw) -> DiscoveredBusiness:
    return DiscoveredBusiness(
        place_id=place_id,
        business_name=name,
        domain=kw.get("domain", f"{place_id}.example.com"),
        country=kw.get("country", "US"),
        category="manufacturer",
        search_query="CNC Machining",
        discovered_via=PROVIDER,
        status=kw.get("status", "new"),
        status_reason=kw.get("status_reason"),
        email=kw.get("email"),
        raw={},
    )


async def test_skipped_business_is_not_resourced(
    db_session, places_on, no_enrichment, monkeypatch
):
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    db_session.add(
        _staged(
            "reject-1",
            "Already Judged Ltd",
            status="skipped",
            status_reason="not imported (route=discard)",
        )
    )
    await db_session.flush()
    # Same stable query, so Places hands back the same business it did last run.
    _fake_places_search(
        monkeypatch, PlacesSearchResult(results=[_place("reject-1", "Already Judged Ltd")])
    )

    records, notes = await prospector._source_from_places(db_session, icp, 10)

    assert records == []
    assert notes["places_duplicates"] == 1
    assert notes["places_candidates"] == 0


async def test_enriched_business_is_still_sourced(
    db_session, places_on, no_enrichment, monkeypatch
):
    # The nightly sweep parks rows at 'enriched' once it recovers an email and
    # nobody has ruled on them yet. Those are the *best* rows on the table — the
    # only Places businesses with someone to email — so excluding everything but
    # 'new' would quietly starve the prospector of exactly them.
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    db_session.add(
        _staged(
            "sweep-1",
            "Sweep Enriched Ltd",
            status="enriched",
            email="ops@sweep-1.example.com",
        )
    )
    await db_session.flush()
    _fake_places_search(
        monkeypatch, PlacesSearchResult(results=[_place("sweep-1", "Sweep Enriched Ltd")])
    )

    records, notes = await prospector._source_from_places(db_session, icp, 10)

    assert [r.name for r in records] == ["Sweep Enriched Ltd"]
    assert notes["places_candidates"] == 1


async def test_run_reaches_the_businesses_behind_a_wall_of_rejects(
    db_session, places_on, no_enrichment, monkeypatch
):
    # The symptom that started this: rejects filled the whole per-run budget, so
    # the unseen businesses behind them were never reached. With them excluded, a
    # 2-wide run spends its budget on the two nobody has looked at.
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    for i in range(3):
        db_session.add(_staged(f"wall-{i}", f"Rejected {i}", status="skipped"))
    await db_session.flush()
    _fake_places_search(
        monkeypatch,
        PlacesSearchResult(
            results=[_place(f"wall-{i}", f"Rejected {i}") for i in range(3)]
            + [_place("fresh-a", "Unseen A"), _place("fresh-b", "Unseen B")]
        ),
    )

    records, notes = await prospector._source_from_places(db_session, icp, 2)

    assert [r.name for r in records] == ["Unseen A", "Unseen B"]
    assert notes["places_candidates"] == 2


async def test_second_run_does_not_resource_a_business_it_rejected(
    db_session, apollo_on, places_on, no_enrichment, monkeypatch
):
    # The two-run version, through the real writeback: whatever verdict run one
    # reaches, run two must not pay to reach it again.
    prospector = get_prospector()
    _fake_apollo(monkeypatch, ApolloSearchResult(records=[], status="ok"))
    _fake_places_search(
        monkeypatch, PlacesSearchResult(results=[_place("verdict-1", "Judged Once Ltd")])
    )

    await prospector.run(db_session, target_volume=5, trigger="test")
    business = (
        await db_session.execute(
            select(DiscoveredBusiness).where(DiscoveredBusiness.place_id == "verdict-1")
        )
    ).scalar_one()
    assert business.status in ("imported", "skipped")

    second = await prospector.run(db_session, target_volume=5, trigger="test")

    assert second.results["places_duplicates"] == 1
    assert second.results["places_candidates"] == 0
