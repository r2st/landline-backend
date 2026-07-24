"""The default Places search grid.

``discovery_searches`` was empty in production (audited 2026-07-24). The weekly
sourcing sweep selects from that table, so an empty one means the sweep runs,
finds nothing due, and reports success — contact acquisition had been dead since
2026-07-16 with nothing in the logs to say so. 39 contacts existed in total and
30 of them were already enrolled.

The two properties that matter here are that the grid is *complete* (every
vertical x every metro, because coverage is the product) and that seeding is
*idempotent and non-destructive* — a deactivated cell is an operator's decision,
not damage for a seeder to repair.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.discovery_search import DISCOVERY_SCHEDULES, DiscoverySearch
from app.services.discovery_seed import (
    METROS,
    VERTICAL_QUERIES,
    default_grid,
    ensure_default_searches,
)


# ---------------------------------------------------------------------------
# Grid shape
# ---------------------------------------------------------------------------
def test_grid_is_the_full_product_of_verticals_and_metros():
    grid = default_grid()
    assert len(grid) == len(VERTICAL_QUERIES) * len(METROS)
    assert len({cell["name"] for cell in grid}) == len(grid), "cell names collide"


def test_every_vertical_has_a_places_query():
    """A vertical with no query silently drops a quarter of the grid."""
    for key, query in VERTICAL_QUERIES.items():
        assert query and query.strip(), f"{key} has no Places textQuery"


def test_grid_verticals_match_the_sequences_contacts_get_enrolled_in():
    """A cell keyed to a vertical with no sequence sources contacts nobody mails."""
    from app.services.industry_verticals import VERTICALS_BY_KEY

    for key in VERTICAL_QUERIES:
        assert key in VERTICALS_BY_KEY, f"no IndustryVertical for grid key {key!r}"


def test_grid_cells_are_storable():
    """Schedule and country must satisfy the table's own constraints."""
    for cell in default_grid():
        assert cell["schedule"] in DISCOVERY_SCHEDULES
        assert len(cell["country"]) == 2
        assert len(cell["name"]) <= 255
        assert len(cell["query"]) <= 500
        assert len(cell["location_text"]) <= 255


def test_grid_is_weekly_not_daily():
    """Local business data changes slowly; daily pays 7x for the same rows."""
    assert all(cell["schedule"] == "weekly" for cell in default_grid())


def test_subsetting_the_grid_is_supported():
    grid = default_grid(verticals=["cnc_machining"], metros=["Detroit, Michigan"])
    assert len(grid) == 1
    assert grid[0]["query"] == VERTICAL_QUERIES["cnc_machining"]
    assert grid[0]["location_text"] == "Detroit, Michigan"


def test_unknown_vertical_yields_a_partial_grid_not_a_crash():
    """Sourcing is the bottleneck; a partial grid beats none."""
    grid = default_grid(verticals=["cnc_machining", "not_a_vertical"], metros=["Chicago, Illinois"])
    assert len(grid) == 1


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_seeding_an_empty_org_creates_the_whole_grid(db_session):
    org_id = uuid.UUID("a9910000-0000-4000-8000-000000000001")
    result = await ensure_default_searches(
        db_session, org_id, verticals=["cnc_machining"], metros=["Detroit, Michigan"]
    )
    await db_session.flush()

    assert result["created"] == 1
    rows = (
        await db_session.execute(
            select(DiscoverySearch).where(DiscoverySearch.name == "cnc_machining:Detroit")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].is_active is True
    assert rows[0].schedule == "weekly"
    assert rows[0].meta["vertical"] == "cnc_machining"


@pytest.mark.asyncio
async def test_seeding_twice_creates_nothing_the_second_time(db_session):
    org_id = uuid.UUID("a9910000-0000-4000-8000-000000000001")
    kw = dict(verticals=["cnc_machining"], metros=["Chicago, Illinois"])

    first = await ensure_default_searches(db_session, org_id, **kw)
    await db_session.flush()
    second = await ensure_default_searches(db_session, org_id, **kw)
    await db_session.flush()

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["existing"] == 1


@pytest.mark.asyncio
async def test_seeding_never_reactivates_a_cell_an_operator_switched_off(db_session):
    """The whole reason this is not on the weekly beat.

    A metro that returns nothing but noise gets deactivated on purpose. A seeder
    that "repaired" that every week would overrule the operator forever.
    """
    org_id = uuid.UUID("a9910000-0000-4000-8000-000000000001")
    kw = dict(verticals=["metal_stamping"], metros=["Houston, Texas"])

    await ensure_default_searches(db_session, org_id, **kw)
    await db_session.flush()

    row = (
        await db_session.execute(
            select(DiscoverySearch).where(DiscoverySearch.name == "metal_stamping:Houston")
        )
    ).scalar_one()
    row.is_active = False
    row.query = "operator's own wording"
    await db_session.flush()

    await ensure_default_searches(db_session, org_id, **kw)
    await db_session.flush()

    await db_session.refresh(row)
    assert row.is_active is False, "seeder reactivated a search the operator turned off"
    assert row.query == "operator's own wording", "seeder overwrote operator's edit"
