"""Initial schema — all tables for Landline lead generation.

Revision ID: 001_initial
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Companies
    op.create_table(
        "companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), nullable=True),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("state_province", sa.String(100), nullable=True),
        sa.Column("country", sa.String(2), nullable=False, server_default="US"),
        sa.Column("employee_count", sa.Integer(), nullable=True),
        sa.Column("naics_code", sa.String(10), nullable=True),
        sa.Column("naics_description", sa.String(255), nullable=True),
        sa.Column("industry_segment", sa.String(100), nullable=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("stage", sa.String(20), nullable=False, server_default="new"),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default="{}"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("domain"),
        sa.CheckConstraint("country IN ('US', 'CA')", name="ck_companies_country"),
        sa.CheckConstraint(
            "stage IN ('new','researched','scored','contacted','engaged','qualified','opportunity','customer','disqualified')",
            name="ck_companies_stage",
        ),
    )
    op.create_index("ix_companies_stage", "companies", ["stage"])
    op.create_index("ix_companies_country", "companies", ["country"])
    op.create_index("ix_companies_naics", "companies", ["naics_code"])

    # Contacts
    op.create_table(
        "contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("title", sa.String(150), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default="false"),
        sa.Column("source", sa.String(50), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_contacts_company", "contacts", ["company_id"])
    op.create_index("ix_contacts_email", "contacts", ["email"])

    # Lead scores
    op.create_table(
        "lead_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score_type", sa.String(20), nullable=False),
        sa.Column("fit_score", sa.Integer(), nullable=False),
        sa.Column("industry_score", sa.Integer(), server_default="0"),
        sa.Column("size_score", sa.Integer(), server_default="0"),
        sa.Column("location_score", sa.Integer(), server_default="0"),
        sa.Column("engagement_score", sa.Integer(), server_default="0"),
        sa.Column("factors", postgresql.JSONB(), server_default="{}"),
        sa.Column("scored_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="CASCADE"),
        sa.CheckConstraint("score_type IN ('rule_based', 'ai')", name="ck_lead_scores_type"),
        sa.CheckConstraint("fit_score BETWEEN 0 AND 100", name="ck_lead_scores_fit"),
    )
    op.create_index("ix_lead_scores_company", "lead_scores", ["company_id"])

    # Consent records
    op.create_table(
        "consent_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consent_type", sa.String(20), nullable=False),
        sa.Column("basis", sa.Text(), nullable=True),
        sa.Column("evidence_url", sa.Text(), nullable=True),
        sa.Column("granted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.CheckConstraint("consent_type IN ('express', 'implied', 'none')", name="ck_consent_type"),
    )
    op.create_index("ix_consent_contact", "consent_records", ["contact_id"])

    # Outreach sequences
    op.create_table(
        "outreach_sequences",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # Sequence steps
    op.create_table(
        "sequence_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("sequence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("delay_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("subject_template", sa.Text(), nullable=False),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["sequence_id"], ["outreach_sequences.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("sequence_id", "step_number", name="uq_sequence_step"),
    )

    # Outreach enrollments
    op.create_table(
        "outreach_enrollments",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("current_step", sa.Integer(), server_default="1"),
        sa.Column("status", sa.String(20), server_default="active"),
        sa.Column("enrolled_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sequence_id"], ["outreach_sequences.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('active','paused','completed','replied','bounced','unsubscribed')",
            name="ck_enrollment_status",
        ),
    )
    op.create_index("ix_enrollment_status", "outreach_enrollments", ["status"])

    # Outreach events
    op.create_table(
        "outreach_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("occurred_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), server_default="{}"),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["enrollment_id"], ["outreach_enrollments.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "event_type IN ('sent','delivered','opened','clicked','replied','bounced','unsubscribed')",
            name="ck_event_type",
        ),
    )
    op.create_index("ix_events_enrollment", "outreach_events", ["enrollment_id"])

    # Interactions
    op.create_table(
        "interactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("interaction_type", sa.String(30), nullable=False),
        sa.Column("subject", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("occurred_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
    )


def downgrade() -> None:
    op.drop_table("interactions")
    op.drop_table("outreach_events")
    op.drop_table("outreach_enrollments")
    op.drop_table("sequence_steps")
    op.drop_table("outreach_sequences")
    op.drop_table("consent_records")
    op.drop_table("lead_scores")
    op.drop_table("contacts")
    op.drop_table("companies")
