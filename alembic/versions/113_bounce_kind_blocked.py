"""Let a bounce record say "blocked", because a policy rejection isn't a bad address.

``ck_bounce_kind`` pinned ``bounce_records.bounce_kind`` to ``hard`` or
``soft``, and ``bounce_handler.classify_bounce`` had only those two answers to
give. So ``550 5.7.1 Our system has determined this message is likely
unsolicited mail`` — a 5, therefore permanent, therefore hard — got the full
dead-address treatment: the contact suppressed and unsubscribed, their live
enrollments closed as ``bounced``, and a ``reason='bounce'`` entry written to
the org do-not-contact list.

Nothing in that rejection was about the recipient. Their mailbox is alive and
being read; what was refused was our DMARC alignment, our sending IP, or a
content verdict. Which makes the blast radius the whole audience behind that
filter, not one address: a broken DMARC record rejects every message in the
send, so a single afternoon suppresses everyone we mailed at every Microsoft
365 and Google Workspace tenant — most of a B2B list. Past
``dead_domain_bounce_threshold`` distinct addresses the domain write-off then
suppresses each company's *remaining* contacts as well, none of whom were
mentioned by any rejection.

And it does not wash out. Fixing the DNS record restores sending and restores
nothing else: ``bounce`` is not a reversible suppression reason, so every entry
has to be lifted by hand, by an operator who first has to work out that they
were never bounces.

``blocked`` is the third answer. It is recorded, it is visible, and it
suppresses nothing — the send is still counted as bounced, so the deliverability
brake still sees it and can still pause the mailbox, which is the right response
to a reputation problem and the opposite of deleting the list.

Widening a CHECK constraint is backwards-compatible in the direction that
matters: every existing row still satisfies the new one. Historical rows keep
their ``hard`` label. Reclassifying them is not possible from the ledger alone —
the diagnostic is retained, but a re-run would have to decide for addresses that
have since been suppressed, re-imported or erased, and inventing that history is
worse than leaving it stated as it was recorded.

Revision ID: 113_bounce_kind_blocked
Revises: 112_fix_deal_agent_org_default
"""

from alembic import op

revision = "113_bounce_kind_blocked"
down_revision = "112_fix_deal_agent_org_default"
branch_labels = None
depends_on = None

_OLD = "bounce_kind IN ('hard','soft')"
_NEW = "bounce_kind IN ('hard','soft','blocked')"


def upgrade() -> None:
    op.drop_constraint("ck_bounce_kind", "bounce_records", type_="check")
    op.create_check_constraint("ck_bounce_kind", "bounce_records", _NEW)


def downgrade() -> None:
    # Rows written by the new code would violate the old constraint. Fold them
    # into ``soft``, not ``hard``: soft is the label whose behaviour is closest
    # to what ``blocked`` means here (recorded, retried, not a dead address),
    # and folding to ``hard`` would hand the domain write-off exactly the
    # evidence this revision exists to keep away from it.
    op.execute(
        "UPDATE bounce_records SET bounce_kind = 'soft' WHERE bounce_kind = 'blocked'"
    )
    op.drop_constraint("ck_bounce_kind", "bounce_records", type_="check")
    op.create_check_constraint("ck_bounce_kind", "bounce_records", _OLD)
