"""Bounce and deliverability read models: what bounced, and where.

Two questions the existing surfaces cannot answer.

**"What actually bounced?"** ``BounceRecord`` has been the ingestion ledger since
the sweep needed a dedup key, and nothing ever read it back. The only bounce
number anywhere is an aggregate rate — ``danger_monitor`` computes one to decide
whether to pull the kill switch, the funnel reports one per campaign — and an
aggregate rate is exactly the wrong shape for the question you have when it
moves, which is *which addresses, from which sender, saying what*.

**"Who is refusing our mail?"** This is the bigger gap. Inbox providers do not
block a sender globally; they block per receiving domain, and they do it quietly.
Outlook starts 550-ing while Gmail keeps accepting, and the blended bounce rate
moves from 1.2% to 2.8% — under every alarm threshold in the system, because the
domain that is rejecting *everything* is diluted by the domains that are fine.
By the time a blended rate breaches, the block is weeks old. Splitting the same
events by recipient domain turns a number nobody can act on into a name.

Denominators
------------

Sends are counted from the same source as the bounces being divided by them:
``OutreachEvent`` for the sequence engine. A rate that divided ``BounceRecord``
rows (which cover every path, including manual template sends) by sequence sends
would over-report, and the error would grow with however much manual mail was
being sent that week.

Both are reported per domain, but a rate is only computed where there is enough
volume to mean anything: two sends and one bounce is not a 50% bounce rate, and
sorting a dashboard by it puts the noisiest domain on top every time.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bounce_record import BounceRecord
from app.models.contact import Contact
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.suppression import SuppressionEntry

logger = logging.getLogger(__name__)

MAX_DAYS = 365

# Below this many sends a domain's rates are reported but flagged
# ``sufficient_sample=False`` and never used for a risk verdict. Matches
# ``danger_min_sample_size`` in spirit: the same reason a global bounce alarm
# does not fire on a sample of three.
DEFAULT_MIN_SENDS = 20

# Recipient-domain risk bands, as a fraction of sends to that domain. Deliberately
# tighter than the global kill-switch threshold (5%): a *single* domain at 5% is a
# much stronger signal than a blended 5%, because the blend has healthy domains
# holding it down and this does not.
DOMAIN_BOUNCE_BLOCKED = 0.20
DOMAIN_BOUNCE_AT_RISK = 0.08


def _rate(numerator: int, denominator: int) -> float:
    """Percentage to one decimal, or 0.0 when there is nothing to divide."""
    if not denominator:
        return 0.0
    return round(100.0 * numerator / denominator, 1)


def email_domain(email: Optional[str]) -> Optional[str]:
    """The lower-cased domain part of an address, or ``None`` if unparseable."""
    if not email:
        return None
    _, _, domain = (email or "").strip().lower().rpartition("@")
    return domain or None


def _as_date(value: Any) -> Optional[date]:
    """Normalise whatever the driver returned for ``date(col)``.

    Postgres hands back a ``date``; SQLite hands back the string it stored.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        logger.warning("Unparseable bounce date %r — skipping", value)
        return None


def _window_start(days: int) -> datetime:
    days = max(1, min(int(days), MAX_DAYS))
    return datetime.now(timezone.utc) - timedelta(days=days)


async def _sends_by_domain(
    db: AsyncSession, since: datetime
) -> tuple[dict[str, dict[str, int]], int]:
    """Sequence-engine events per recipient domain, and the total send count.

    Aggregated in SQL by *address* and event type, then folded into domains here.
    Extracting the domain in SQL would need ``split_part`` (Postgres) or
    ``instr``/``substr`` (SQLite) — the two databases in play disagree, and a
    read model is not worth a dialect branch. The SQL side is already a GROUP BY,
    so what crosses the wire is one row per (address, event type) that has any
    activity in the window, not one per event.
    """
    rows = (
        await db.execute(
            select(Contact.email, OutreachEvent.event_type, func.count(OutreachEvent.id))
            .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            .join(Contact, OutreachEnrollment.contact_id == Contact.id)
            .where(
                OutreachEvent.occurred_at >= since,
                OutreachEvent.event_type.in_(
                    ("sent", "opened", "clicked", "replied", "bounced", "unsubscribed")
                ),
                Contact.email.isnot(None),
            )
            .group_by(Contact.email, OutreachEvent.event_type)
        )
    ).all()

    by_domain: dict[str, dict[str, int]] = {}
    total_sent = 0
    for email, event_type, n in rows:
        domain = email_domain(email)
        if domain is None:
            continue
        bucket = by_domain.setdefault(domain, {})
        bucket[event_type] = bucket.get(event_type, 0) + n
        # Contacts are tracked per domain too — "12 bounces" reads very
        # differently against 12 addresses than against one retried twelve times.
        if event_type == "sent":
            total_sent += n
            bucket["_contacts"] = bucket.get("_contacts", 0) + 1
    return by_domain, total_sent


