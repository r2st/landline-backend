"""POST /discovery/search-companies-gemini — Discovery search on the org's Gemini key."""

import json

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.org_llm_key import OrgLlmKey
from app.org_context import APPREND_ORG_ID
from app.services import crypto, gemini_discovery, org_gemini

URL = "/api/v1/discovery/search-companies-gemini"
KEY = "test-gemini-key-123"

ANSWER = {
    "companies": [
        {
            "name": "Acme Stamping",
            "domain": "https://www.acme-stamping.com/",
            "industry": "Metal stamping",
            "city": "Dayton",
            "state": "Ohio",
            "country": "United States",
            "employee_count": "51-200",
            "description": "Progressive-die stamping for automotive.",
            "contacts": [
                {"name": "Jane Doe", "title": "COO", "email": "jane@acme-stamping.com",
                 "linkedin": "linkedin.com/in/janedoe"},
                # Off-domain address: the model's guess, not the company's.
                {"name": "Lee Wong", "title": "Owner", "email": "lee.wong@gmail.com",
                 "linkedin": None},
            ],
        },
        {"name": "Acme Stamping", "domain": "acme-stamping.com"},  # duplicate
        {"name": "", "domain": "nameless.com"},
        {"name": "Giant Corp", "domain": "giant.com", "employee_count": 50000},
        {"name": "Beta Molding", "domain": "betamolding.com", "employee_count": None,
         "contacts": "not a list"},
    ]
}


@pytest.fixture(autouse=True)
def _crypto_key():
    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


@pytest.fixture(autouse=True)
def _reset_org_breaker():
    org_gemini.reset_breaker()
    yield
    org_gemini.reset_breaker()


class _FakeGemini:
    """Stands in for ``httpx.AsyncClient`` inside ``org_gemini``."""

    requests: list = []
    status = 200
    text = json.dumps(ANSWER)
    sources = [{"web": {"uri": "https://example.com/a", "title": "acme-stamping.com"}}]

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).requests.append({"url": url, "headers": headers, "json": json})
        body = {
            "candidates": [{
                "content": {"parts": [{"text": type(self).text}]},
                "groundingMetadata": {"groundingChunks": type(self).sources},
            }]
        }
        return httpx.Response(
            type(self).status, json=body, request=httpx.Request("POST", url),
        )


@pytest.fixture
def gemini(monkeypatch):
    _FakeGemini.requests = []
    _FakeGemini.status = 200
    _FakeGemini.text = json.dumps(ANSWER)
    monkeypatch.setattr(org_gemini.httpx, "AsyncClient", _FakeGemini)
    return _FakeGemini


async def _save_key(db_session):
    db_session.add(OrgLlmKey(
        org_id=APPREND_ORG_ID, provider="gemini",
        key_encrypted=crypto.encrypt(KEY), key_hint="••••",
    ))
    await db_session.commit()


SEARCH = {"industry": "metal stamping", "location": "Ohio", "keywords": ["automotive"],
          "max_employees": 1000, "per_page": 10}


