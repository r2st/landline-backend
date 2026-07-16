"""Feature 3 — A/B Testing: ab_test_variants, ab_test_assignments, ab_tests.

Revision ID: 064_ab_testing
Revises: 063_merge_dialtone_features
Create Date: 2026-07-16

Persistence + assignment tracking around the existing statistical engine in
``app/services/ab_testing_service.py``. Three tables:

* ``ab_test_variants``    — up to 4 copy variants per sequence step (A control, B-D
                            alternatives); NULL template inherits the step.
* ``ab_test_assignments`` — which variant an enrollment got at a step, written
                            before the send; unique on (enrollment_id, step_id).
* ``ab_tests``            — per-step test config + state (status, strategy, min
                            sample, confidence target, metric, declared winner).

Metrics are computed on demand from ``outreach_events``/``email_sends`` via the
assignment join — counts are not denormalised here (avoids drift).

Chaining: Features 1/2/5 branched off ``059`` and were reunited by
``063_merge_dialtone_features`` (the current single committed head). This
migration chains off that merge. Verified with ``alembic heads`` before writing.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "064_ab_testing"
down_revision = "063_merge_dialtone_features"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ab_test_variants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("sequence_step_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sequence_steps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("label", sa.String(1), nullable=False),
        sa.Column("subject_template", sa.Text, nullable=True),
        sa.Column("body_template", sa.Text, nullable=True),
        sa.Column("is_control", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("sequence_step_id", "label", name="uq_variant_step_label"),
    )
    op.create_index("ix_ab_test_variants_step", "ab_test_variants", ["sequence_step_id"])

    op.create_table(
        "ab_tests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("sequence_step_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sequence_steps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("strategy", sa.String(20), nullable=False, server_default="even"),
        sa.Column("min_sample", sa.Integer, nullable=False, server_default="200"),
        sa.Column("confidence_target", sa.Float, nullable=False, server_default="0.95"),
        sa.Column("metric", sa.String(20), nullable=False, server_default="replied"),
        sa.Column("winner_variant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ab_test_variants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("declared_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_ab_tests_step", "ab_tests", ["sequence_step_id"])

    op.create_table(
        "ab_test_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ab_test_variants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence_step_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sequence_steps.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enrollment_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("outreach_enrollments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("email_send_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("email_sends.id", ondelete="SET NULL"), nullable=True),
        sa.Column("assigned_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("enrollment_id", "sequence_step_id", name="uq_assignment_step"),
    )
    op.create_index("ix_ab_test_assignments_variant", "ab_test_assignments", ["variant_id"])
    op.create_index("ix_ab_test_assignments_step", "ab_test_assignments", ["sequence_step_id"])
    op.create_index("ix_ab_test_assignments_enrollment", "ab_test_assignments", ["enrollment_id"])
    op.create_index("ix_ab_test_assignments_send", "ab_test_assignments", ["email_send_id"])
    # Supports the per-variant metric roll-up (assignment → variant at a step).
    op.create_index(
        "ix_ab_test_assignments_step_variant",
        "ab_test_assignments",
        ["sequence_step_id", "variant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ab_test_assignments_step_variant", table_name="ab_test_assignments")
    op.drop_index("ix_ab_test_assignments_send", table_name="ab_test_assignments")
    op.drop_index("ix_ab_test_assignments_enrollment", table_name="ab_test_assignments")
    op.drop_index("ix_ab_test_assignments_step", table_name="ab_test_assignments")
    op.drop_index("ix_ab_test_assignments_variant", table_name="ab_test_assignments")
    op.drop_table("ab_test_assignments")
    op.drop_index("ix_ab_tests_step", table_name="ab_tests")
    op.drop_table("ab_tests")
    op.drop_index("ix_ab_test_variants_step", table_name="ab_test_variants")
    op.drop_table("ab_test_variants")
