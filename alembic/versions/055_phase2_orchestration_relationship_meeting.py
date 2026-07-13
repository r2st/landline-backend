"""Phase 2 tables: orchestration, relationship intelligence, meeting briefs.

Covers features:
* 2.2 Autonomous Multi-Channel Orchestration → orchestration_policies +
      outreach_enrollments.orchestration_mode / orchestration_log
* 2.3 Relationship Intelligence & Org Chart  → stakeholder_maps, org_relationships
* 2.5 Smart Meeting Scheduler & Briefings     → meeting_briefs

Revision ID: 055_phase2_orchestration_relationship_meeting
Revises: 054_merge_phase3_phase4
Create Date: 2026-07-13

Re-chained onto the Phase 3+4 merge head (054) at merge time so the migration
history stays linear; the Phase 3 and Phase 4 work landed on main first.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "055_phase2_orchestration_relationship_meeting"
down_revision = "054_merge_phase3_phase4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- 2.2 Extend outreach_enrollments ---
    op.add_column(
        "outreach_enrollments",
        sa.Column("orchestration_mode", sa.String(20), nullable=False, server_default="manual"),
    )
    op.add_column(
        "outreach_enrollments",
        sa.Column("orchestration_log", postgresql.JSONB, nullable=False, server_default="[]"),
    )

    # --- 2.2 Orchestration policies ---
    op.create_table(
        "orchestration_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(120), nullable=False, server_default="Default policy"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("max_daily_touches", sa.Integer, nullable=False, server_default="2"),
        sa.Column("max_total_touches", sa.Integer, nullable=False, server_default="12"),
        sa.Column("min_hours_between_touches", sa.Integer, nullable=False, server_default="24"),
        sa.Column("allowed_channels", postgresql.JSONB, nullable=False, server_default='["email", "linkedin", "phone"]'),
        sa.Column("required_channels", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("human_review_triggers", postgresql.JSONB, nullable=False, server_default='["pricing_request", "complex_objection", "referral", "legal"]'),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_orchestration_policies_user_id", "orchestration_policies", ["user_id"])

    # --- 2.3 Stakeholder maps ---
    op.create_table(
        "stakeholder_maps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("inferred_role", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("relationship_strength", sa.String(10), nullable=False, server_default="cold"),
        sa.Column("engagement_score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("engagement_trend", sa.String(12), nullable=False, server_default="unknown"),
        sa.Column("last_interaction", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rationale", sa.Text, nullable=True),
        sa.Column("ai_analysis", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("company_id", "contact_id", name="uq_stakeholder_company_contact"),
    )
    op.create_index("ix_stakeholder_maps_company_id", "stakeholder_maps", ["company_id"])
    op.create_index("ix_stakeholder_maps_contact_id", "stakeholder_maps", ["contact_id"])

    # --- 2.3 Org relationships ---
    op.create_table(
        "org_relationships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("related_contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relationship_type", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Integer, nullable=False, server_default="50"),
        sa.Column("source", sa.String(30), nullable=False, server_default="title_inference"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("contact_id", "related_contact_id", "relationship_type", name="uq_org_edge"),
    )
    op.create_index("ix_org_relationships_company_id", "org_relationships", ["company_id"])
    op.create_index("ix_org_relationships_contact_id", "org_relationships", ["contact_id"])

    # --- 2.5 Meeting briefs ---
    op.create_table(
        "meeting_briefs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("contact_ids", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("title", sa.String(255), nullable=False, server_default="Meeting briefing"),
        sa.Column("meeting_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("briefing_content", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ready"),
        sa.Column("calendar_event_id", sa.String(255), nullable=True),
        sa.Column("post_meeting_analysis", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("model_used", sa.String(50), nullable=True),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_meeting_briefs_company_id", "meeting_briefs", ["company_id"])
    op.create_index("ix_meeting_briefs_user_id", "meeting_briefs", ["user_id"])


def downgrade() -> None:
    op.drop_table("meeting_briefs")
    op.drop_table("org_relationships")
    op.drop_table("stakeholder_maps")
    op.drop_index("ix_orchestration_policies_user_id", table_name="orchestration_policies")
    op.drop_table("orchestration_policies")
    op.drop_column("outreach_enrollments", "orchestration_log")
    op.drop_column("outreach_enrollments", "orchestration_mode")
