"""Tests for P5 — the unified ingestion pipeline, dedup, and the credit gate.

Covers the seven fixes this change set lands:

1. one ingestion path for every source        (test_*_dedup_*, test_*_parity)
2. the CSV case-sensitivity dedup bug          (test_csv_name_match_is_case_insensitive)
3. the US/CA country constraint + coercion     (test_country_*, test_location_score_*)
4. search_companies' mid-transaction commit    (test_search_companies_does_not_commit)
5. the empty industry filter                   (test_industry_filter_is_absent_not_empty)
6. the lead scorer wired to the prospector     (test_ingest_scores_company_post_persistence)
7. API credit tracking + backoff               (test_budget_gate_*, test_rate_limited_*)
"""

import json as json_lib
import uuid

import pytest

from app.models.api_credit import ApiCreditLedger
from app.models.company import Company
from app.models.contact import Contact
from app.models.lead_score import LeadScore
from app.models.prospecting import ProspectCandidate
from app.services import api_credits
from app.services.autonomous_prospector import get_prospector
from app.services.ingestion import (
    DEDUP_CERTAIN,
    DEDUP_HIGH,
    DEDUP_MEDIUM,
    DEDUP_NONE,
    ROUTE_AUTO_ENROLL,
    ROUTE_DISCARD,
    ROUTE_REVIEW,
    CandidateRecord,
    ContactRecord,
    IngestionError,
    IngestionService,
    get_ingestion_service,
)
from app.services.normalize import normalize_name_key, normalize_phone
from app.services.scoring import _location_score

from sqlalchemy import func, select

# pytest.ini sets asyncio_mode = auto, so async tests need no marker.


def _company(**kw) -> Company:
    base = dict(
        id=uuid.uuid4(),
        name="Existing Co",
        country="US",
        source="test",
        stage="new",
        pipeline_stage="new_lead",
    )
    base.update(kw)
    if "name_key" not in base:
        base["name_key"] = normalize_name_key(base["name"])
    return Company(**base)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_name_key_canonicalizes_legal_suffixes_and_case():
    # The whole point: these are one company, and the four old paths disagreed.
    assert normalize_name_key("Acme Corp.") == "acme"
    assert normalize_name_key("ACME CORPORATION") == "acme"
    assert normalize_name_key("acme, inc.") == "acme"
    assert normalize_name_key("Acme Pvt Ltd") == "acme"
    assert normalize_name_key("Acme Private Limited") == "acme"
    # Accents fold so "Nestlé" and "Nestle" match.
    assert normalize_name_key("Nestlé S.A.") == normalize_name_key("Nestle SA")
    # Distinct companies stay distinct.
    assert normalize_name_key("Acme Widgets") != normalize_name_key("Acme Gadgets")


def test_name_key_never_strips_to_nothing():
    # A company genuinely named "Ltd" keeps a key rather than collapsing onto
    # every other suffix-only name.
    assert normalize_name_key("Ltd") == "ltd"
    assert normalize_name_key("...") is None
    assert normalize_name_key("") is None
    assert normalize_name_key(None) is None


def test_normalize_phone_is_a_match_key():
    assert normalize_phone("+1 (216) 555-0100") == "+12165550100"
    assert normalize_phone("216-555-0100") == "2165550100"
    # Too short to identify anyone.
    assert normalize_phone("1234") is None
    assert normalize_phone(None) is None


# ---------------------------------------------------------------------------
# 2. Cross-source dedup precedence
# ---------------------------------------------------------------------------

async def test_dedup_by_domain_is_certain(db_session):
    db_session.add(_company(name="Acme Corp", domain="acme.com"))
    await db_session.flush()

    match = await get_ingestion_service().resolve_company(
        db_session, CandidateRecord(name="Totally Different Name", domain="acme.com")
    )
    assert match.confidence == DEDUP_CERTAIN
    assert match.matched_on == "domain"
    assert match.can_auto_merge is True


