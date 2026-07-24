"""Seed the default Places discovery grid.

``discovery_searches`` was empty in production (audited 2026-07-24), so the
weekly sourcing sweep had nothing to run and contact acquisition had been dead
since 2026-07-16. See ``app.services.discovery_seed`` for the grid and the
reasoning behind its shape.

Insert-only and name-keyed: a cell whose name already exists is left untouched,
so re-running this is a no-op and an operator's own searches are never edited.
The downgrade removes only the rows this migration seeded (``meta->>'seeded'``),
never an operator's.

Revision ID: 099_seed_discovery_grid
Revises: 098_inbox_labels
"""

import json
import uuid

import sqlalchemy as sa
from alembic import op

revision = "099_seed_discovery_grid"
down_revision = "098_inbox_labels"
branch_labels = None
depends_on = None


# Inlined rather than imported from app.services.discovery_seed: a migration
# must keep reproducing the same result after the service's grid is edited, and
# importing application code into a migration couples schema history to a module
# that is free to change.
VERTICAL_QUERIES = {
    "injection_molding": "plastic injection molding manufacturer",
    "cnc_machining": "CNC machine shop",
    "metal_stamping": "metal stamping and sheet metal fabrication",
    "food_beverage": "food and beverage manufacturing company",
}

METROS = (
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


def upgrade() -> None:
    conn = op.get_bind()

    # Seed for every existing org. Places searches are org-scoped, and a grid
    # bound to a hardcoded org id would silently do nothing on any other
    # deployment of this schema.
    org_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM organizations"))]
    if not org_ids:
        return

    existing = {
        (row[0], row[1])
        for row in conn.execute(sa.text("SELECT org_id, name FROM discovery_searches"))
    }

    rows = []
    for org_id in org_ids:
        for key, query in VERTICAL_QUERIES.items():
            for metro in METROS:
                name = f"{key}:{metro.split(',')[0].strip()}"
                if (org_id, name) in existing:
                    continue
                rows.append(
                    {
                        "id": str(uuid.uuid4()),
                        "org_id": str(org_id),
                        "name": name,
                        "query": query,
                        "location_text": metro,
                        "country": "US",
                        "schedule": "weekly",
                        "is_active": True,
                        "meta": json.dumps({"seeded": True, "vertical": key}),
                    }
                )

    if not rows:
        return

    conn.execute(
        sa.text(
            """
            INSERT INTO discovery_searches
                (id, org_id, name, query, location_text, country, schedule,
                 is_active, meta, results_count, credits_spent)
            VALUES
                (:id, :org_id, :name, :query, :location_text, :country, :schedule,
                 :is_active, CAST(:meta AS jsonb), 0, 0)
            """
        ),
        rows,
    )


def downgrade() -> None:
    # Only the seeded rows. An operator's own searches are not this migration's
    # to delete, and by the time anyone downgrades there may be many.
    op.get_bind().execute(
        sa.text("DELETE FROM discovery_searches WHERE meta->>'seeded' = 'true'")
    )
