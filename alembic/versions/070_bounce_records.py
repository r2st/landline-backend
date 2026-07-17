"""Bounce ingestion ledger.

Chains off ``069_places_discovery``, the current head.

One table, no alterations. ``bounce_records`` is the dedup ledger that makes the
scheduled bounce sweep safe to repeat. Gmail is the primary transport and offers
no bounce webhook, so delivery-failure notifications have to be *polled* out of
the mailbox on a rolling lookback window — which means the same DSN is seen on
every pass until it ages out.

That matters because ``bounce_handler.record_bounce`` is not idempotent: it
increments ``contacts.bounce_count`` and suppresses the contact once the
soft-bounce threshold is crossed. Without a record of what has already been
handled, a 15-minute sweep over a 3-day window would re-count one transient
bounce ~288 times and permanently suppress a live contact purely as an artifact
of polling frequency.

``gmail_message_id`` is therefore UNIQUE — that constraint is the actual
safety mechanism, not just an index. The remaining columns exist to audit a
classification after the fact (why was this called hard?) and to see what the
handler did about it.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "070_bounce_records"
down_revision = "069_places_discovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bounce_records",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # The dedup key. UNIQUE is load-bearing: it is what stops a repeated
        # sweep from re-counting the same bounce.
        sa.Column("gmail_message_id", sa.String(255), nullable=False),
        sa.Column(
            "gmail_account_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gmail_accounts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("recipient_email", sa.String(320), nullable=False),
        sa.Column("bounce_kind", sa.String(10), nullable=False),
        sa.Column("status_code", sa.String(20), nullable=True),
        sa.Column("diagnostic", sa.Text(), nullable=True),
        sa.Column(
            "result", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "processed_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("bounce_kind IN ('hard','soft')", name="ck_bounce_kind"),
    )
    op.create_index(
        "ix_bounce_records_gmail_message_id", "bounce_records",
        ["gmail_message_id"], unique=True,
    )
    op.create_index(
        "ix_bounce_records_recipient_email", "bounce_records", ["recipient_email"],
    )


def downgrade() -> None:
    op.drop_index("ix_bounce_records_recipient_email", table_name="bounce_records")
    op.drop_index("ix_bounce_records_gmail_message_id", table_name="bounce_records")
    op.drop_table("bounce_records")
