"""Launch sender defaults: pause sending + start warm-up today.

Seeds two ``system_settings`` rows so a fresh production install is safe by
default when the apprend.tech Gmail sender is first configured:

* ``sending_enabled`` = ``{"enabled": false}`` — the global kill switch ships
  ENGAGED (all outbound email paused) until an operator explicitly turns
  sending on. Pairs with the ``SENDING_ENABLED_DEFAULT=false`` env flag.
* ``warmup_start_date`` = the launch date — the conservative 20/50/100-per-day
  ramp starts from go-live rather than a stale configured date.

Both inserts are idempotent: they only fire when the key is absent, so
re-running migrations (or deploying after an operator has already flipped the
switch) never clobbers live state.

Revision ID: 059_launch_sender_defaults
Revises: 058_kill_switch_email_validation
Create Date: 2026-07-15
"""

from alembic import op

revision = "059_launch_sender_defaults"
down_revision = "058_kill_switch_email_validation"
branch_labels = None
depends_on = None

# Launch date — the warm-up ramp starts here.
_WARMUP_START = "2026-07-15"


def upgrade() -> None:
    # Kill switch OFF by default (paused until explicitly enabled).
    op.execute(
        """
        INSERT INTO system_settings (key, value, updated_by)
        SELECT 'sending_enabled',
               '{"enabled": false, "reason": "launch default: paused until apprend.tech sender is verified and enabled"}'::jsonb,
               'migration:059'
        WHERE NOT EXISTS (
            SELECT 1 FROM system_settings WHERE key = 'sending_enabled'
        )
        """
    )
    # Warm-up ramp starts on the launch date.
    op.execute(
        f"""
        INSERT INTO system_settings (key, value, updated_by)
        SELECT 'warmup_start_date',
               '{{"date": "{_WARMUP_START}"}}'::jsonb,
               'migration:059'
        WHERE NOT EXISTS (
            SELECT 1 FROM system_settings WHERE key = 'warmup_start_date'
        )
        """
    )


def downgrade() -> None:
    # Only remove the rows this migration seeded (identified by updated_by).
    op.execute(
        "DELETE FROM system_settings "
        "WHERE key IN ('sending_enabled', 'warmup_start_date') "
        "AND updated_by = 'migration:059'"
    )
