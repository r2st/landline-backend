"""The default Places search grid — the thing that actually feeds the pipeline.

Places has no "give me every US manufacturer" query. Coverage is the product of
*many narrow* searches (a category x metro grid), each capped at 60 results.
:class:`~app.models.discovery_search.DiscoverySearch` is one cell of that grid;
this module is the grid itself.

**Why this file exists.** Audited 2026-07-24: ``discovery_searches`` was empty in
production. The weekly sweep ran on schedule and found nothing to do, the daily
email-enrichment sweep drained its backlog and went idle, and contact sourcing
stopped dead on 2026-07-16 — 39 contacts total, of which 30 were already
enrolled. Every downstream fix (better copy, better sequences, better reply
handling) is rate-limited by that number. An empty grid is not a configuration
choice anyone made; it is what you get when discovery is only ever run by hand
from the API and nobody saves the search.

**Seeded once, then owned by the operator.** ``ensure_default_searches`` inserts
only names that do not already exist and never reactivates or edits a row it
finds. Deactivating a cell is a decision — a metro that returns nothing but
noise, a vertical that has been paused — and a seeder that "repaired" that on
the next beat would overrule the operator every week. This is why the grid is
seeded by a migration and by an explicitly-triggered task, and is *not* wired
into the weekly sweep.

**Cost.** ``google_places_daily_budget`` (100/day) and the monthly cap are
enforced inside ``places.discover()``, so this grid cannot outspend them however
large it gets; an over-budget cell returns ``budget_exhausted`` and the next
sweep picks it up (the sweep orders oldest-run-first, so a capped run always
advances the most stale cell rather than re-running the same ones). At weekly
cadence the grid below is roughly 48 searches/week against a 2000/month cap.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.discovery_search import DiscoverySearch

logger = logging.getLogger(__name__)

# The Places textQuery per vertical, keyed by ``IndustryVertical.key`` so a cell
# is traceable to the sequence its contacts get enrolled in.
#
# These are the *trade* terms, not our internal labels: Places matches business
# names and categories, so "CNC machine shop" finds shops that "precision
# manufacturing services" does not. One query per vertical — a second phrasing
# mostly re-finds the same businesses (place_id dedups them) at full price.
VERTICAL_QUERIES: dict[str, str] = {
    "injection_molding": "plastic injection molding manufacturer",
    "cnc_machining": "CNC machine shop",
    "metal_stamping": "metal stamping and sheet metal fabrication",
    "food_beverage": "food and beverage manufacturing company",
}

# US metros chosen for manufacturing density, weighted toward the ones these
# verticals actually cluster in (Grand Rapids and Detroit for molding and
# stamping; Cleveland and Milwaukee for machining). Kept to twelve because the
# grid is a product: every metro added costs one search per vertical per week.
METROS: tuple[str, ...] = (
    "Chicago, Illinois",
    "Detroit, Michigan",
    "Grand Rapids, Michigan",
    "Cleveland, Ohio",
    "Los Angeles, California",
    "Houston, Texas",
    "Dallas, Texas",
    "Minneapolis, Minnesota",
    "Milwaukee, Wisconsin",
    "Charlotte, North Carolina",
    "Atlanta, Georgia",
    "Philadelphia, Pennsylvania",
)

# Weekly, matching the sweep's own cadence rationale: local business data
# changes slowly, so daily would pay seven times over for the same rows.
DEFAULT_SCHEDULE = "weekly"
DEFAULT_COUNTRY = "US"


def _search_name(vertical_key: str, metro: str) -> str:
    """Stable identity for a cell. This is the idempotency key.

    Name rather than a (query, location) pair because it is also what an
    operator sees in the UI and switches off; keying dedup on anything they
    can't see would make "why did this come back" unanswerable.
    """
    city = metro.split(",")[0].strip()
    return f"{vertical_key}:{city}"


def default_grid(
    *,
    verticals: Optional[Iterable[str]] = None,
    metros: Optional[Iterable[str]] = None,
) -> list[dict]:
    """The full category x metro grid, as insertable dicts."""
    keys = list(verticals) if verticals is not None else list(VERTICAL_QUERIES)
    places = list(metros) if metros is not None else list(METROS)
    grid: list[dict] = []
    for key in keys:
        query = VERTICAL_QUERIES.get(key)
        if not query:
            # An unknown vertical is a caller bug, but a partial grid beats no
            # grid — sourcing is the bottleneck this module exists to relieve.
            logger.warning("No Places query for vertical %r — skipping", key)
            continue
        for metro in places:
            grid.append(
                {
                    "name": _search_name(key, metro),
                    "query": query,
                    "location_text": metro,
                    "country": DEFAULT_COUNTRY,
                    "schedule": DEFAULT_SCHEDULE,
                    "is_active": True,
                    "meta": {"seeded": True, "vertical": key},
                }
            )
    return grid


async def ensure_default_searches(
    db: AsyncSession,
    org_id: UUID,
    *,
    user_id: Optional[UUID] = None,
    verticals: Optional[Iterable[str]] = None,
    metros: Optional[Iterable[str]] = None,
) -> dict:
    """Insert any missing grid cell for *org_id*. Idempotent; commits nothing.

    Existing rows are left exactly as found — including deactivated ones, which
    are an operator decision and not damage to repair. Returns a summary; the
    caller owns the transaction.
    """
    grid = default_grid(verticals=verticals, metros=metros)
    wanted = [cell["name"] for cell in grid]

    existing = set(
        (
            await db.execute(
                select(DiscoverySearch.name).where(
                    DiscoverySearch.org_id == org_id,
                    DiscoverySearch.name.in_(wanted),
                )
            )
        )
        .scalars()
        .all()
    )

    created = 0
    for cell in grid:
        if cell["name"] in existing:
            continue
        db.add(DiscoverySearch(org_id=org_id, user_id=user_id, **cell))
        created += 1

    summary = {
        "created": created,
        "existing": len(existing),
        "total": len(grid),
    }
    logger.info("Discovery grid for org %s: %s", org_id, summary)
    return summary
