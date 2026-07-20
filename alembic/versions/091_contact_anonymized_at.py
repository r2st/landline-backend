"""GDPR right-to-erasure: mark a contact anonymized (soft-delete).

Chains off ``090_smtp_imap_settings``, the current head.

Adds ``anonymized_at`` to ``contacts``. Erasure scrubs the PII columns in place
and stamps this timestamp, so the row survives (referential integrity for
aggregate stats / audit) while carrying no personal data and being excluded from
any surface that would re-expose or re-contact the person.
"""

from alembic import op
import sqlalchemy as sa

revision = "091_contact_anonymized_at"
down_revision = "090_smtp_imap_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column("anonymized_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("contacts", "anonymized_at")
