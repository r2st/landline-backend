"""Per-org auto-reply opt-in (P1 safety floor).

is_auto_reply_enabled falls back to the global config default until an org sets a
preference, then honours the stored per-org value.
"""

from app.config import get_settings
from app.services import system_settings


async def test_defaults_to_config_when_unset(db_session):
    # No row yet → the global config default decides (True in dev config).
    assert await system_settings.is_auto_reply_enabled(db_session) is bool(
        get_settings().auto_reply_enabled
    )


async def test_opt_out_is_honoured(db_session):
    await system_settings.set_auto_reply_enabled(db_session, False, updated_by="tester")
    assert await system_settings.is_auto_reply_enabled(db_session) is False


async def test_opt_in_is_honoured(db_session):
    await system_settings.set_auto_reply_enabled(db_session, False)
    await system_settings.set_auto_reply_enabled(db_session, True)
    assert await system_settings.is_auto_reply_enabled(db_session) is True
