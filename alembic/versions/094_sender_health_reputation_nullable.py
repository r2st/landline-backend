"""Sender-health reputation becomes nullable (email pipeline audit).

Chains off ``093_postmaster_metrics``.

Before this, ``sender_health.reputation_score`` was NOT NULL with a default of
100, so an account that had never sent an email was assessed as
"reputation 100 / healthy" — a fabricated perfect score for a sender we had
literally no delivery data on. The optimizer now reports such accounts as
``status="pending"`` with a NULL reputation (honestly "not rated yet"), which
requires the column — on both the live table and the append-only snapshot — to
allow NULL.

Data-only, reversible: existing non-NULL scores are untouched. The downgrade
backfills any NULLs to 100 before restoring the NOT NULL constraint so it can't
fail on pending rows.
"""

from alembic import op
import sqlalchemy as sa

revision = "094_sender_health_reputation_nullable"
down_revision = "093_postmaster_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "sender_health",
        "reputation_score",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.alter_column(
        "sender_health_snapshot",
        "reputation_score",
        existing_type=sa.Integer(),
        nullable=True,
    )


def downgrade() -> None:
    # Backfill pending rows so the NOT NULL restore can't fail.
    op.execute(
        "UPDATE sender_health SET reputation_score = 100 "
        "WHERE reputation_score IS NULL"
    )
    op.execute(
        "UPDATE sender_health_snapshot SET reputation_score = 100 "
        "WHERE reputation_score IS NULL"
    )
    op.alter_column(
        "sender_health_snapshot",
        "reputation_score",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        "sender_health",
        "reputation_score",
        existing_type=sa.Integer(),
        nullable=False,
    )