async def test_dedup_by_name_and_geo_is_high(db_session):
    db_session.add(_company(name="Precision Manufacturing", country="US", state_province="OH"))
    await db_session.flush()

    match = await get_ingestion_service().resolve_company(
        db_session,
        CandidateRecord(name="Precision Manufacturing Inc.", country="US", state="OH"),
    )
    assert match.confidence == DEDUP_HIGH
    assert match.matched_on == "name_key+country+state"


async def test_dedup_by_name_and_phone_is_high(db_session):
    db_session.add(_company(name="Precision Manufacturing", phone="216-555-0100"))
    await db_session.flush()

    match = await get_ingestion_service().resolve_company(
        db_session,
        CandidateRecord(name="Precision Manufacturing", phone="(216) 555-0100"),
    )
    assert match.confidence == DEDUP_HIGH
    assert match.matched_on == "name_key+phone"


async def test_same_name_different_geo_does_not_merge(db_session):
    """Ohio and Pune are two real companies. Never fold them together."""
    db_session.add(_company(name="Precision Manufacturing", country="US", state_province="OH"))
    await db_session.flush()

    svc = get_ingestion_service()
    match = await svc.resolve_company(
        db_session,
        CandidateRecord(name="Precision Manufacturing", country="IN", state="Maharashtra"),
    )
    # It surfaces the name collision but refuses to auto-merge on it.
    assert match.confidence == DEDUP_MEDIUM
    assert match.can_auto_merge is False

    # And ingesting really does create a second, separate company.
    outcome = await svc.ingest_one(
        db_session,
        CandidateRecord(
            name="Precision Manufacturing", country="IN", state="Maharashtra", source="test"
        ),
    )
    assert outcome.created is True
    total = (
        await db_session.execute(
            select(func.count(Company.id)).where(Company.name_key == "precision manufacturing")
        )
    ).scalar()
    assert total == 2


async def test_dedup_no_match_is_none(db_session):
    match = await get_ingestion_service().resolve_company(
        db_session, CandidateRecord(name="Nobody Has This Name", domain="nowhere.example")
    )
    assert match.confidence == DEDUP_NONE
    assert match.is_match is False


# ---------------------------------------------------------------------------
# 2b. The CSV case-sensitivity bug
# ---------------------------------------------------------------------------

async def test_csv_name_match_is_case_insensitive(db_session):
    """The bug: CSV matched names case-*sensitively* while every other path
    lowercased, so a CSV created the duplicate the prospector would have found.
    """
    db_session.add(_company(name="ACME STAMPING CO"))
    await db_session.flush()

    match = await get_ingestion_service().resolve_company(
        db_session, CandidateRecord(name="Acme Stamping Co")
    )
    assert match.is_match is True
    assert match.matched_on == "name_key"


