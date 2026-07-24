"""Keep the person Hunter names, not just the address.

Hunter's ``/domain-search`` returns first_name/last_name/position alongside each
address, ranked personal-first. ``places_discovery.extract_email`` read only the
address and dropped the rest, so every Places-sourced contact arrived nameless:
29 of 39 production contacts had no first name and every cold email opened
"Hi there," (audited 2026-07-24).

Nullable with no backfill — the names were never stored, so there is nothing to
recover for rows already enriched. They fill in as the daily enrichment sweep
reaches new businesses.

Revision ID: 100_discovered_business_contact_name
Revises: 099_seed_discovery_grid
"""

import sqlalchemy as sa
from alembic import op

revision = "100_discovered_business_contact_name"
down_revision = "099_seed_discovery_grid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "discovered_businesses",
        sa.Column("contact_first_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "discovered_businesses",
        sa.Column("contact_last_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "discovered_businesses",
        sa.Column("contact_title", sa.String(length=150), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("discovered_businesses", "contact_title")
    op.drop_column("discovered_businesses", "contact_last_name")
    op.drop_column("discovered_businesses", "contact_first_name")
