"""AI-composed first-touch drafts + a pending_review enrollment state.

Chains off ``070_bounce_records``.

Two changes:

1. ``ai_email_drafts`` — the human review queue for AI-composed cold copy. The
   first N AI emails per sequence are parked here instead of being sent. It
   keeps both the proposed copy and the static copy it would replace, because a
   reviewer's actual question is "is this better than what we already had?",
   which they cannot answer without the comparison.

2. ``outreach_enrollments.status`` gains ``pending_review``. The existing CHECK
   allowed only active/paused/completed/replied/bounced/unsubscribed, and the
   parked state genuinely is none of those. Reusing ``paused`` was the
   alternative and is rejected on purpose: paused already means three unrelated
   things (no contact email, undeliverable domain, consent block), and the
   outreach queue only dispatches ``active`` rows — so an enrollment waiting on
   a reviewer must be distinguishable from one halted for compliance, or the
   queue cannot tell "a human needs to look at this" from "never send this".

The check constraint is dropped and recreated rather than altered: Postgres has
no ALTER CONSTRAINT for a CHECK expression.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "071_ai_email_drafts"
down_revision = "070_bounce_records"
branch_labels = None
depends_on = None

_OLD_STATUSES = "'active','paused','completed','replied','bounced','unsubscribed'"
_NEW_STATUSES = _OLD_STATUSES + ",'pending_review'"


def upgrade() -> None:
    op.create_table(
        "ai_email_drafts",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "enrollment_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("outreach_enrollments.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "sequence_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("outreach_sequences.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "contact_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "company_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("static_subject", sa.Text(), nullable=True),
        sa.Column("static_body", sa.Text(), nullable=True),
        sa.Column(
            "ai_generated", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "research_used", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="pending"
        ),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column(
            "reviewed_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "send_result", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','sent','failed')",
            name="ck_ai_draft_status",
        ),
    )
    op.create_index("ix_ai_email_drafts_enrollment_id", "ai_email_drafts", ["enrollment_id"])
    op.create_index("ix_ai_email_drafts_status", "ai_email_drafts", ["status"])

    op.drop_constraint("ck_enrollment_status", "outreach_enrollments", type_="check")
    op.create_check_constraint(
        "ck_enrollment_status", "outreach_enrollments", f"status IN ({_NEW_STATUSES})"
    )


def downgrade() -> None:
    # Park anything mid-review back to paused so the narrower CHECK can apply.
    op.execute(
        "UPDATE outreach_enrollments SET status = 'paused' WHERE status = 'pending_review'"
    )
    op.drop_constraint("ck_enrollment_status", "outreach_enrollments", type_="check")
    op.create_check_constraint(
        "ck_enrollment_status", "outreach_enrollments", f"status IN ({_OLD_STATUSES})"
    )
    op.drop_index("ix_ai_email_drafts_status", table_name="ai_email_drafts")
    op.drop_index("ix_ai_email_drafts_enrollment_id", table_name="ai_email_drafts")
    op.drop_table("ai_email_drafts")