async def test_csv_import_dedups_case_insensitively_on_domain(client):
    """The real-world CSV case: a domain is present, so the match is certain.

    The old path compared ``Company.name`` exactly while every other path
    lowercased, so differing case created a duplicate. Now it resolves.
    """
    import io

    csv_body = (
        "company_name,domain,country\n"
        "Acme Stamping Co,acmestamp.example,US\n"
        "ACME STAMPING CO,ACMEStamp.example,US\n"
    )
    files = {"file": ("list.csv", io.BytesIO(csv_body.encode()), "text/csv")}
    r = await client.post("/api/v1/import-export/import/csv", files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported_companies"] == 1
    assert body["skipped"] == 1


async def test_csv_and_prospector_resolve_identically(db_session):
    """The actual bug was *divergence*: CSV matched names case-sensitively and
    the prospector case-insensitively, so the same row resolved differently
    depending on which door it came through. Both now share one rule.
    """
    db_session.add(_company(name="ACME STAMPING CO", domain="acmestamp.example"))
    await db_session.flush()

    svc = get_ingestion_service()
    # Same company, arriving from two sources with different casing.
    csv_match = await svc.resolve_company(
        db_session,
        CandidateRecord(name="Acme Stamping Co", domain="acmestamp.example", source="csv_import"),
    )
    apollo_match = await svc.resolve_company(
        db_session,
        CandidateRecord(name="acme stamping co.", domain="ACMEStamp.example", source="apollo"),
    )
    assert csv_match.confidence == apollo_match.confidence == DEDUP_CERTAIN
    assert csv_match.company.id == apollo_match.company.id


async def test_name_only_collision_is_flagged_not_merged(db_session):
    """Two same-named rows with nothing to corroborate them stay separate and
    get flagged — the spec's rule, since silently merging two real prospects is
    the worse failure. The prospector used to over-merge these on lower(name).
    """
    run = await get_prospector().run(
        db_session,
        seed_candidates=[
            {"name": "Precision Manufacturing", "industry": "Metal Stamping",
             "employee_count": 150, "country": "US", "source": "seed"},
            {"name": "Precision Manufacturing", "industry": "Metal Stamping",
             "employee_count": 150, "country": "US", "source": "seed"},
        ],
        trigger="manual",
    )
    cands = (
        await db_session.execute(
            select(ProspectCandidate).where(ProspectCandidate.run_id == run.id)
        )
    ).scalars().all()
    confidences = sorted(c.dedup_confidence for c in cands)
    assert confidences == [DEDUP_MEDIUM, DEDUP_NONE]
    # The medium one is visible for a human merge decision rather than lost.
    assert any(c.dedup_confidence == DEDUP_MEDIUM for c in cands)


async def test_duplicates_endpoint_surfaces_medium_matches(client):
    seeds = [
        {"name": "Precision Manufacturing", "industry": "Metal Stamping",
         "employee_count": 150, "country": "US", "source": "seed"},
        {"name": "Precision Manufacturing", "industry": "Metal Stamping",
         "employee_count": 150, "country": "US", "source": "seed"},
    ]
    r = await client.post("/api/v1/prospecting/run", json={"seed_candidates": seeds})
    assert r.status_code == 200, r.text

    r = await client.get("/api/v1/prospecting/candidates/duplicates")
    assert r.status_code == 200, r.text
    dupes = r.json()
    assert len(dupes) == 1
    assert dupes[0]["dedup_confidence"] == "medium"

    # A human confirms the merge.
    company_id = dupes[0]["company_id"]
    r = await client.post(
        f"/api/v1/prospecting/candidates/{dupes[0]['id']}/merge?reviewer=jane",
        json={"target_company_id": company_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["merged_into_company_id"] == company_id
    assert r.json()["reviewed_by"] == "jane"

    # Once merged it leaves the queue.
    r = await client.get("/api/v1/prospecting/candidates/duplicates")
    assert r.json() == []


# ---------------------------------------------------------------------------
# 3. Country: allowlist, and rejection instead of silent relabeling
# ---------------------------------------------------------------------------

async def test_international_company_inserts(db_session):
    outcome = await get_ingestion_service().ingest_one(
        db_session, CandidateRecord(name="Pune Precision", country="IN", source="test")
    )
    assert outcome.created is True
    assert outcome.company.country == "IN"


async def test_unsupported_country_is_rejected_not_coerced(db_session):
    """The old paths relabelled anything non-US/CA to "US" — corrupting data
    rather than failing. A rejected row is recoverable; a mislabelled one isn't.
    """
    svc = get_ingestion_service()
    with pytest.raises(IngestionError) as exc:
        await svc.ingest_one(
            db_session, CandidateRecord(name="Tokyo Metalworks", country="JP", source="test")
        )
    assert "JP" in str(exc.value)

    # Crucially: nothing was written, and *nothing was relabelled to US*.
    rows = (
        await db_session.execute(select(Company).where(Company.name == "Tokyo Metalworks"))
    ).scalars().all()
    assert rows == []


async def test_absent_country_defaults_without_relabeling(db_session):
    # Defaulting a blank is not relabelling — no information is destroyed.
    outcome = await get_ingestion_service().ingest_one(
        db_session, CandidateRecord(name="No Country Co", country=None, source="test")
    )
    assert outcome.company.country == "US"


async def test_csv_rejects_unsupported_country_with_row_error(client):
    import io

    csv_body = "company_name,domain,country\nTokyo Metalworks,tokyometal.jp,JP\n"
    files = {"file": ("list.csv", io.BytesIO(csv_body.encode()), "text/csv")}
    r = await client.post("/api/v1/import-export/import/csv", files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported_companies"] == 0
    assert len(body["errors"]) == 1
    assert "JP" in body["errors"][0]
    assert "Row 2" in body["errors"][0]


def test_location_score_is_nonzero_for_supported_international():
    """An IN company must not be structurally zeroed by the scorer meant to
    rank it. US/CA keep their historical weights.
    """
    assert _location_score("US") == (20, "United States — primary market")
    assert _location_score("CA")[0] == 15
    in_pts, in_reason = _location_score("IN")
    assert in_pts > 0, "supported market scored zero — Indian leads are disqualified"
    assert in_pts == 12
    assert "IN" in in_reason
    # Genuinely off-market still scores 0.
    assert _location_score("JP")[0] == 0


# ---------------------------------------------------------------------------
# 4 + 5. enrichment: no internal commit, no phantom industry filter
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None, text=None):
        self._payload = payload or {}
        self.status_code = status_code
        self.headers = headers or {}
        # A real httpx.Response always carries .text, and the client reads it to
        # record *why* a call failed. Defaults to the JSON body, like the real one.
        self.text = text if text is not None else json_lib.dumps(self._payload)

    def json(self):
        return self._payload


def _install_fake_apollo(monkeypatch, responses, captured=None, headers_out=None):
    """Point httpx.AsyncClient at a scripted list of responses."""
    import httpx

    seq = list(responses)

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): ...
        async def post(self, url, headers=None, json=None):
            if captured is not None:
                captured.append(json)
            if headers_out is not None:
                headers_out.append(headers or {})
            return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    # Never actually sleep through a backoff in tests.
    import app.services.enrichment as enrichment_mod

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(enrichment_mod, "_sleep", _no_sleep)
    enrichment_mod.reset_rate_limiter()


async def test_search_companies_does_not_commit(db_session, monkeypatch):
    """The bug: the service called db.commit()/db.rollback() internally, so a
    failure inside a prospecting run rolled back the run row itself.
    """
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "fake"
    _install_fake_apollo(
        monkeypatch, [_FakeResponse({"organizations": [{"name": "Apollo Co"}]})]
    )

    commits = []
    rollbacks = []
    monkeypatch.setattr(type(db_session), "commit", lambda self: commits.append(1))
    monkeypatch.setattr(type(db_session), "rollback", lambda self: rollbacks.append(1))

    result = await svc.search_companies(db_session, industry="Metal Stamping", per_page=5)
    assert result.status == "ok"
    assert [r.name for r in result.records] == ["Apollo Co"]
    assert commits == [], "search_companies must not commit — the caller owns the transaction"
    assert rollbacks == [], "search_companies must not roll back the caller's work"


async def test_search_companies_imports_nothing(db_session, monkeypatch):
    """It's a client now: records out, persistence is ingestion's job."""
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "fake"
    _install_fake_apollo(
        monkeypatch, [_FakeResponse({"organizations": [{"name": "Ghost Co"}]})]
    )
    await svc.search_companies(db_session, industry="x", per_page=5)

    rows = (
        await db_session.execute(select(Company).where(Company.name == "Ghost Co"))
    ).scalars().all()
    assert rows == []


async def test_industry_filter_is_absent_not_empty(db_session, monkeypatch):
    """The bug: organization_industry_tag_ids = [] read like a filter, matched
    nothing, and applied nothing.
    """
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "fake"
    captured = []
    _install_fake_apollo(monkeypatch, [_FakeResponse({"organizations": []})], captured)

    await svc.search_companies(db_session, industry="Metal Stamping", per_page=5)

    body = captured[0]
    assert "organization_industry_tag_ids" not in body, (
        "an empty list that reads as a filter is worse than no filter"
    )
    # The industry still reaches Apollo, honestly, as a keyword tag.
    assert body["q_organization_keyword_tags"] == ["Metal Stamping"]


async def test_api_key_travels_in_header_never_in_body(db_session, monkeypatch):
    """The bug: the key was POSTed in the JSON body, so Apollo answered every
    org search with 422 INVALID_API_KEY_LOCATION — "API key must be passed in
    the X-Api-Key header for security reasons".
    """
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "secret-key"
    captured, headers = [], []
    _install_fake_apollo(
        monkeypatch, [_FakeResponse({"organizations": []})], captured, headers
    )

    await svc.search_companies(db_session, industry="CNC Machining", per_page=5)

    assert headers[0].get("X-Api-Key") == "secret-key", "Apollo 422s without this header"
    assert "api_key" not in captured[0], (
        "the key in the request body is exactly what Apollo rejects"
    )


async def test_search_payload_is_shaped_the_way_apollo_expects(db_session, monkeypatch):
    """The whole org-search body, pinned. This is the heuristic-v1 ICP: an
    industry, 50-500 employees, US.
    """
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "fake"
    captured = []
    _install_fake_apollo(monkeypatch, [_FakeResponse({"organizations": []})], captured)

    await svc.search_companies(
        db_session,
        industry="Injection Molding",
        location="United States",
        min_employees=50,
        max_employees=500,
        per_page=5,
    )

    assert captured[0] == {
        "page": 1,
        "per_page": 5,
        "q_organization_keyword_tags": ["Injection Molding"],
        "organization_locations": ["United States"],
        "organization_num_employees_ranges": ["50,500"],
    }


async def test_apollo_error_body_reaches_the_ledger(db_session, monkeypatch):
    """The 422 was diagnosed as a payload bug for want of one line of evidence:
    the client recorded "HTTP 422" and dropped the body, where Apollo had
    already explained itself.
    """
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "fake"
    monkeypatch.setattr(svc.settings, "apollo_daily_credit_budget", 100)
    body = (
        '{"error":"API key must be passed in the X-Api-Key header for security '
        'reasons.","error_code":"INVALID_API_KEY_LOCATION"}'
    )
    _install_fake_apollo(monkeypatch, [_FakeResponse({}, status_code=422, text=body)])

    result = await svc.search_companies(db_session, industry="x", per_page=5)

    assert result.status == "error"
    assert "422" in result.error
    assert "INVALID_API_KEY_LOCATION" in result.error, (
        "Apollo said why; the client must not throw it away"
    )
    rows = (await db_session.execute(select(ApiCreditLedger))).scalars().all()
    assert "INVALID_API_KEY_LOCATION" in rows[0].details["error"]


async def test_permanent_4xx_is_not_retried(db_session, monkeypatch):
    """A 403 (endpoint not on this Apollo plan) is a verdict, not weather.
    Retrying it three times just spends three credits to be told the same thing.
    """
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "fake"
    monkeypatch.setattr(svc.settings, "apollo_daily_credit_budget", 100)
    body = '{"error":"not accessible with this api_key on a free plan.","error_code":"API_INACCESSIBLE"}'
    _install_fake_apollo(monkeypatch, [_FakeResponse({}, status_code=403, text=body)])

    result = await svc.search_companies(db_session, industry="x", per_page=5)

    assert result.status == "error"
    assert "free plan" in result.error, "the operator needs the reason, not just a number"
    rows = (await db_session.execute(select(ApiCreditLedger))).scalars().all()
    assert rows[0].details["attempts"] == 1, "a plan limit was retried like a blip"
    assert rows[0].credits == 1


async def test_apollo_country_is_passed_through_verbatim(db_session, monkeypatch):
    """enrichment.py:342 used to coerce non-US/CA to "US" on the way in."""
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "fake"
    _install_fake_apollo(
        monkeypatch,
        [_FakeResponse({"organizations": [{"name": "Mumbai Metals", "country": "IN"}]})],
    )
    result = await svc.search_companies(db_session, industry="x", per_page=5)
    assert result.records[0].country == "IN", "country was silently relabelled"


# ---------------------------------------------------------------------------
# 6. The real lead scorer, wired in
# ---------------------------------------------------------------------------

async def test_ingest_scores_company_post_persistence(db_session):
    """qualify() answers "does it match the ICP?"; scoring.py answers "how good
    is this lead?". Both are wanted — only CSV ever got the second one before.
    """
    outcome = await get_ingestion_service().ingest_one(
        db_session,
        CandidateRecord(
            name="Scored Co", domain="scoredco.com", country="US",
            employee_count=150, source="test",
        ),
        score=True,
    )
    scores = (
        await db_session.execute(
            select(LeadScore).where(LeadScore.company_id == outcome.company_id)
        )
    ).scalars().all()
    assert len(scores) == 1
    assert scores[0].score_type == "rule_based"


async def test_prospector_run_scores_every_company(db_session):
    """The prospector never called scoring.py at all. Now every source does."""
    run = await get_prospector().run(
        db_session,
        seed_candidates=[{
            "name": "Precision Stamping Corp", "domain": "precisionstamp.com",
            "industry": "Metal Stamping", "employee_count": 150,
            "country": "US", "source": "seed",
        }],
        trigger="manual",
    )
    assert run.status == "completed"
    cand = (
        await db_session.execute(
            select(ProspectCandidate).where(ProspectCandidate.run_id == run.id)
        )
    ).scalars().first()
    assert cand.company_id is not None
    scores = (
        await db_session.execute(
            select(LeadScore).where(LeadScore.company_id == cand.company_id)
        )
    ).scalars().all()
    assert len(scores) >= 1


# ---------------------------------------------------------------------------
# 7. Credit ledger, budget gate, backoff
# ---------------------------------------------------------------------------

async def test_budget_gate_blocks_before_spending(db_session, monkeypatch):
    """The single most important test here: the gate must refuse the call, not
    account for it afterwards. An exhausted budget costs zero.
    """
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "fake"
    monkeypatch.setattr(svc.settings, "apollo_daily_credit_budget", 2)
    monkeypatch.setattr(svc.settings, "apollo_monthly_credit_budget", 0)

    calls = []

    import httpx

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): ...
        async def post(self, *a, **k):
            calls.append(1)
            return _FakeResponse({"organizations": []})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    # Burn the budget.
    for _ in range(2):
        await api_credits.record_usage(db_session, "apollo", "org_search", credits=1)

    result = await svc.search_companies(db_session, industry="x", per_page=5)
    assert result.status == "budget_exhausted"
    assert calls == [], "the gate let a call through after the budget was gone"


async def test_budget_zero_means_unlimited(db_session, monkeypatch):
    monkeypatch.setattr(api_credits.get_settings(), "apollo_daily_credit_budget", 0)
    monkeypatch.setattr(api_credits.get_settings(), "apollo_monthly_credit_budget", 0)
    for _ in range(50):
        await api_credits.record_usage(db_session, "apollo", "org_search", credits=10)
    status = await api_credits.check_budget(db_session, "apollo", cost=1)
    assert status.allowed is True


async def test_ledger_debits_on_success(db_session, monkeypatch):
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "fake"
    monkeypatch.setattr(svc.settings, "apollo_daily_credit_budget", 100)
    _install_fake_apollo(monkeypatch, [_FakeResponse({"organizations": []})])

    await svc.search_companies(db_session, industry="x", per_page=5)
    rows = (await db_session.execute(select(ApiCreditLedger))).scalars().all()
    assert len(rows) == 1
    assert rows[0].provider == "apollo"
    assert rows[0].operation == "org_search"
    assert rows[0].status == "ok"
    # The API key must never reach the ledger.
    assert "api_key" not in rows[0].details.get("params", {})


async def test_ledger_debits_on_rate_limit_and_status_is_distinct(db_session, monkeypatch):
    """A 429 that survives its retries is 'rate_limited' — NOT 'found nothing'.
    Conflating them hides an outage as a bad ICP.
    """
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "fake"
    monkeypatch.setattr(svc.settings, "apollo_daily_credit_budget", 100)
    _install_fake_apollo(monkeypatch, [_FakeResponse({}, status_code=429)])

    result = await svc.search_companies(db_session, industry="x", per_page=5)
    assert result.status == "rate_limited"
    assert result.records == []
    assert result.status != "ok", "a rate-limited run must not look like an empty one"

    rows = (await db_session.execute(select(ApiCreditLedger))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "rate_limited"
    # Retried up to the cap, and the ledger records what it cost.
    assert rows[0].details["attempts"] == 3
    assert rows[0].credits == 3


async def test_429_then_success_retries_and_succeeds(db_session, monkeypatch):
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = "fake"
    monkeypatch.setattr(svc.settings, "apollo_daily_credit_budget", 100)
    _install_fake_apollo(
        monkeypatch,
        [
            _FakeResponse({}, status_code=429, headers={"Retry-After": "0"}),
            _FakeResponse({"organizations": [{"name": "Recovered Co"}]}),
        ],
    )
    result = await svc.search_companies(db_session, industry="x", per_page=5)
    assert result.status == "ok"
    assert [r.name for r in result.records] == ["Recovered Co"]


async def test_no_api_key_is_skipped_not_error(db_session, monkeypatch):
    """External calls fail soft — a missing key degrades, never breaks a run."""
    from app.services.enrichment import ApolloEnrichmentService

    svc = ApolloEnrichmentService()
    svc.api_key = ""
    result = await svc.search_companies(db_session, industry="x", per_page=5)
    assert result.status == "skipped"
    assert result.records == []
    rows = (await db_session.execute(select(ApiCreditLedger))).scalars().all()
    assert rows == [], "a call we never made must not be billed"


async def test_credits_endpoint_reports_spend(client, db_session):
    r = await client.get("/api/v1/prospecting/credits")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "apollo"
    assert "daily" in body and "monthly" in body
    assert body["daily"]["spent"] == 0


# ---------------------------------------------------------------------------
# Contact dedup + suppression
# ---------------------------------------------------------------------------

async def test_contact_dedup_is_global_not_per_company(db_session):
    """People change jobs. The old per-company email scope duplicated them."""
    first = _company(name="First Employer", domain="first.example")
    db_session.add(first)
    await db_session.flush()
    db_session.add(Contact(
        id=uuid.uuid4(), company_id=first.id, first_name="Dana",
        last_name="Moved", email="dana@personal.example", source="test",
    ))
    await db_session.flush()

    outcome = await get_ingestion_service().ingest_one(
        db_session,
        CandidateRecord(
            name="Second Employer", domain="second.example", source="test",
            contacts=[ContactRecord(
                first_name="Dana", last_name="Moved", email="dana@personal.example"
            )],
        ),
    )
    assert outcome.contacts_created == 0
    total = (
        await db_session.execute(
            select(func.count(Contact.id)).where(
                func.lower(Contact.email) == "dana@personal.example"
            )
        )
    ).scalar()
    assert total == 1


async def test_suppressed_contact_is_not_re_enrolled_on_reimport(db_session):
    """Re-importing a CSV used to happily resurrect a suppressed contact."""
    company = _company(name="Suppression Co", domain="suppress.example")
    db_session.add(company)
    await db_session.flush()
    db_session.add(Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Opted",
        last_name="Out", email="optedout@suppress.example",
        source="test", suppressed=True,
    ))
    await db_session.flush()

    svc = get_ingestion_service()
    outcomes = await svc.ingest_contacts(
        db_session,
        company,
        [ContactRecord(first_name="Opted", last_name="Out",
                       email="optedout@suppress.example")],
    )
    assert outcomes[0].created is False
    assert outcomes[0].reason == "suppressed"
    total = (
        await db_session.execute(
            select(func.count(Contact.id)).where(
                func.lower(Contact.email) == "optedout@suppress.example"
            )
        )
    ).scalar()
    assert total == 1


async def test_contacts_deduped_within_one_batch(db_session):
    company = _company(name="Batch Co", domain="batch.example")
    db_session.add(company)
    await db_session.flush()

    outcomes = await get_ingestion_service().ingest_contacts(
        db_session,
        company,
        [
            ContactRecord(first_name="Sam", last_name="Twice", email="sam@batch.example"),
            ContactRecord(first_name="Sam", last_name="Twice", email="SAM@batch.example"),
        ],
    )
    assert sum(1 for o in outcomes if o.created) == 1


# ---------------------------------------------------------------------------
# Routing + dry run
# ---------------------------------------------------------------------------

async def test_dedup_medium_vetoes_auto_enroll(db_session):
    """Fit alone is not enough when we aren't sure who the company even is."""
    svc = IngestionService()
    strong = {"fit_score": 95, "excluded": False}
    assert svc.route(strong, _match(DEDUP_CERTAIN), enroll_budget=5) == ROUTE_AUTO_ENROLL
    assert svc.route(strong, _match(DEDUP_MEDIUM), enroll_budget=5) == ROUTE_REVIEW


async def test_auto_enroll_threshold_is_65(db_session):
    """The Places-era threshold: a 70-fit SMB is enrollable, not just reviewable.

    Places candidates cluster in the 60s-70s, so 75 parked all of them in the
    review queue. Pinned because the exact number is a business decision, not
    an implementation detail — moving it should be a deliberate edit here.
    """
    svc = IngestionService()
    assert svc.settings.prospecting_auto_enroll_threshold == 65
    at_70 = {"fit_score": 70, "excluded": False}
    assert svc.route(at_70, _match(DEDUP_CERTAIN), enroll_budget=5) == ROUTE_AUTO_ENROLL


async def test_route_bands_at_threshold_boundaries(db_session):
    """Walk the exact edges of discard → review → auto-enroll."""
    svc = IngestionService()

    def route_at(fit: int) -> str:
        return svc.route({"fit_score": fit, "excluded": False},
                         _match(DEDUP_CERTAIN), enroll_budget=5)

    assert route_at(49) == ROUTE_DISCARD      # below review floor
    assert route_at(50) == ROUTE_REVIEW       # review floor is inclusive
    assert route_at(64) == ROUTE_REVIEW       # last point below auto-enroll
    assert route_at(65) == ROUTE_AUTO_ENROLL  # auto-enroll floor is inclusive


async def test_zero_enroll_budget_downgrades_auto_enroll_to_review(db_session):
    """The dry-run guarantee: budget 0 means nothing can enrol, however good.

    PROSPECTING_DAILY_MAX=0 is the safety recipe used to rehearse live runs, so
    the downgrade has to hold even for a candidate way over the threshold.
    """
    svc = IngestionService()
    strong = {"fit_score": 95, "excluded": False}
    assert svc.route(strong, _match(DEDUP_CERTAIN), enroll_budget=0) == ROUTE_REVIEW


async def test_dry_run_persists_nothing(db_session):
    svc = get_ingestion_service()
    icp = await get_prospector().ensure_icp(db_session)
    result = await svc.ingest(
        db_session,
        [CandidateRecord(
            name="Dry Run Co", domain="dryrun.example", industry="Metal Stamping",
            employee_count=150, country="US", source="test",
        )],
        icp=icp,
        enroll_budget=5,
        dry_run=True,
    )
    # It still tells you what *would* happen...
    assert result.outcomes[0].fit_score > 0
    # ...without writing anything.
    rows = (
        await db_session.execute(select(Company).where(Company.name == "Dry Run Co"))
    ).scalars().all()
    assert rows == []


async def test_ingest_batch_survives_one_bad_record(db_session):
    result = await get_ingestion_service().ingest(
        db_session,
        [
            CandidateRecord(name="Good Co", domain="goodco.example", source="test"),
            CandidateRecord(name="Bad Country Co", country="JP", source="test"),
            CandidateRecord(name="Other Good Co", domain="othergood.example", source="test"),
        ],
    )
    assert result.companies_created == 2
    assert len(result.errors) == 1
    assert "JP" in result.errors[0]


def _match(confidence):
    from app.services.ingestion import DedupMatch

    return DedupMatch(None, confidence)


# ---------------------------------------------------------------------------
# Four-path parity
# ---------------------------------------------------------------------------

async def test_all_paths_share_one_dedup_rule(db_session):
    """Regression parity: CSV, Apollo and the prospector must all resolve the
    same existing company from the same name — the divergence this fixes.
    """
    db_session.add(_company(name="Parity Corp", domain="parity.example"))
    await db_session.flush()

    svc = get_ingestion_service()
    for variant in ("Parity Corp", "PARITY CORP", "Parity Corporation", "parity, inc."):
        match = await svc.resolve_company(db_session, CandidateRecord(name=variant))
        assert match.is_match is True, f"{variant!r} failed to match Parity Corp"
