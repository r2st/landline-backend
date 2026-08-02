"""System settings service — global kill switch + warm-up persistence.

Central home for runtime-mutable, restart-surviving configuration backed by the
``system_settings`` table. Two responsibilities:

* **Global sending kill switch** (``sending_enabled``). Every autonomous send
  consults :func:`is_sending_enabled` before putting mail on the wire. Flipping
  it off via :func:`set_sending_enabled` also pauses all active enrollments so
  the queue stops picking them up; flipping it back on resumes exactly those.
* **Warm-up start date** (``warmup_start_date``). Persisted here so the ramp
  survives restarts, can be reset from the UI, and is auto-stamped on the first
  real send (see :func:`ensure_warmup_started`).

All reads are fail-safe: if the row is missing or the table doesn't exist yet
(pre-migration), sending defaults to **enabled** rather than silently halting a
production system on a transient DB hiccup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.contact import Contact  # noqa: F401 — ensure model import side effects
from app.models.outreach import OutreachEnrollment
from app.models.system_setting import SystemSetting
from app.org_context import scope_write_to_current_org

logger = logging.getLogger(__name__)

SENDING_ENABLED_KEY = "sending_enabled"
WARMUP_START_KEY = "warmup_start_date"
# Per-org opt-in for autonomous auto-replies (P1 safety floor). Distinct from the
# global ``auto_reply_enabled`` config: sending is a per-tenant decision, so an
# org must opt in for the SmartReplyService to auto-send on its behalf. When the
# row is unset the global config default applies, preserving current behaviour.
AUTO_REPLY_ENABLED_KEY = "auto_reply_enabled"


async def _get_row(db: AsyncSession, key: str) -> Optional[SystemSetting]:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    return result.scalar_one_or_none()


async def get_value(db: AsyncSession, key: str, default: Any = None) -> Any:
    """Return the JSON ``value`` for a setting key, or ``default`` if unset.

    Never raises — a lookup failure (e.g. the table doesn't exist yet) returns
    the default so callers on the hot send path stay resilient.
    """
    try:
        row = await _get_row(db, key)
    except Exception as exc:  # pragma: no cover - defensive (pre-migration / DB blip)
        logger.warning("system_settings lookup failed for %s: %s", key, exc)
        return default
    return row.value if row is not None else default


async def set_value(
    db: AsyncSession, key: str, value: dict, updated_by: Optional[str] = None
) -> SystemSetting:
    """Upsert a setting row (portable — no PG-specific ON CONFLICT)."""
    row = await _get_row(db, key)
    if row is None:
        row = SystemSetting(key=key, value=value, updated_by=updated_by)
        db.add(row)
    else:
        row.value = value
        row.updated_by = updated_by
        row.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return row


# --- Global sending kill switch -------------------------------------------------


def _default_sending_enabled() -> bool:
    """Fallback kill-switch state when it has never been explicitly configured.

    Controlled by ``sending_enabled_default`` — production sets it False so a
    fresh install stays paused until an operator turns sending on.
    """
    return bool(get_settings().sending_enabled_default)


async def is_sending_enabled(db: AsyncSession) -> bool:
    """Whether autonomous email sending is currently allowed.

    Defaults to ``sending_enabled_default`` (True in dev, False in production)
    when the switch has never been explicitly set.
    """
    value = await get_value(db, SENDING_ENABLED_KEY, default=None)
    if not value:
        return _default_sending_enabled()  # never configured → config default
    return bool(value.get("enabled", _default_sending_enabled()))


async def get_sending_status(db: AsyncSession) -> dict:
    """Full kill-switch status for display."""
    row = await _get_row(db, SENDING_ENABLED_KEY)
    if row is None:
        return {
            "enabled": _default_sending_enabled(),
            "reason": None,
            "updated_by": None,
            "updated_at": None,
        }
    value = row.value or {}
    return {
        "enabled": bool(value.get("enabled", _default_sending_enabled())),
        "reason": value.get("reason"),
        "updated_by": row.updated_by,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def set_sending_enabled(
    db: AsyncSession,
    enabled: bool,
    updated_by: Optional[str] = None,
    reason: Optional[str] = None,
) -> dict:
    """Flip this org's kill switch and cascade to its active enrollments.

    Disabling pauses every ``active`` enrollment (marking ``kill_switch_paused``)
    so the outreach queue immediately stops dispatching. Enabling resumes only
    the enrollments the kill switch paused — enrollments paused for other reasons
    (no email, consent, bounce) are left as-is. Returns the new status plus the
    number of enrollments affected.

    **The cascade is scoped to the caller's org**, and the setting always was:
    ``system_settings`` is per-tenant (migration 080 made ``(org_id, key)``
    unique precisely so two orgs could hold a kill switch independently). Only
    the two UPDATEs below were platform-wide, because a Core ``update()``
    receives no ``org_id`` predicate from the global read filter.

    That combination was the worst of the two. The switch reads as scoped —
    in the model, in the database, in the UI — and halted the platform. The
    resume side was worse still: it did not merely resume strangers' outreach,
    it *defeated their kill switch*. Org B pauses because its sending domain is
    on fire; org A, who has never heard of org B, releases its own switch and B
    starts sending again with B's UI still reporting the switch as engaged. A
    safety control another tenant can silently release is not a safety control.
    """
    await set_value(
        db,
        SENDING_ENABLED_KEY,
        {"enabled": enabled, "reason": reason},
        updated_by=updated_by,
    )

    if not enabled:
        result = await db.execute(
            scope_write_to_current_org(
                update(OutreachEnrollment).where(
                    OutreachEnrollment.status == "active"
                ),
                OutreachEnrollment,
            ).values(status="paused", kill_switch_paused=True)
        )
        affected = result.rowcount or 0
        logger.warning(
            "Dialtone PAUSED (kill switch engaged) by %s (%s) — sending halted, %d enrollments paused",
            updated_by or "system", reason or "no reason given", affected,
        )
        await _notify_admin_killswitch(engaged=True, actor=updated_by, reason=reason, count=affected)
    else:
        result = await db.execute(
            scope_write_to_current_org(
                update(OutreachEnrollment).where(
                    OutreachEnrollment.kill_switch_paused == True  # noqa: E712
                ),
                OutreachEnrollment,
            ).values(status="active", kill_switch_paused=False)
        )
        affected = result.rowcount or 0
        logger.warning(
            "Dialtone RESUMED (kill switch released) by %s — sending resumed, %d enrollments reactivated",
            updated_by or "system", affected,
        )
        await _notify_admin_killswitch(engaged=False, actor=updated_by, reason=reason, count=affected)

    await db.flush()
    status = await get_sending_status(db)
    status["enrollments_affected"] = affected
    return status


async def _notify_admin_killswitch(
    *, engaged: bool, actor: Optional[str], reason: Optional[str], count: int
) -> None:
    """Best-effort admin notification when the kill switch flips. Never raises."""
    settings = get_settings()
    to = getattr(settings, "notification_email", "") or ""
    if not to:
        return
    try:
        from app.services.email_sender import EmailSender

        action = "PAUSED — all sending halted" if engaged else "RESUMED — sending back on"
        subject = f"[Dialtone] {action}"
        body = (
            f"Dialtone was {'paused (kill switch engaged)' if engaged else 'resumed (kill switch released)'}.\n\n"
            f"By: {actor or 'system'}\n"
            f"Reason: {reason or 'n/a'}\n"
            f"Enrollments affected: {count}\n"
        )
        import asyncio

        sender = EmailSender()
        await asyncio.to_thread(
            sender.send_email,
            to_email=to,
            subject=subject,
            body_html=f"<pre>{body}</pre>",
            body_text=body,
            include_unsubscribe=False,
        )
    except Exception as exc:  # pragma: no cover - notification is best-effort
        logger.info("Kill-switch admin notification skipped: %s", exc)


# --- Per-org auto-reply opt-in --------------------------------------------------


async def is_auto_reply_enabled(db: AsyncSession) -> bool:
    """Whether THIS org has opted in to autonomous auto-replies.

    Falls back to the global ``auto_reply_enabled`` config when the org has never
    set a preference (fail-safe: the global default decides). Never raises — a
    lookup failure degrades to the config default rather than silently changing
    send behaviour.
    """
    default = bool(get_settings().auto_reply_enabled)
    value = await get_value(db, AUTO_REPLY_ENABLED_KEY, default=None)
    if not value:
        return default
    return bool(value.get("enabled", default))


async def set_auto_reply_enabled(
    db: AsyncSession, enabled: bool, updated_by: Optional[str] = None
) -> dict:
    """Set the per-org auto-reply opt-in. Returns the new state."""
    await set_value(db, AUTO_REPLY_ENABLED_KEY, {"enabled": enabled}, updated_by=updated_by)
    await db.flush()
    return {"enabled": enabled}


# --- Warm-up start date ---------------------------------------------------------


async def get_warmup_start_date(db: AsyncSession) -> Optional[str]:
    """Effective warm-up start date (ISO): system_settings override or config.

    A persisted value in ``system_settings`` (set on first send or via reset)
    takes precedence over the static ``warmup_start_date`` config so a stale
    env value never keeps the ramp pinned in the past.
    """
    value = await get_value(db, WARMUP_START_KEY, default=None)
    if value and value.get("date"):
        return value["date"]
    configured = get_settings().warmup_start_date
    return configured or None


async def ensure_warmup_started(db: AsyncSession) -> str:
    """Stamp today as the warm-up start if it has never been persisted.

    Called after the first successful send so the ramp auto-starts from the real
    first-send date, overriding any stale configured date.
    """
    existing = await get_value(db, WARMUP_START_KEY, default=None)
    if existing and existing.get("date"):
        return existing["date"]
    today = datetime.now(timezone.utc).date().isoformat()
    await set_value(db, WARMUP_START_KEY, {"date": today}, updated_by="auto:first-send")
    logger.info("Warm-up start date auto-set to %s on first send", today)
    return today


async def reset_warmup_start_date(
    db: AsyncSession, updated_by: Optional[str] = None
) -> str:
    """Reset the warm-up ramp to start today. Returns the new ISO date."""
    today = datetime.now(timezone.utc).date().isoformat()
    await set_value(db, WARMUP_START_KEY, {"date": today}, updated_by=updated_by or "reset")
    logger.info("Warm-up start date reset to %s by %s", today, updated_by or "reset")
    return today
