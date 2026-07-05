#!/usr/bin/env python
"""End-to-end email roundtrip test between two connected accounts.

Sends a real outreach email from one connected Gmail account (the "salesperson")
to another (the "prospect"), then polls the prospect's mailbox via the Gmail API
to confirm delivery and inspect what actually arrived — subject, body, the
List-Unsubscribe header, the CAN-SPAM footer, and the text/plain alternative.

This must run where the DB and OAuth credentials live (i.e. on the server, or
locally with a DATABASE_URL pointing at a DB that has connected accounts). It
sends a REAL email, so only run it against mailboxes you own.

Usage:
    python -m scripts.test_email_roundtrip                  # auto-pick two accounts
    python -m scripts.test_email_roundtrip sales@x prospect@y

The two positional args are the sender and recipient account emails; both must be
connected Gmail accounts in the DB. Without args it uses the first two connected
accounts (sending-enabled first).
"""

import asyncio
import base64
import sys
import time

from sqlalchemy import select

from app.database import async_session_factory
from app.models.gmail_account import GmailAccount
from app.services.email_compose import render
from app.services.email_dispatch import send_outreach_email
from app.services.gmail_sender import GmailSender
from app.services.outreach_templates import MOLDING_SEQUENCE_STEPS


SAMPLE_CTX = {
    "first_name": "Raj",
    "company_name": "OneNet Plastics",
    "title": "VP Operations",
    "demo_link": "https://best-molding-demo.pages.dev",
}


async def _pick_accounts(db, sender_email, recipient_email):
    q = select(GmailAccount).where(GmailAccount.status == "connected")
    accounts = list((await db.execute(q)).scalars().all())
    if len(accounts) < 2:
        raise SystemExit(
            f"Need at least 2 connected Gmail accounts; found {len(accounts)}. "
            "Connect a second account in the app first."
        )
    by_email = {a.email.lower(): a for a in accounts}
    if sender_email and recipient_email:
        sender = by_email.get(sender_email.lower())
        recipient = by_email.get(recipient_email.lower())
        if not sender or not recipient:
            raise SystemExit("One of the given emails is not a connected account.")
        return sender, recipient
    # Auto: prefer a sending-enabled account as sender.
    accounts.sort(key=lambda a: (not getattr(a, "sending_enabled", False)))
    return accounts[0], accounts[1]


def _poll_for_message(recipient_account, subject, timeout=120):
    """Poll the recipient mailbox for the test message. Returns the raw MIME str."""
    sender = GmailSender(recipient_account)  # reuse its credential builder
    service = __import__("googleapiclient.discovery", fromlist=["build"]).build(
        "gmail", "v1", credentials=sender._credentials(), cache_discovery=False
    )
    deadline = time.time() + timeout
    query = f'subject:"{subject}" newer_than:1d'
    while time.time() < deadline:
        resp = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
        msgs = resp.get("messages", [])
        if msgs:
            full = service.users().messages().get(
                userId="me", id=msgs[0]["id"], format="raw"
            ).execute()
            return base64.urlsafe_b64decode(full["raw"]).decode("utf-8", errors="replace")
        time.sleep(5)
    return None


async def main():
    sender_email = sys.argv[1] if len(sys.argv) > 1 else None
    recipient_email = sys.argv[2] if len(sys.argv) > 2 else None

    async with async_session_factory() as db:
        sender, recipient = await _pick_accounts(db, sender_email, recipient_email)

        # Use molding step 1 with a unique marker so we can find it.
        marker = f"[roundtrip {int(time.time())}]"
        _, _, subj_tpl, body_tpl = MOLDING_SEQUENCE_STEPS[0]
        subject = f"{marker} {render(subj_tpl, SAMPLE_CTX)}"
        body_html = render(body_tpl, SAMPLE_CTX)

        print(f"→ Sending from {sender.email}  to  {recipient.email}")
        print(f"  subject: {subject}")
        result = await send_outreach_email(
            db,
            to_email=recipient.email,
            subject=subject,
            body_html=body_html,
            sender_account_id=sender.id,
        )
        print(f"  send result: {result.get('status')}  transport={result.get('transport')}")
        if result.get("status") != "sent":
            raise SystemExit(f"Send failed: {result}")

        print("→ Polling recipient mailbox (up to 120s)…")
        raw = await asyncio.to_thread(_poll_for_message, recipient, subject)

    print("\n================ VERIFICATION ================")
    if not raw:
        print("✗ Message not found in recipient mailbox within timeout.")
        raise SystemExit(1)

    checks = {
        "delivered to inbox": True,
        "List-Unsubscribe header present": "list-unsubscribe:" in raw.lower(),
        "one-click header present": "list-unsubscribe-post:" in raw.lower(),
        "text/plain alternative present": "text/plain" in raw.lower(),
        "text/html part present": "text/html" in raw.lower(),
        "CAN-SPAM footer (Toronto) present": "toronto" in raw.lower(),
        "unsubscribe link present": "unsubscribe" in raw.lower(),
        "demo link present": "best-molding-demo" in raw.lower(),
        "no raw <img> tag": "<img" not in raw.lower(),
    }
    all_ok = True
    for name, ok in checks.items():
        print(f"  {'✓' if ok else '✗'} {name}")
        all_ok = all_ok and ok
    print("=============================================")
    print("RESULT:", "ALL CHECKS PASSED ✓" if all_ok else "SOME CHECKS FAILED ✗")
    raise SystemExit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