async def by_recipient_domain(
    db: AsyncSession,
    *,
    days: int = 30,
    min_sends: int = DEFAULT_MIN_SENDS,
    limit: int = 100,
) -> dict:
    """Deliverability split by the domain we were mailing *into*.

    Riskiest first: domains whose bounce rate says they are refusing us, then by
    volume, so the top of the list is always the domain costing the most
    reputation rather than the one with the most dramatic ratio over four sends.
    """
    days = max(1, min(int(days), MAX_DAYS))
    since = _window_start(days)
    by_domain, total_sent = await _sends_by_domain(db, since)

    # Hard/soft split comes from the bounce ledger, which is the only place the
    # classification is kept — ``OutreachEvent`` records that a send bounced, not
    # what kind. The two counts can legitimately differ (the ledger also covers
    # manual sends), so they are reported side by side rather than reconciled.
    ledger_rows = (
        await db.execute(
            select(
                BounceRecord.recipient_email,
                BounceRecord.bounce_kind,
                func.count(BounceRecord.id),
            )
            .where(BounceRecord.processed_at >= since)
            .group_by(BounceRecord.recipient_email, BounceRecord.bounce_kind)
        )
    ).all()
    hard: Counter = Counter()
    soft: Counter = Counter()
    for email, kind, n in ledger_rows:
        domain = email_domain(email)
        if domain is None:
            continue
        (hard if kind == "hard" else soft)[domain] += n

    domains: list[dict] = []
    for domain, counts in by_domain.items():
        sent = counts.get("sent", 0)
        bounced = counts.get("bounced", 0)
        sufficient = sent >= min_sends
        bounce_rate = _rate(bounced, sent)

        if not sufficient:
            risk = "insufficient_data"
        elif bounced / sent >= DOMAIN_BOUNCE_BLOCKED:
            risk = "blocked"
        elif bounced / sent >= DOMAIN_BOUNCE_AT_RISK:
            risk = "at_risk"
        else:
            risk = "healthy"

        domains.append({
            "domain": domain,
            "contacts": counts.get("_contacts", 0),
            "sent": sent,
            "opened": counts.get("opened", 0),
            "clicked": counts.get("clicked", 0),
            "replied": counts.get("replied", 0),
            "bounced": bounced,
            "unsubscribed": counts.get("unsubscribed", 0),
            "hard_bounces": hard.get(domain, 0),
            "soft_bounces": soft.get(domain, 0),
            "open_rate": _rate(counts.get("opened", 0), sent),
            "reply_rate": _rate(counts.get("replied", 0), sent),
            "bounce_rate": bounce_rate,
            "sufficient_sample": sufficient,
            "risk": risk,
        })

    _RISK_ORDER = {"blocked": 0, "at_risk": 1, "healthy": 2, "insufficient_data": 3}
    domains.sort(key=lambda d: (_RISK_ORDER[d["risk"]], -d["sent"], d["domain"]))

    flagged = [d for d in domains if d["risk"] in ("blocked", "at_risk")]
    return {
        "timeframe_days": days,
        "min_sends": min_sends,
        "total_sent": total_sent,
        "domains_seen": len(domains),
        # The share of volume going somewhere that is rejecting it. This is the
        # number to alarm on: a single blocked domain matters in proportion to
        # how much of the send budget is aimed at it.
        "sent_to_flagged_domains": sum(d["sent"] for d in flagged),
        "flagged": [d["domain"] for d in flagged],
        "domains": domains[:limit],
    }


