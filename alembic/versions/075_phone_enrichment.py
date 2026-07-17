"""Phone enrichment for warm leads.

When a prospect shows repeated engagement (3+ distinct opens, or any click /
reply) Dialtone tries to enrich their contact with a phone number so the
operator can follow up personally. The contact already carries ``phone`` /
``mobile`` columns (populated opportunistically by card scans and Apollo people
search); this migration adds the two columns that make an *enrichment attempt*
auditable and idempotent:

* ``phone_source`` — where the number came from ('apollo' | 'company' | 'manual'
  | 'card'). Lets the UI badge an auto-found number and keeps a manual edit from
  being overwritten by a later sweep.
* ``phone_enriched_at`` — when the warm-lead sweep last *attempted* enrichment.
  Set whether or not a number was found, so the sweep never re-charges Apollo
  for the same contact twice. Indexed because the sweep selects ``IS NULL`` (the
  never-attempted set), mirroring the ``email_verified_at`` pattern.

Chains off ``074_honest_legacy_copy`` (the single head at author time).
"""

from alembic import op
import sqlalchemy as sa

revision = "075_phone_enrichment"
down_revision = "074_honest_legacy_copy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column("phone_source", sa.String(20), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("phone_enriched_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_contacts_phone_enriched_at", "contacts", ["phone_enriched_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_contacts_phone_enriched_at", table_name="contacts")
    op.drop_column("contacts", "phone_enriched_at")
    op.drop_column("contacts", "phone_source")
