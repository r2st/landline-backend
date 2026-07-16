"""Feature 4 — Calendar Booking Injection: per-sender booking config.

Adds the booking-link configuration to each sending identity so Dialtone can
inject a rep's scheduling URL into follow-ups once a prospect shows interest:

* ``booking_url``          — the scheduler link (Calendly/Cal.com/etc.).
* ``booking_provider``     — display-only provider label.
* ``booking_auto_inject``  — whether AI follow-ups auto-weave the link on interest.

Applied to both ``gmail_accounts`` and ``smtp_accounts`` (each rep books to their
own calendar regardless of transport).

Revision ID: 064_booking_links
Revises: 063_merge_dialtone_features
Create Date: 2026-07-16

Chaining note: this anchors to ``063_merge_dialtone_features`` — the merge that
reunited the parallel Feature 1/2/5 branches into a single head — which is the
committed head at authoring time (verified via ``alembic heads``).
"""

from alembic import op
import sqlalchemy as sa

revision = "064_booking_links"
down_revision = "063_merge_dialtone_features"
branch_labels = None
depends_on = None

_TABLES = ("gmail_accounts", "smtp_accounts")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("booking_url", sa.String(500), nullable=True))
        op.add_column(table, sa.Column("booking_provider", sa.String(40), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "booking_auto_inject",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "booking_auto_inject")
        op.drop_column(table, "booking_provider")
        op.drop_column(table, "booking_url")
