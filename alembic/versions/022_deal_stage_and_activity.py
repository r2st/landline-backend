"""Add deal_stage column, deal_activities and deal_notes tables

Revision ID: 022_deal_stage_and_activity
Revises: 021_indexes_and_deal_lost_reason
Create Date: 2026-06-23
"""
from alembic import op

revision = "022_deal_stage_and_activity"
down_revision = "021_indexes_and_deal_lost_reason"
branch_labels = None
depends_on = None


def upgrade():
    # Add deal_stage column to deals
    op.execute("""
        ALTER TABLE deals
        ADD COLUMN deal_stage VARCHAR(20) NOT NULL DEFAULT 'qualification'
    """)
    op.execute("""
        ALTER TABLE deals
        ADD CONSTRAINT ck_deals_deal_stage
        CHECK (deal_stage IN ('qualification','discovery','proposal','negotiation','closing'))
    """)

    # Create deal_activities table
    op.execute("""
        CREATE TABLE deal_activities (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            deal_id     UUID NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
            activity_type VARCHAR(50) NOT NULL,
            description TEXT,
            old_value   VARCHAR(255),
            new_value   VARCHAR(255),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # Create deal_notes table
    op.execute("""
        CREATE TABLE deal_notes (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            deal_id     UUID NOT NULL REFERENCES deals(id) ON DELETE CASCADE,
            content     TEXT NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS deal_notes")
    op.execute("DROP TABLE IF EXISTS deal_activities")
    op.execute("ALTER TABLE deals DROP CONSTRAINT IF EXISTS ck_deals_deal_stage")
    op.execute("ALTER TABLE deals DROP COLUMN IF EXISTS deal_stage")