@pytest.mark.asyncio
async def test_without_a_key_reports_key_required_and_calls_nothing(client, gemini):
    resp = await client.post(URL, json=SEARCH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["search_status"] == "skipped"
    assert body["search_error_code"] == "gemini_key_required"
    assert "Settings" in body["search_error"]
    assert body["companies"] == []
    assert gemini.requests == []


@pytest.mark.asyncio
async def test_search_imports_validated_companies_in_apollo_shape(client, db_session, gemini):
    await _save_key(db_session)
    resp = await client.post(URL, json=SEARCH)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["source"] == "gemini"
    assert body["search_status"] == "ok"
    assert body["search_error"] is None
    # Duplicate, nameless and over-range (50k > max 1000) entries are dropped;
    # an unknown employee count stays.
    assert [c["name"] for c in body["companies"]] == ["Acme Stamping", "Beta Molding"]
    assert body["total"] == 2 and body["imported"] == 2
    assert body["sources"] == [{"title": "acme-stamping.com", "url": "https://example.com/a"}]

    acme = body["companies"][0]
    for k in ("name", "domain", "status", "id", "industry", "employee_count", "city",
              "state", "dedup_confidence", "error"):
        assert k in acme
    assert acme["domain"] == "acme-stamping.com"
    assert acme["status"] == "imported"
    assert acme["employee_count"] == 200
    assert acme["location"] == "Dayton, Ohio, United States"
    assert acme["description"].startswith("Progressive-die")
    assert acme["contacts"][0] == {
        "name": "Jane Doe", "title": "COO", "email": "jane@acme-stamping.com",
        "linkedin": "https://linkedin.com/in/janedoe",
    }
    # The off-domain guess is not kept as an email.
    assert acme["contacts"][1]["email"] is None

    company = (await db_session.execute(
        select(Company).where(Company.domain == "acme-stamping.com")
    )).scalars().first()
    assert company is not None and company.source == "gemini"
    emails = {c.email for c in (await db_session.execute(
        select(Contact).where(Contact.company_id == company.id)
    )).scalars()}
    assert "lee.wong@gmail.com" not in emails
    assert "jane@acme-stamping.com" in emails


@pytest.mark.asyncio
async def test_request_is_grounded_and_key_travels_in_a_header(client, db_session, gemini):
    await _save_key(db_session)
    await client.post(URL, json=SEARCH)
    (req,) = gemini.requests
    assert req["json"]["tools"] == [{"google_search": {}}]
    assert "responseMimeType" not in req["json"]["generationConfig"]
    assert req["headers"]["x-goog-api-key"] == KEY
    assert KEY not in req["url"]
    assert req["url"].endswith(f"/models/{get_settings().discovery_gemini_model}:generateContent")
    prompt = req["json"]["contents"][0]["parts"][0]["text"]
    assert "metal stamping" in prompt and "Ohio" in prompt and "automotive" in prompt


@pytest.mark.asyncio
async def test_existing_company_reads_as_exists(client, db_session, gemini):
    await _save_key(db_session)
    first = (await client.post(URL, json=SEARCH)).json()
    assert first["imported"] == 2
    second = (await client.post(URL, json=SEARCH)).json()
    assert second["imported"] == 0
    assert second["already_existed"] == 2
    assert {c["status"] for c in second["companies"]} == {"exists"}


@pytest.mark.asyncio
async def test_source_param_on_search_companies_routes_to_gemini(client, db_session, gemini):
    await _save_key(db_session)
    resp = await client.post("/api/v1/discovery/search-companies", json={**SEARCH, "source": "gemini"})
    assert resp.status_code == 200
    assert resp.json()["source"] == "gemini"
    assert len(gemini.requests) == 1


@pytest.mark.asyncio
async def test_prose_wrapped_json_is_still_parsed(client, db_session, gemini):
    await _save_key(db_session)
    gemini.text = "Here are the companies I found:\n```json\n" + json.dumps(ANSWER) + "\n```"
    body = (await client.post(URL, json=SEARCH)).json()
    assert body["search_status"] == "ok"
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_malformed_answer_is_a_readable_error(client, db_session, gemini):
    await _save_key(db_session)
    gemini.text = "Sorry, I could not find any companies."
    body = (await client.post(URL, json=SEARCH)).json()
    assert body["search_status"] == "error"
    assert "malformed" in body["search_error"].lower()
    assert body["companies"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,phrase", [(429, "quota"), (403, "refused"), (503, "unavailable")])
async def test_upstream_errors_are_readable_and_never_carry_the_key(
    client, db_session, gemini, status, phrase
):
    await _save_key(db_session)
    gemini.status = status
    resp = await client.post(URL, json=SEARCH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["search_status"] == "error"
    assert phrase in body["search_error"].lower()
    assert KEY not in resp.text
    count = (await db_session.execute(select(Company.id))).scalars().all()
    assert count == []


@pytest.mark.asyncio
async def test_empty_answer_is_an_error(client, db_session, gemini):
    await _save_key(db_session)
    gemini.text = ""
    body = (await client.post(URL, json=SEARCH)).json()
    assert body["search_status"] == "error"
    assert "empty" in body["search_error"].lower()


@pytest.mark.asyncio
async def test_requires_auth(unauth_client):
    resp = await unauth_client.post(URL, json=SEARCH)
    assert resp.status_code == 401


@pytest.mark.parametrize("raw,expected", [
    (250, 250), ("250", 250), ("1,200", 1200), ("51-200", 200), ("500+", 500),
    (None, None), ("unknown", None), (0, None), (True, None),
])
def test_parse_employee_count(raw, expected):
    assert gemini_discovery.parse_employee_count(raw) == expected
