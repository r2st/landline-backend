"""Link every won deal back to the outreach that produced it.

Chains off ``074_honest_legacy_copy``.

Dialtone has always been able to say how many emails it sent and how many
replies came back, but not the question that actually justifies the spend:
*which sequence produced revenue?* A deal knew its company; it did not know the
enrollment that first put that company in front of us. So "Final v6 booked
$40k of pipeline last month" was unanswerable — the thread from a won deal back
to the sequence that started the conversation simply wasn't stored.

This adds two nullable columns to ``deals``:

* ``source_enrollment_id`` — the enrollment that brought the company in. FK to
  ``outreach_enrollments`` with ``ON DELETE SET NULL``: the deal (and its
  revenue) must outlive enrollment pruning; it only loses the finer-grained
  link.
* ``source_sequence_id`` — the sequence credited with the deal, denormalized
  alongside the enrollment. FK to ``outreach_sequences``, also ``SET NULL``.
  Denormalized on purpose: attribution rollups group by sequence, and pinning
  the sequence id on the row keeps that credit stable even if the enrollment is
  later deleted, and keeps the rollup from having to join through enrollments.

Both are nullable because most historical deals predate attribution and manual
deals may have no sequence at all — a NULL simply means "unattributed", which
the rollup buckets separately rather than mis-crediting. Indexed because the
attribution endpoint filters and groups on ``source_sequence_id``.

Backfill is best-effort and non-destructive: for each deal with no attribution,
credit the earliest enrollment among the company's contacts. Deals whose
company was never enrolled stay NULL.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "075_deal_sequence_attribution"
down_revision = "074_honest_legacy_copy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deals",
        sa.Column("source_enrollment_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "deals",
        sa.Column("source_sequence_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_deals_source_enrollment",
        "deals",
        "outreach_enrollments",
        ["source_enrollment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_deals_source_sequence",
        "deals",
        "outreach_sequences",
        ["source_sequence_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_deals_source_enrollment_id", "deals", ["source_enrollment_id"]
    )
    op.create_index(
        "ix_deals_source_sequence_id", "deals", ["source_sequence_id"]
    )

    # Best-effort backfill: credit each unattributed deal to the earliest
    # enrollment among its company's contacts. LATERAL keeps it to one enrollment
    # per deal (the first the company was ever put into), which is the one that
    # "brought them in". Deals whose company was never enrolled stay NULL.
    op.execute(
        """
        UPDATE deals d
        SET source_enrollment_id = e.id,
            source_sequence_id   = e.sequence_id
        FROM LATERAL (
            SELECT en.id, en.sequence_id
            FROM outreach_enrollments en
            JOIN contacts c ON c.id = en.contact_id
            WHERE c.company_id = d.company_id
            ORDER BY en.enrolled_at ASC
            LIMIT 1
        ) e
        WHERE d.source_enrollment_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_deals_source_sequence_id", table_name="deals")
    op.drop_index("ix_deals_source_enrollment_id", table_name="deals")
    op.drop_constraint("fk_deals_source_sequence", "deals", type_="foreignkey")
    op.drop_constraint("fk_deals_source_enrollment", "deals", type_="foreignkey")
    op.drop_column("deals", "source_sequence_id")
    op.drop_column("deals", "source_enrollment_id")
