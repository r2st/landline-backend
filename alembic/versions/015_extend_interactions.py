"""Extend interactions with content, follow_up_date, created_at

Adds the client-management fields to the existing interactions table:
- content: free-form body (meeting notes, call summary, email text)
- follow_up_date: optional reminder date
- created_at: row creation timestamp (back-fills existing rows via server default)

No check constraint is added on interaction_type: internal writers (reply
tracker, email logger) use values beyond the meeting/call/email/note set, which
the /interactions create endpoint validates at the API layer instead.

This revision also used to create ``ix_interactions_company_id`` and
``ix_interactions_occurred_at``. Both are already created by ``005_add_indexes``,
ten revisions upstream, so a fresh ``alembic upgrade head`` died here with
"relation already exists" — the migration chain could not build an environment
from nothing. Prod was fine (it applies incrementally and the indexes were
there), which is exactly what made it invisible: the break only showed up in the
case you need it in, a disaster recovery or a new deployment.

Removed rather than made conditional: 005 owns those indexes, and two revisions
claiming the same object is the bug. Nothing changes for a database that has
already run this revision — applied migrations are not re-run — so this is a
fresh-database fix only.

Revision ID: 015_extend_interactions
Revises: 014_add_tasks
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = "015_extend_interactions"
down_revision = "014_add_tasks"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("interactions", sa.Column("content", sa.Text, nullable=True))
    op.add_column("interactions", sa.Column("follow_up_date", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(
        "interactions",
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Indexes deliberately not created here — see the module docstring. They
    # belong to 005_add_indexes.


def downgrade():
    op.drop_column("interactions", "created_at")
    op.drop_column("interactions", "follow_up_date")
    op.drop_column("interactions", "content")