async def summary(db: AsyncSession, *, days: int = 30) -> dict:
    """What bounced in the window, classified, attributed and trended.

    ``by_kind`` and ``by_sender`` come from the bounce ledger, which is the only
    record carrying the DSN classification and the mailbox that received the
    notification. ``daily`` is the ledger too, so the trend line and the totals
    above it cannot disagree.
    """
    days = max(1, min(int(days), MAX_DAYS))
    since = _window_start(days)

    kind_rows = (
        await db.execute(
            select(BounceRecord.bounce_kind, func.count(BounceRecord.id))
            .where(BounceRecord.processed_at >= since)
            .group_by(BounceRecord.bounce_kind)
        )
    ).all()
    by_kind = {kind: n for kind, n in kind_rows}
    hard = by_kind.get("hard", 0)
    soft = by_kind.get("soft", 0)

    # Sends in the same window, for the rate. Sequence engine only — see module
    # docstring on why this is not the union of both send tables.
    sent = (
        await db.execute(
            select(func.count(OutreachEvent.id)).where(
                OutreachEvent.event_type == "sent",
                OutreachEvent.occurred_at >= since,
            )
        )
    ).scalar() or 0

    # Distinct addresses, not notifications: a mailbox that soft-bounces daily
    # for a week is one bad address, and counting it as seven makes a stable
    # problem look like a growing one.
    distinct_addresses = (
        await db.execute(
            select(func.count(func.distinct(BounceRecord.recipient_email)))
            .where(BounceRecord.processed_at >= since)
        )
    ).scalar() or 0

    day_col = func.date(BounceRecord.processed_at)
    daily_rows = (
        await db.execute(
            select(day_col, BounceRecord.bounce_kind, func.count(BounceRecord.id))
            .where(BounceRecord.processed_at >= since)
            .group_by(day_col, BounceRecord.bounce_kind)
        )
    ).all()
    per_day: dict[date, dict[str, int]] = {}
    for raw_day, kind, n in daily_rows:
        day = _as_date(raw_day)
        if day is None:
            continue
        per_day.setdefault(day, {})[kind] = n
    daily = [
        {
            "day": day,
            "hard": counts.get("hard", 0),
            "soft": counts.get("soft", 0),
            "total": sum(counts.values()),
        }
        for day, counts in sorted(per_day.items())
    ]

    # Which of our mailboxes the DSNs came back to. A bounce spike concentrated
    # on one sender is a sender problem (a cold mailbox, a broken SPF record); a
    # spike spread evenly across the fleet is a list problem.
    sender_rows = (
        await db.execute(
            select(
                BounceRecord.gmail_account_id,
                GmailAccount.email,
                func.count(BounceRecord.id),
            )
            .outerjoin(GmailAccount, BounceRecord.gmail_account_id == GmailAccount.id)
            .where(BounceRecord.processed_at >= since)
            .group_by(BounceRecord.gmail_account_id, GmailAccount.email)
            .order_by(func.count(BounceRecord.id).desc())
        )
    ).all()
    by_sender = [
        {
            "account_id": account_id,
            "email": email,
            "bounces": n,
        }
        for account_id, email, n in sender_rows
    ]

    # Suppressions this window's bounces produced. The point of the number is
    # that it should track ``distinct_addresses`` for hard bounces; a large gap
    # means addresses are bouncing without being taken out of rotation, which is
    # the failure mode that turns one bounce into a reputation problem.
    auto_suppressed = (
        await db.execute(
            select(func.count(SuppressionEntry.id)).where(
                SuppressionEntry.reason == "bounce",
                SuppressionEntry.source == "auto",
                SuppressionEntry.created_at >= since,
            )
        )
    ).scalar() or 0

    top_diagnostics = (
        await db.execute(
            select(BounceRecord.diagnostic)
            .where(
                BounceRecord.processed_at >= since,
                BounceRecord.diagnostic.isnot(None),
            )
            .limit(2000)
        )
    ).scalars().all()
    # First line only: the tail of a DSN diagnostic carries the recipient and a
    # message id, which would split one provider's single refusal reason into as
    # many unique strings as there were bounces.
    tally = Counter(
        (d or "").strip().splitlines()[0][:160]
        for d in top_diagnostics if (d or "").strip()
    )

    return {
        "timeframe_days": days,
        "total": hard + soft,
        "hard": hard,
        "soft": soft,
        "distinct_addresses": distinct_addresses,
        "sent": sent,
        "bounce_rate": _rate(hard + soft, sent),
        "hard_bounce_rate": _rate(hard, sent),
        "auto_suppressed": auto_suppressed,
        "daily": daily,
        "by_sender": by_sender,
        "top_diagnostics": [
            {"diagnostic": d, "count": n} for d, n in tally.most_common(5)
        ],
    }


