"""Credentials an operator can rotate from the dashboard instead of over SSH.

Two halves, and the second is the one with teeth:

* **Resolution.** A row in ``app_credentials`` overrides the environment for its
  key; no row means the deployed value stands. Clearing an override has to
  restore the environment value, which is only possible because the store
  snapshots what the environment gave it *before* the first hydrate overwrites
  the attribute — by the second hydrate the attribute holds the previous
  override, and a store that read it then would "restore" to that. This is the
  failure you would meet while trying to back out a bad credential, i.e. at the
  worst possible moment, so it is pinned directly.

* **Exposure.** These identify the deployment to Google and to the LLM
  providers. A secret must never come back out of the API — not on read, not in
  the response to the write that set it — and only a ``platform_admin`` may
  read or write. An ``org_admin`` who could write ``google_client_id`` could
  point the consent flow at a client they control and harvest mailbox grants,
  which is why the role gate is asserted per route rather than trusted to the
  mount.
"""

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.app_credential import AppCredential
from app.models.user import UserRole
from app.services import credential_store, crypto

NEW_CLIENT_ID = "000000000000-testclient.apps.googleusercontent.com"
ENV_CLIENT_ID = "env-deployed-client.apps.googleusercontent.com"
ENV_SECRET = "GOCSPX-env-deployed-secret-value"


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
def _env_baseline():
    """Stand in for what the deploy put in ``.env``, and restore after.

    The store's baseline is captured once per process, so a test that changed
    ``Settings`` without resetting it would leak its value into every later
    test as "the environment value" — the same class of bug as the two-clock
    reading in the daily-summary tests.
    """
    settings = get_settings()
    previous = (settings.google_client_id, settings.google_client_secret)
    saved_baseline = dict(credential_store._ENV_BASELINE)

    credential_store._ENV_BASELINE.clear()
    settings.google_client_id = ENV_CLIENT_ID
    settings.google_client_secret = ENV_SECRET
    yield
    credential_store._ENV_BASELINE.clear()
    credential_store._ENV_BASELINE.update(saved_baseline)
    settings.google_client_id, settings.google_client_secret = previous


@pytest_asyncio.fixture
async def platform_admin(make_user):
    return await make_user(
        "root@landline.app", role=UserRole.PLATFORM_ADMIN.value, org_id=None
    )


# --------------------------------------------------------------------------- #
# Resolution: database over environment, and back again
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_with_no_override_the_environment_value_stands(db_session: AsyncSession):
    """An empty table is a no-op — nothing about an existing install changes."""
    await credential_store.hydrate(db_session)
    assert get_settings().google_client_id == ENV_CLIENT_ID


@pytest.mark.asyncio
async def test_a_stored_override_replaces_the_environment_value(db_session: AsyncSession):
    await credential_store.set_credential(
        db_session, "google_client_id", NEW_CLIENT_ID, updated_by="root@landline.app"
    )
    assert get_settings().google_client_id == NEW_CLIENT_ID


@pytest.mark.asyncio
async def test_clearing_an_override_restores_the_deployed_value(db_session: AsyncSession):
    """The reason the store snapshots the environment before the first hydrate.

    After one hydrate the attribute holds the override, so a store that read it
    back to find "the environment value" would restore to the override — and
    the operator backing out a bad credential would find it still live.
    """
    await credential_store.set_credential(db_session, "google_client_id", NEW_CLIENT_ID)
    assert get_settings().google_client_id == NEW_CLIENT_ID

    await credential_store.clear_credential(db_session, "google_client_id")
    assert get_settings().google_client_id == ENV_CLIENT_ID


@pytest.mark.asyncio
async def test_setting_one_credential_leaves_the_others_alone(db_session: AsyncSession):
    """``apply_overrides`` writes every managed key, so this is not free."""
    await credential_store.set_credential(db_session, "google_client_id", NEW_CLIENT_ID)
    assert get_settings().google_client_secret == ENV_SECRET


