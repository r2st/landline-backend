"""Feature 6 — Smart Send-Time Optimization.

Adds the per-contact engagement profile (resolved timezone + learned local
(dow, hour) engagement buckets) and two scheduling fields on outreach
enrollments:

* ``scheduled_send_at`` — the smart-timing target for the currently-due step.
* ``timing_arm``        — 'optimized' | 'control' (A/B split to prove the lift).

Chains off the single head after Features 1-5 landed and were reconciled by
``065_merge_ab_booking`` (verified via ``alembic heads`` at author time).
``contacts.timezone`` already exists and remains the cached authoritative tz.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "066_send_time_optimization"
down_revision = "065_merge_ab_booking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contact_engagement_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tz", sa.String(64), nullable=True),
        sa.Column("tz_source", sa.String(20), nullable=True),
        sa.Column("buckets", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("opens_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("best_dow", sa.Integer, nullable=True),
        sa.Column("best_hour", sa.Integer, nullable=True),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("last_computed", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("contact_id", name="uq_engagement_profile_contact"),
    )
    op.create_index(
        "ix_contact_engagement_profiles_contact_id",
        "contact_engagement_profiles",
        ["contact_id"],
    )

    op.add_column(
        "outreach_enrollments",
        sa.Column("scheduled_send_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "outreach_enrollments",
        sa.Column("timing_arm", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outreach_enrollments", "timing_arm")
    op.drop_column("outreach_enrollments", "scheduled_send_at")
    op.drop_index(
        "ix_contact_engagement_profiles_contact_id",
        table_name="contact_engagement_profiles",
    )
    op.drop_table("contact_engagement_profiles")
