"""POST /discovery/documents/batch — several files in one upload, one document each."""

import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models.discovery_document import DiscoveryDocument
from app.models.org_llm_key import OrgLlmKey
from app.org_context import APPREND_ORG_ID
from app.routers import discovery_documents as router_mod
from app.services import crypto, org_gemini

URL = "/api/v1/discovery/documents/batch"

CSV = (
    "Company,Website,Contact,Email,Title\n"
    "Acme Stamping,https://www.acme-stamping.com,Jane Doe,jane@acme-stamping.com,COO\n"
    "Beta Molding,,Lee Wong,lee.wong@gmail.com,Owner\n"
).encode()

CSV_MAPPING = {
    "has_header": True,
    "mapping": {"company_name": 0, "website": 1, "full_name": 2, "email": 3, "title": 4},
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


@pytest.fixture
def fake_gemini(monkeypatch):
    calls = []

    async def fake_chat(messages, *, providers, **kw):
        calls.append(messages[0]["content"])
        return json.dumps(CSV_MAPPING), providers[0].name, providers[0].models[0]

    monkeypatch.setattr(org_gemini, "chat_completion", fake_chat)
    return calls


@pytest.fixture
def bg_sessions(db_engine, monkeypatch):
    monkeypatch.setattr(
        router_mod, "session_factory",
        async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False),
    )


async def _save_key(db_session):
    db_session.add(OrgLlmKey(
        org_id=APPREND_ORG_ID, provider="gemini",
        key_encrypted=crypto.encrypt("test-gemini-key-123"), key_hint="••••",
    ))
    await db_session.commit()


async def _doc_count(db_session) -> int:
    return len((await db_session.execute(select(DiscoveryDocument.id))).all())


@pytest.mark.asyncio
async def test_each_file_becomes_its_own_processed_document(client, db_session, fake_gemini, bg_sessions):
    await _save_key(db_session)
    resp = await client.post(URL, files=[
        ("files", ("east.csv", CSV, "text/csv")),
        ("files", ("west.csv", CSV, "text/csv")),
        ("files", ("notes.txt", b"Call Acme Stamping about the new press line.", "text/plain")),
    ])
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert [d["filename"] for d in body["documents"]] == ["east.csv", "west.csv", "notes.txt"]
    assert body["rejected"] == []
    assert len({d["id"] for d in body["documents"]}) == 3

    # Every document was extracted by the background jobs.
    for d in body["documents"][:2]:
        detail = (await client.get(f"/api/v1/discovery/documents/{d['id']}")).json()
        assert detail["status"] == "ready", detail
        assert detail["candidate_count"] == 2

    listed = (await client.get("/api/v1/discovery/documents")).json()
    assert {d["filename"] for d in listed} >= {"east.csv", "west.csv", "notes.txt"}


@pytest.mark.asyncio
async def test_bad_files_are_reported_and_good_ones_still_upload(client, db_session, fake_gemini, bg_sessions):
    await _save_key(db_session)
    resp = await client.post(URL, files=[
        ("files", ("leads.csv", CSV, "text/csv")),
        ("files", ("fake.pdf", b"not a pdf", "application/pdf")),
        ("files", ("tool.exe", b"MZ", "application/octet-stream")),
    ])
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert [d["filename"] for d in body["documents"]] == ["leads.csv"]
    assert [r["filename"] for r in body["rejected"]] == ["fake.pdf", "tool.exe"]
    assert [r["index"] for r in body["rejected"]] == [1, 2]
    assert all(r["error"] for r in body["rejected"])
    assert await _doc_count(db_session) == 1


@pytest.mark.asyncio
async def test_all_files_bad_is_422_and_stores_nothing(client, db_session):
    await _save_key(db_session)
    resp = await client.post(URL, files=[
        ("files", ("fake.pdf", b"not a pdf", "application/pdf")),
        ("files", ("tool.exe", b"MZ", "application/octet-stream")),
    ])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["message"]
    assert [r["index"] for r in detail["rejected"]] == [0, 1]
    assert await _doc_count(db_session) == 0


@pytest.mark.asyncio
async def test_too_many_files_is_422(client, db_session):
    await _save_key(db_session)
    files = [("files", (f"f{i}.csv", CSV, "text/csv")) for i in range(router_mod.MAX_BATCH_FILES + 1)]
    resp = await client.post(URL, files=files)
    assert resp.status_code == 422
    assert await _doc_count(db_session) == 0


@pytest.mark.asyncio
async def test_without_gemini_key_is_409(client, db_session):
    resp = await client.post(URL, files=[("files", ("leads.csv", CSV, "text/csv"))])
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "gemini_key_required"
    assert await _doc_count(db_session) == 0


@pytest.mark.asyncio
async def test_requires_auth(unauth_client):
    resp = await unauth_client.post(URL, files=[("files", ("leads.csv", CSV, "text/csv"))])
    assert resp.status_code == 401
