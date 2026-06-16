"""Shared test fixtures for Landline backend tests."""

import asyncio
import json
import uuid
import uuid as uuid_mod
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import Text, String, event, text, TypeDecorator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from httpx import AsyncClient, ASGITransport

from app.database import Base, get_db
from app.main import app
from app.models.company import Company
from app.models.contact import Contact
from app.models.consent import ConsentRecord


# Use SQLite for tests (async via aiosqlite)
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


class SQLiteUUID(TypeDecorator):
    """Store UUID as a plain string in SQLite."""
    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return uuid_mod.UUID(value)
        return value


class SQLiteJSON(TypeDecorator):
    """Store dicts/lists as JSON strings in SQLite."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return json.dumps(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return json.loads(value)
        return value


# --- SQLite compatibility: patch PG-only types before table creation ---

_patched = False


def _patch_pg_types_for_sqlite():
    """Replace PostgreSQL-specific column types (ARRAY, JSONB, UUID)
    with SQLite-compatible types across all mapped tables, and strip
    all check constraints that use PG-specific SQL syntax."""
    global _patched
    if _patched:
        return
    _patched = True

    from sqlalchemy import CheckConstraint

    for table in Base.metadata.tables.values():
        # Remove check constraints (they use PG syntax like IN (...))
        to_remove = [
            c for c in table.constraints
            if isinstance(c, CheckConstraint)
        ]
        for c in to_remove:
            table.constraints.discard(c)

        # Patch column types by class name (avoids PG vs generic import issues)
        for col in table.columns:
            type_name = type(col.type).__name__
            if type_name == "ARRAY":
                col.type = SQLiteJSON()
            elif type_name in ("JSONB", "JSON"):
                col.type = SQLiteJSON()
            elif type_name == "UUID":
                col.type = SQLiteUUID()

            # Remove server_default that uses PG functions (gen_random_uuid, now())
            if col.server_default is not None:
                sd_text = str(getattr(col.server_default, "arg", ""))
                if "gen_random_uuid" in sd_text or "uuid" in sd_text.lower():
                    col.server_default = None


@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def db_engine():
    """Create a fresh test database engine per test."""
    _patch_pg_types_for_sqlite()

    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(db_engine):
    """Create a database session for a test."""
    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def client(db_engine):
    """HTTP test client with overridden database dependency."""
    session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def sample_company(db_session: AsyncSession) -> Company:
    """Create a sample US manufacturing company."""
    company = Company(
        id=uuid.uuid4(),
        name="Acme Stamping Co.",
        domain="acmestamp.com",
        city="Cleveland",
        state_province="OH",
        country="US",
        employee_count=150,
        naics_code="3321",
        naics_description="Forging and Stamping",
        industry_segment="Metal Stamping",
        source="test",
        website="https://acmestamp.com",
        phone="216-555-0100",
    )
    db_session.add(company)
    await db_session.flush()
    return company


@pytest_asyncio.fixture
async def sample_contact(db_session: AsyncSession, sample_company: Company) -> Contact:
    """Create a sample contact at the sample company."""
    contact = Contact(
        id=uuid.uuid4(),
        company_id=sample_company.id,
        first_name="John",
        last_name="Smith",
        title="VP Manufacturing",
        email="john.smith@acmestamp.com",
        phone="216-555-0101",
        is_primary=True,
        source="test",
    )
    db_session.add(contact)
    await db_session.flush()
    return contact


@pytest_asyncio.fixture
async def canadian_company(db_session: AsyncSession) -> Company:
    """Create a sample Canadian company (for CASL testing)."""
    company = Company(
        id=uuid.uuid4(),
        name="Maple Fabrication Ltd.",
        domain="maplefab.ca",
        city="Toronto",
        state_province="ON",
        country="CA",
        employee_count=80,
        naics_code="3323",
        naics_description="Architectural and Structural Metals Manufacturing",
        source="test",
    )
    db_session.add(company)
    await db_session.flush()
    return company


@pytest_asyncio.fixture
async def canadian_contact(
    db_session: AsyncSession, canadian_company: Company
) -> Contact:
    """Create a contact at the Canadian company."""
    contact = Contact(
        id=uuid.uuid4(),
        company_id=canadian_company.id,
        first_name="Marie",
        last_name="Tremblay",
        title="Plant Manager",
        email="marie@maplefab.ca",
        source="test",
    )
    db_session.add(contact)
    await db_session.flush()
    return contact
