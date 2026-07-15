#!/usr/bin/env python3
"""Configure the outreach sender for launch.

Run this AFTER connecting the apprend.tech Gmail account through the OAuth flow
(Settings → Connect Gmail, as suman.das@apprend.tech). The OAuth grant itself is
interactive and browser-based; this script only flips the DB state that can't be
set from a headless session:

  * marks the given account as the PRIMARY sender (is_active) and enables it for
    the sending rotation,
  * (optionally) resets the warm-up ramp to start today,
  * (optionally) engages/releases the global sending kill switch.

Usage:
    # Make suman.das@apprend.tech the primary sender (default email):
    python scripts/configure_sender.py

    # Explicit email + reset warm-up to today + keep sending PAUSED:
    python scripts/configure_sender.py --email suman.das@apprend.tech --reset-warmup

    # Same, but turn sending ON (only once deliverability is verified):
    python scripts/configure_sender.py --enable-sending

Nothing is emailed and no OAuth is performed — this is pure local DB state.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import async_session_factory  # noqa: E402
from app.models.gmail_account import GmailAccount  # noqa: E402
from app.models.smtp_account import SmtpAccount  # noqa: E402
from app.services import system_settings  # noqa: E402


async def _set_primary(session, email: str) -> bool:
    account = (
        await session.execute(
            select(GmailAccount).where(GmailAccount.email == email)
        )
    ).scalar_one_or_none()
    if account is None:
        print(f"✗ No connected Gmail account found for {email}.")
        print("  Connect it first via the OAuth flow (Settings → Connect Gmail),")
        print("  then re-run this script.")
        return False
    if account.status != "connected":
        print(f"✗ Account {email} exists but status is '{account.status}' (not connected).")
        print("  Reconnect it via OAuth, then re-run.")
        return False

    # Demote every other primary sender across Gmail + SMTP.
    await session.execute(update(GmailAccount).values(is_active=False))
    await session.execute(update(SmtpAccount).values(is_active=False))
    account.is_active = True
    account.sending_enabled = True
    await session.flush()
    print(f"✓ Primary sender set to {email} (is_active=True, sending_enabled=True).")
    return True


async def main(args) -> int:
    async with async_session_factory() as session:
        ok = await _set_primary(session, args.email)
        if not ok and not args.force:
            await session.rollback()
            return 1

        if args.reset_warmup:
            date = await system_settings.reset_warmup_start_date(
                session, updated_by="script:configure_sender"
            )
            print(f"✓ Warm-up ramp reset to start {date} (20/50/100 per day).")

        if args.enable_sending:
            await system_settings.set_sending_enabled(
                session, True, updated_by="script:configure_sender",
                reason="enabled via configure_sender after deliverability check",
            )
            print("✓ Global sending kill switch RELEASED — outbound email is ON.")
        elif args.pause_sending:
            await system_settings.set_sending_enabled(
                session, False, updated_by="script:configure_sender",
                reason="paused via configure_sender",
            )
            print("✓ Global sending kill switch ENGAGED — outbound email is PAUSED.")

        await session.commit()

    status = "ON" if args.enable_sending else "unchanged"
    print()
    print(f"Done. Sending: {status}. Primary sender: {args.email}.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email",
        default=get_settings().primary_sender_email,
        help="Gmail account address to make the primary sender "
        "(default: primary_sender_email from config).",
    )
    parser.add_argument(
        "--reset-warmup", action="store_true",
        help="Reset the warm-up ramp to start today.",
    )
    parser.add_argument(
        "--enable-sending", action="store_true",
        help="Release the kill switch (turn sending ON). Only after verifying deliverability.",
    )
    parser.add_argument(
        "--pause-sending", action="store_true",
        help="Engage the kill switch (turn sending OFF).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Apply warm-up/kill-switch changes even if the primary account isn't found.",
    )
    raise SystemExit(asyncio.run(main(parser.parse_args())))
