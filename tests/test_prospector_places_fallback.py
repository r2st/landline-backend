"""Google Places as the prospector's second company source.

Apollo's free plan 403s both search endpoints, which left the prospector with no
source at all. These cover the fallback: when Apollo yields nothing, Places is
queried, staged and handed to the same ingestion path — and when Apollo works,
Places is never touched and never billed.

Only the HTTP boundary is faked. ``discover``, ``_stage`` and ``to_candidate``
run for real, so a change to the Places → CandidateRecord mapping fails here.
"""

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.discovered_business import DiscoveredBusiness
from app.services import places_discovery
from app.services.enrichment import ApolloSearchResult
from app.services.ingestion import CandidateRecord
from app.services.places_discovery import PROVIDER, PlaceResult, PlacesSearchResult
from app.services.autonomous_prospector import PLACES_SOURCE, get_prospector

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


async def test_places_query_is_derived_from_the_icp(db_session, places_on):
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    icp.industry_weights = {"Injection Molding": 0.9, "Woodworking": 0.1}
    icp.location_preferences = {"countries": ["IN"], "states": ["Maharashtra"]}

    search = prospector._places_search_from_icp(icp)

    assert search.query == "Injection Molding"
    assert search.location_text == "Maharashtra"
    assert search.country == "IN"
    # Transient on purpose: saved searches are the operator's, not the agent's.
    assert search.id is None


async def test_places_query_falls_back_to_country_without_states(db_session, places_on):
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)
    icp.industry_weights = {"CNC Machining": 1.0}
    icp.location_preferences = {"countries": ["US"], "states": []}

    search = prospector._places_search_from_icp(icp)

    assert search.query == "CNC Machining"
    assert search.location_text == "US"
    assert search.country == "US"


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
