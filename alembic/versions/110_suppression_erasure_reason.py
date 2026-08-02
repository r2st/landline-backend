"""Let the suppression list say "erased", because that entry must never be pruned.

``SUPPRESSION_REASONS`` had four values — unsubscribe, bounce, complaint, manual
— and a CHECK constraint pinning them. None of them fits the reason this
migration exists for.

``gdpr.erase_subject`` honours a right-to-erasure request by scrubbing the
subject's PII in place: it sets ``suppressed`` and ``unsubscribed`` on the
contact, and then overwrites ``contact.email`` with
``anonymized+<id>@redacted.invalid``. The two flags survive. The address they
were about does not. Nothing else in the system maps that person to "never
contact", so the moment the scraper rediscovers them — from the public source it
found them on the first time — a fresh contact row appears with
``suppressed=False`` and the sequence engine mails someone who exercised their
right to be forgotten. Erasure does not merely fail to be durable, the way a
flag on a re-scraped row does; it destroys the only surviving link between the
address and the decision.

The suppression list is the fix, because it is keyed by address and outlives
every contact row. Retaining the minimum needed to *honour* an erasure or an
objection is the recognised carve-out — suppressing someone requires knowing who
not to mail — so the entry is written before the scrub, while the address is
still readable.

It gets its own reason rather than borrowing ``manual``. An operator pruning the
suppression list can re-verify a ``bounce`` and may argue about a ``manual``;
``erasure`` is the one entry whose removal re-opens a legal exposure, and it
should say so on its face.

Widening a CHECK constraint is backwards-compatible in the direction that
matters: every existing row still satisfies the new one.

Revision ID: 110_suppression_erasure_reason
Revises: 109_enrollment_send_claim
"""

from alembic import op

revision = "110_suppression_erasure_reason"
down_revision = "109_enrollment_send_claim"
branch_labels = None
depends_on = None

_OLD = "reason IN ('unsubscribe','bounce','complaint','manual')"
_NEW = "reason IN ('unsubscribe','bounce','complaint','manual','erasure')"


def upgrade() -> None:
    op.drop_constraint("ck_suppression_reason", "suppression_entries", type_="check")
    op.create_check_constraint("ck_suppression_reason", "suppression_entries", _NEW)


def downgrade() -> None:
    # Rows written by the new code would violate the old constraint. Fold them
    # into ``manual`` rather than failing the downgrade or, worse, deleting the
    # one class of entry that must not be lost.
    op.execute(
        "UPDATE suppression_entries SET reason = 'manual' WHERE reason = 'erasure'"
    )
    op.drop_constraint("ck_suppression_reason", "suppression_entries", type_="check")
    op.create_check_constraint("ck_suppression_reason", "suppression_entries", _OLD)
