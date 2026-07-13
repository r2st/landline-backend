"""Add Phase 3.1 autonomous prospecting tables.

* icp_profiles       — learned Ideal Customer Profiles
* prospecting_runs   — one autonomous prospecting session
* prospect_candidates — companies surfaced by a run (incl. human review queue)

Revision ID: 049_add_prospecting
Revises: 048_merge_phase1_phase2
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "049_add_prospecting"
down_revision = "048_merge_phase1_phase2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "icp_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(120), nullable=False, server_default="Learned ICP"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("industry_weights", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("size_range", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("location_preferences", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("technology_signals", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("pain_point_indicators", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("exclusion_criteria", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("learned_from_deal_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("model_used", sa.String(50), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_icp_profiles_is_active", "icp_profiles", ["is_active"])

    op.create_table(
        "prospecting_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("icp_profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("icp_profiles.id", ondelete="SET NULL"), nullable=True),
        sa.Column("trigger", sa.String(20), nullable=False, server_default="scheduled"),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("target_volume", sa.Integer, nullable=False, server_default="25"),
        sa.Column("sources_queried", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("companies_found", sa.Integer, nullable=False, server_default="0"),
        sa.Column("companies_qualified", sa.Integer, nullable=False, server_default="0"),
        sa.Column("companies_enrolled", sa.Integer, nullable=False, server_default="0"),
        sa.Column("companies_review_queue", sa.Integer, nullable=False, server_default="0"),
        sa.Column("results", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_prospecting_runs_icp_profile_id", "prospecting_runs", ["icp_profile_id"])
    op.create_index("ix_prospecting_runs_status", "prospecting_runs", ["status"])

    op.create_table(
        "prospect_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("prospecting_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("company_name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), nullable=True),
        sa.Column("source", sa.String(50), nullable=False, server_default="unknown"),
        sa.Column("raw_data", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("fit_score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("qualification", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("reasoning", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="qualified"),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_prospect_candidates_run_id", "prospect_candidates", ["run_id"])
    op.create_index("ix_prospect_candidates_company_id", "prospect_candidates", ["company_id"])
    op.create_index("ix_prospect_candidates_status", "prospect_candidates", ["status"])


def downgrade() -> None:
    op.drop_table("prospect_candidates")
    op.drop_table("prospecting_runs")
    op.drop_table("icp_profiles")
