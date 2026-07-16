"""Merge the two Dialtone feature branches that forked from 063.

Features 3 and 4 were built concurrently, so each branched a head off
``063_merge_dialtone_features``:

* Feature 3 (A/B testing)          — ``064_ab_testing``.
* Feature 4 (calendar booking)     — ``064_booking_links``.

This no-op merge reunites them into a single head so ``alembic upgrade head``
applies both branches cleanly. It lands with Feature 3, the later of the two to
be committed (mirroring how ``063_merge_dialtone_features`` reunited Features
1/2/5).

Revision ID: 065_merge_ab_booking
Revises: 064_ab_testing, 064_booking_links
Create Date: 2026-07-16
"""

revision = "065_merge_ab_booking"
down_revision = ("064_ab_testing", "064_booking_links")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
