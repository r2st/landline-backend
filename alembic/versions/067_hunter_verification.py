"""P3 — Hunter.io email verification.

Extends ``contacts`` rather than adding a parallel verification table:
``email_status`` / ``email_checked_at`` already live here, and a second home for
"can we email this person?" would inevitably drift out of sync with the first.

``email_status`` stays the SINGLE gate every send path reads
('valid' | 'invalid' | 'unknown'); the columns added here only explain *why* it
holds that value.

``email_verified_at`` is indexed because it drives both sweeps: the pending
sweep (``IS NULL``) and the staleness sweep (``< now - hunter_reverify_days``).

Chains off ``066_send_time_optimization`` — the single head once Features 1-6
were reconciled by ``063_merge_dialtone_features`` and ``065_merge_ab_booking``
(verified with ``alembic heads`` at author time).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "067_hunter_verification"
down_revision = "066_send_time_optimization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column("email_verification_source", sa.String(10), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("email_verification_result", sa.String(15), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("email_verification_score", sa.Integer, nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column(
            "email_verification_meta",
            postgresql.JSONB,
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "contacts",
        sa.Column("email_verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_contacts_email_verified_at", "contacts", ["email_verified_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_contacts_email_verified_at", table_name="contacts")
    op.drop_column("contacts", "email_verified_at")
    op.drop_column("contacts", "email_verification_meta")
    op.drop_column("contacts", "email_verification_score")
    op.drop_column("contacts", "email_verification_result")
    op.drop_column("contacts", "email_verification_source")
