"""Migration 079 — platform superadmin promotion.

The test harness runs against SQLite via ``Base.metadata.create_all`` rather than
the Alembic migrations, so we exercise the migration's *effect* by running the
exact SQL the migration file executes. The email and target org are imported
from the migration module, so a drift in either (a typo'd address, wrong org id)
fails this test.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from app.org_context import APPREND_ORG_ID

# Load the numeric-prefixed migration module by path (not importable normally).
_MIG_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "079_platform_superadmin.py"
_spec = importlib.util.spec_from_file_location("migration_079", _MIG_PATH)
migration_079 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration_079)


UPGRADE_SQL = text(
    "UPDATE users SET role = 'platform_admin', org_id = NULL WHERE email = :email"
)


def test_migration_targets_the_expected_account():
    """Guards against a typo in the promoted email / target org constant."""
    assert migration_079.SUPERADMIN_EMAIL == "sumaninster7@gmail.com"
    assert uuid.UUID(migration_079.APPREND_ORG_ID) == APPREND_ORG_ID


@pytest.mark.asyncio
async def test_upgrade_promotes_to_platform_admin(db_session: AsyncSession, make_user):
    """An org_admin bound to Apprend becomes an org-less platform_admin."""
    await make_user(
        migration_079.SUPERADMIN_EMAIL,
        role=UserRole.ORG_ADMIN.value,
        org_id=APPREND_ORG_ID,
    )

    await db_session.execute(UPGRADE_SQL.bindparams(email=migration_079.SUPERADMIN_EMAIL))
    await db_session.commit()

    user = (
        await db_session.execute(
            select(User).where(User.email == migration_079.SUPERADMIN_EMAIL)
        )
    ).scalar_one()
    assert user.role == UserRole.PLATFORM_ADMIN.value
    assert user.org_id is None


@pytest.mark.asyncio
async def test_upgrade_is_a_noop_on_missing_account(db_session: AsyncSession):
    """A fresh DB with no such account matches zero rows and doesn't error."""
    result = await db_session.execute(
        UPGRADE_SQL.bindparams(email=migration_079.SUPERADMIN_EMAIL)
    )
    assert result.rowcount == 0


@pytest.mark.asyncio
async def test_upgrade_is_idempotent(db_session: AsyncSession, make_user):
    """Re-running the promotion leaves the account in the same target state."""
    await make_user(
        migration_079.SUPERADMIN_EMAIL,
        role=UserRole.PLATFORM_ADMIN.value,
        org_id=None,
    )

    await db_session.execute(UPGRADE_SQL.bindparams(email=migration_079.SUPERADMIN_EMAIL))
    await db_session.commit()

    user = (
        await db_session.execute(
            select(User).where(User.email == migration_079.SUPERADMIN_EMAIL)
        )
    ).scalar_one()
    assert user.role == UserRole.PLATFORM_ADMIN.value
    assert user.org_id is None
