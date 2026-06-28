"""Add industry tag to sequences and seed per-industry outreach verticals.

Adds ``outreach_sequences.industry`` and idempotently seeds one sequence (+ steps)
per industry vertical (plastic injection molding, CNC machining, metal stamping &
forming, food & beverage). The molding sequence created in migration 025 is
backfilled with its industry tag rather than duplicated.

Revision ID: 026_industry_verticals
Revises: 025_seed_molding_sequence
Create Date: 2026-06-28
"""
import sqlalchemy as sa
from alembic import op

from app.services.industry_verticals import seed_all_verticals

revision = "026_industry_verticals"
down_revision = "025_seed_molding_sequence"
branch_labels = None
depends_on = None


def _has_column(conn, table, column) -> bool:
    insp = sa.inspect(conn)
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade():
    conn = op.get_bind()
    # Idempotent column add (migration may be retried; keep it safe).
    if not _has_column(conn, "outreach_sequences", "industry"):
        op.add_column(
            "outreach_sequences",
            sa.Column("industry", sa.String(80), nullable=True),
        )
    # Seed/realign every vertical from the registry (idempotent by name).
    seed_all_verticals(conn)


def downgrade():
    conn = op.get_bind()
    # Remove the verticals seeded here (molding stays — it predates this revision).
    for name in (
        "CNC Machining — Deal Cracker",
        "Metal Stamping & Forming — Deal Cracker",
        "Food & Beverage — Deal Cracker",
    ):
        conn.execute(
            sa.text("DELETE FROM outreach_sequences WHERE name = :name"),
            {"name": name},
        )
    if _has_column(conn, "outreach_sequences", "industry"):
        op.drop_column("outreach_sequences", "industry")