async def recent(
    db: AsyncSession,
    *,
    days: int = 30,
    kind: Optional[str] = None,
    domain: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """The bounce ledger itself, newest first, with the contact resolved.

    ``domain`` filters with a trailing-``@`` suffix match rather than a bare
    substring: ``domain=acme.com`` must not also return ``notacme.com`` or an
    address whose *local* part happens to contain the string.
    """
    since = _window_start(days)
    conditions = [BounceRecord.processed_at >= since]
    if kind in ("hard", "soft"):
        conditions.append(BounceRecord.bounce_kind == kind)
    if domain:
        conditions.append(
            BounceRecord.recipient_email.ilike(f"%@{domain.strip().lower()}")
        )
    if q:
        conditions.append(BounceRecord.recipient_email.ilike(f"%{q.strip().lower()}%"))

    total = (
        await db.execute(select(func.count(BounceRecord.id)).where(*conditions))
    ).scalar() or 0

    # Only the sender is joined. Contacts are resolved separately and
    # deliberately: an address can have **several** contact rows — a re-import
    # creates a new one for a mailbox we already had — and joining on email would
    # emit one page row per contact, so a page would hold fewer than ``limit``
    # bounces while reporting a ``total`` that counted each once.
    rows = (
        await db.execute(
            select(BounceRecord, GmailAccount.email)
            .outerjoin(GmailAccount, BounceRecord.gmail_account_id == GmailAccount.id)
            .where(*conditions)
            .order_by(BounceRecord.processed_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    page_emails = {record.recipient_email for record, _ in rows}
    contacts: dict[str, Contact] = {}
    listed: set[str] = set()
    if page_emails:
        # Newest row wins where an address has more than one, which is the one a
        # send path would most likely pick up next.
        found = (
            await db.execute(
                select(Contact)
                .where(func.lower(Contact.email).in_(page_emails))
                .order_by(Contact.created_at.asc())
            )
        ).scalars().all()
        for contact in found:
            contacts[(contact.email or "").lower()] = contact

        # "Is this address still mailable?" is what the list is read for, and the
        # contact flag alone cannot answer it: the address-level suppression list
        # also covers addresses with no contact row at all.
        listed = set(
            (
                await db.execute(
                    select(SuppressionEntry.email).where(
                        SuppressionEntry.email.in_(page_emails)
                    )
                )
            ).scalars().all()
        )

    items = []
    for record, sender_email in rows:
        contact = contacts.get(record.recipient_email)
        name = None
        if contact is not None:
            name = " ".join(
                p for p in (contact.first_name, contact.last_name) if p
            ).strip() or None
        items.append({
            "id": record.id,
            "recipient_email": record.recipient_email,
            "domain": email_domain(record.recipient_email),
            "bounce_kind": record.bounce_kind,
            "status_code": record.status_code,
            "diagnostic": record.diagnostic,
            "processed_at": record.processed_at,
            "sender_email": sender_email,
            "contact_id": contact.id if contact is not None else None,
            "contact_name": name,
            # Whether the address is currently out of rotation, by either gate.
            # A hard-bounced address showing ``False`` here is the thing worth
            # chasing: it means the send paths can still pick it up.
            "suppressed": (
                record.recipient_email in listed
                or bool(getattr(contact, "suppressed", False))
            ),
            "result": record.result or {},
        })

    return {"total": total, "limit": limit, "offset": offset, "bounces": items}