@pytest.mark.asyncio
async def test_the_value_is_encrypted_at_rest(db_session: AsyncSession):
    """A database dump must not be a credential dump."""
    await credential_store.set_credential(
        db_session, "google_client_secret", "GOCSPX-plaintext-would-be-here"
    )
    row = (
        await db_session.execute(
            select(AppCredential).where(AppCredential.key == "google_client_secret")
        )
    ).scalar_one()
    assert "GOCSPX-plaintext-would-be-here" not in row.value_encrypted
    assert crypto.decrypt(row.value_encrypted) == "GOCSPX-plaintext-would-be-here"


@pytest.mark.asyncio
async def test_a_second_write_replaces_rather_than_duplicates(db_session: AsyncSession):
    """``key`` is unique — two rows would make the live value row-order dependent."""
    await credential_store.set_credential(db_session, "google_client_id", "first-id")
    await credential_store.set_credential(db_session, "google_client_id", "second-id")

    rows = (
        await db_session.execute(
            select(AppCredential).where(AppCredential.key == "google_client_id")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert get_settings().google_client_id == "second-id"


@pytest.mark.asyncio
async def test_an_undecryptable_row_falls_back_instead_of_raising(db_session: AsyncSession):
    """A rotated Fernet key must not stop the app booting.

    The environment value is a working fallback, and an API that will not start
    is worse than one running on the deployed credentials.
    """
    db_session.add(AppCredential(
        key="google_client_id", value_encrypted="not-a-fernet-token",
    ))
    await db_session.commit()

    overrides = await credential_store.hydrate(db_session)
    assert "google_client_id" not in overrides
    assert get_settings().google_client_id == ENV_CLIENT_ID


@pytest.mark.asyncio
async def test_a_key_outside_the_registry_cannot_be_written(db_session: AsyncSession):
    """The allowlist is what keeps this from being a settings-write primitive."""
    with pytest.raises(KeyError):
        await credential_store.set_credential(db_session, "secret_key", "pwned")


@pytest.mark.asyncio
async def test_an_empty_value_is_refused(db_session: AsyncSession):
    """"Set it to nothing" is ambiguous; clearing is its own operation."""
    with pytest.raises(ValueError):
        await credential_store.set_credential(db_session, "google_client_id", "   ")


def test_masking_does_not_partially_reveal_a_short_secret():
    assert credential_store.mask("") == ""
    assert "abcdef" not in credential_store.mask("abcdef")
    masked = credential_store.mask("GOCSPX-averylongsecretvalue")
    assert masked.endswith("alue") and "averylong" not in masked


# --------------------------------------------------------------------------- #
# Exposure: who may look, and what comes back
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_platform_admin_sees_every_managed_credential(
    client: AsyncClient, platform_admin, auth_headers
):
    resp = await client.get(
        "/api/v1/platform/credentials", headers=auth_headers(platform_admin)
    )
    assert resp.status_code == 200, resp.text
    keys = {c["key"] for c in resp.json()["credentials"]}
    assert {"google_client_id", "google_client_secret", "openrouter_api_key"} <= keys


@pytest.mark.asyncio
async def test_the_secret_never_comes_back(
    client: AsyncClient, db_session: AsyncSession, platform_admin, auth_headers
):
    """Not on read, and not in the response to the write that set it."""
    h = auth_headers(platform_admin)
    secret = "GOCSPX-test-secret-should-never-appear"

    written = await client.put(
        "/api/v1/platform/credentials/google_client_secret",
        json={"value": secret}, headers=h,
    )
    assert written.status_code == 200, written.text
    assert secret not in written.text

    listed = await client.get("/api/v1/platform/credentials", headers=h)
    assert secret not in listed.text
    entry = next(
        c for c in listed.json()["credentials"] if c["key"] == "google_client_secret"
    )
    assert entry["source"] == "database"
    assert entry["value"].endswith(secret[-4:])  # enough to identify, not to use


@pytest.mark.asyncio
async def test_a_client_id_is_shown_in_full(
    client: AsyncClient, platform_admin, auth_headers
):
    """It is in the consent URL every user sees; masking it defeats the screen.

    Confirming *which* Google project is live without SSHing to the box is the
    whole point of this page.
    """
    h = auth_headers(platform_admin)
    await client.put(
        "/api/v1/platform/credentials/google_client_id",
        json={"value": NEW_CLIENT_ID}, headers=h,
    )
    listed = await client.get("/api/v1/platform/credentials", headers=h)
    entry = next(
        c for c in listed.json()["credentials"] if c["key"] == "google_client_id"
    )
    assert entry["value"] == NEW_CLIENT_ID
    assert entry["is_secret"] is False


@pytest.mark.asyncio
async def test_the_source_distinguishes_deployed_from_dashboard(
    client: AsyncClient, platform_admin, auth_headers
):
    """"Which credential is live, and where did it come from?" in one field."""
    h = auth_headers(platform_admin)
    listed = await client.get("/api/v1/platform/credentials", headers=h)
    entry = next(
        c for c in listed.json()["credentials"] if c["key"] == "google_client_id"
    )
    assert entry["source"] == "environment"
    assert entry["has_env_fallback"] is True

    await client.put(
        "/api/v1/platform/credentials/google_client_id",
        json={"value": NEW_CLIENT_ID}, headers=h,
    )
    listed = await client.get("/api/v1/platform/credentials", headers=h)
    entry = next(
        c for c in listed.json()["credentials"] if c["key"] == "google_client_id"
    )
    assert entry["source"] == "database"


@pytest.mark.asyncio
async def test_delete_restores_the_environment_source(
    client: AsyncClient, platform_admin, auth_headers
):
    h = auth_headers(platform_admin)
    await client.put(
        "/api/v1/platform/credentials/google_client_id",
        json={"value": NEW_CLIENT_ID}, headers=h,
    )
    resp = await client.delete("/api/v1/platform/credentials/google_client_id", headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "environment"
    assert get_settings().google_client_id == ENV_CLIENT_ID


@pytest.mark.asyncio
async def test_who_changed_it_is_recorded(
    client: AsyncClient, platform_admin, auth_headers
):
    """Rotating a credential is a change someone has to be able to attribute."""
    h = auth_headers(platform_admin)
    await client.put(
        "/api/v1/platform/credentials/google_client_id",
        json={"value": NEW_CLIENT_ID}, headers=h,
    )
    listed = await client.get("/api/v1/platform/credentials", headers=h)
    entry = next(
        c for c in listed.json()["credentials"] if c["key"] == "google_client_id"
    )
    assert entry["updated_by"] == "root@landline.app"
    assert entry["updated_at"] is not None


@pytest.mark.asyncio
async def test_an_org_admin_is_refused_every_route(
    client: AsyncClient, make_user, auth_headers
):
    """The gate that stops a tenant repointing the platform's consent flow."""
    admin = await make_user("orgadmin@alpha.com", role=UserRole.ORG_ADMIN.value)
    h = auth_headers(admin)

    assert (await client.get("/api/v1/platform/credentials", headers=h)).status_code == 403
    assert (
        await client.put(
            "/api/v1/platform/credentials/google_client_id",
            json={"value": "attacker-client-id"}, headers=h,
        )
    ).status_code == 403
    assert (
        await client.delete("/api/v1/platform/credentials/google_client_id", headers=h)
    ).status_code == 403


@pytest.mark.asyncio
async def test_an_org_agent_is_refused_too(client: AsyncClient, make_user, auth_headers):
    agent = await make_user("agent@alpha.com", role=UserRole.AGENT.value)
    resp = await client.get(
        "/api/v1/platform/credentials", headers=auth_headers(agent)
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_an_unknown_key_is_a_404_not_a_write(
    client: AsyncClient, db_session: AsyncSession, platform_admin, auth_headers
):
    h = auth_headers(platform_admin)
    resp = await client.put(
        "/api/v1/platform/credentials/secret_key", json={"value": "pwned"}, headers=h
    )
    assert resp.status_code == 404
    rows = (await db_session.execute(select(AppCredential))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_the_endpoint_requires_authentication(client: AsyncClient):
    assert (await client.get("/api/v1/platform/credentials")).status_code in (401, 403)
