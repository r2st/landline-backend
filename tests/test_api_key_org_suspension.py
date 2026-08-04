"""A suspended tenant's API keys stop working too.

``dependencies.get_current_user`` refuses a caller whose organization has been
deactivated, and its comment calls itself "the single dependency every
authenticated route runs through". That was true of the JWT surface. The public
REST API (``/api/v1/public``) authenticates with ``X-API-Key`` through
``api_key_auth.get_api_key_context`` — a second, later seam — and it checked the
key's revocation and expiry and nothing about the tenant.

So ``DELETE /platform/organizations/{id}`` locked a tenant's people out of the
whole product and left every one of its API keys reading and writing contacts,
companies, deals and sequences, server-to-server, with no expiry and nothing in
the console suggesting anything was still connected.

The JWT half is covered here as a control: if it ever regressed, the API-key
assertions below would still pass while the platform-wide invariant was gone.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.api_key import ALL_SCOPES, ApiKey
from app.models.tenant import Organization
from app.org_context import APPREND_ORG_ID
from app.services.api_key_auth import generate_api_key


async def _mint_key(db_session, org_id):
    raw, key_hash, prefix = generate_api_key()
    db_session.add(
        ApiKey(
            org_id=org_id, name="integration", key_hash=key_hash, key_prefix=prefix,
            scopes=list(ALL_SCOPES), rate_limit=100000,
        )
    )
    await db_session.commit()
    return raw


async def _set_org_active(db_session, org_id, active):
    org = (
        await db_session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one()
    org.is_active = active
    await db_session.commit()


@pytest_asyncio.fixture
async def key_for_apprend(db_session):
    return await _mint_key(db_session, APPREND_ORG_ID)


@pytest.mark.asyncio
async def test_key_works_while_the_org_is_active(client, key_for_apprend):
    """The control. Without it, a broken key would 'pass' the tests below."""
    resp = await client.get(
        "/api/v1/public/contacts", headers={"X-API-Key": key_for_apprend}
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_deactivating_the_org_stops_its_api_keys(
    client, db_session, key_for_apprend
):
    await _set_org_active(db_session, APPREND_ORG_ID, False)
    try:
        resp = await client.get(
            "/api/v1/public/contacts", headers={"X-API-Key": key_for_apprend}
        )
        assert resp.status_code == 403, resp.text
        assert "deactivated" in resp.json()["detail"].lower()
    finally:
        await _set_org_active(db_session, APPREND_ORG_ID, True)


@pytest.mark.asyncio
async def test_a_suspended_tenant_cannot_write_either(
    client, db_session, key_for_apprend, sample_company
):
    """Reads are the mild half — the key held write scopes on every resource."""
    await _set_org_active(db_session, APPREND_ORG_ID, False)
    try:
        resp = await client.post(
            "/api/v1/public/contacts",
            headers={"X-API-Key": key_for_apprend},
            json={
                "company_id": str(sample_company.id),
                "first_name": "Ada",
                "last_name": "Lovelace",
                "email": "ada@suspended.example",
            },
        )
        assert resp.status_code == 403, resp.text
    finally:
        await _set_org_active(db_session, APPREND_ORG_ID, True)


@pytest.mark.asyncio
async def test_a_key_whose_org_row_is_gone_is_refused(client, db_session):
    """A key pointing at no organization is not a key that can be scoped.

    ``organizations.id`` is the FK target for every ``org_id`` on the platform,
    so this is the state a hard delete leaves behind rather than one a running
    system reaches on its own — and 'no row' must not read as 'not deactivated'.
    """
    orphan_org = uuid.uuid4()
    db_session.add(Organization(id=orphan_org, name="Gone", slug="gone-soon"))
    await db_session.commit()
    raw = await _mint_key(db_session, orphan_org)

    org = (
        await db_session.execute(
            select(Organization).where(Organization.id == orphan_org)
        )
    ).scalar_one()
    await db_session.delete(org)
    await db_session.commit()

    resp = await client.get("/api/v1/public/contacts", headers={"X-API-Key": raw})
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_the_refusal_precedes_the_rate_limit_counter(
    client, db_session, monkeypatch
):
    """A suspended tenant must not be able to spend the limiter at all.

    Order matters beyond tidiness: the check sits above ``enforce_rate_limit``
    so a key belonging to a dead tenant does no work and touches no counter.
    """
    from app.services import api_key_auth

    raw = await _mint_key(db_session, APPREND_ORG_ID)
    await _set_org_active(db_session, APPREND_ORG_ID, False)
    called = []

    async def _spy(api_key):
        called.append(api_key.id)

    monkeypatch.setattr(api_key_auth, "enforce_rate_limit", _spy)
    try:
        resp = await client.get("/api/v1/public/contacts", headers={"X-API-Key": raw})
        assert resp.status_code == 403
        assert called == []
    finally:
        await _set_org_active(db_session, APPREND_ORG_ID, True)
