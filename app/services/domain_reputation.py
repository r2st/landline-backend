"""Is this recipient *domain* still worth spending a send on?

Every deliverability gate in this system reasons about one address at a time.
``email_validation`` checks that a domain publishes MX; ``bounce_handler``
suppresses the mailbox that bounced; the suppression list remembers it after the
contact row is gone. All of them are keyed on an address, and none of them can
notice that the last three addresses we tried at ``acme.com`` were all dead.

That blind spot is reachable, and the pipeline aims straight at it:

* ``email_pattern_generator`` *derives* addresses from a name and a domain when
  Apollo and Hunter are out of credits, and says so plainly — a generated address
  is a guess, written with ``email_status='unknown'``, which the send path mails.
  When ``firstname.lastname@`` is the wrong convention for a company, it is wrong
  for **everyone** there, so the guesses fail as a set, not individually.
* Round 9's scraper fix deliberately keeps generic inboxes so SMB shops aren't
  stranded, which makes four contacts at one company the normal case.
* The MX gate cannot catch any of this. A folded company keeps paying for DNS,
  and a mail host that has decided to 550 everything we send still publishes MX
  and still answers. Both look perfectly healthy to a pre-send lookup.

So we walk the list: guess, bounce, suppress the address, guess the next one,
bounce. Each of those is a hard bounce charged against the *global* rate the
kill switch reads at 5% — the rail is aggregate, so a handful of dead domains
spends the whole fleet's safety margin.

``bounce_analytics.by_recipient_domain`` already computes exactly this shape and
its docstring names the failure — "the domain that is rejecting *everything* is
diluted by the domains that are fine". It is a **read model**: it renders on a
dashboard and nothing consults it before sending. This module is the acting half.

The verdict, and why each clause is there
-----------------------------------------

A domain is written off only when all three hold:

1. **Hard bounces at ≥ N distinct addresses** (``dead_domain_bounce_threshold``,
   default 3). *Distinct* is the load-bearing word: one mailbox that bounces
   every retry is one bad address, not a bad domain — the same distinction
   ``bounce_analytics`` draws between "12 bounces" across twelve addresses and
   one retried twelve times. Hard only; a soft bounce is transient by
   definition and suppressing a domain over greylisting would be absurd.
2. **No sign of life, ever.** Not one open, click or reply from any address at
   the domain, across *both* send tables. This is the clause that makes the rule
   safe: any domain we have a real relationship with is exempt automatically,
   and it needs no list to maintain. Positive evidence outranks any amount of
   negative evidence — three dead mailboxes at a company that has replied to us
   means we have three bad addresses, not a dead domain.
3. **Not a mailbox provider.** ``gmail.com`` cannot be written off on three
   bounces; the whole fleet mails into it. Clause 2 would normally cover this
   (a provider that big always has engagement) but not on day one of a fresh
   deployment, and "we blocked Gmail because the list was young" is not a
   failure worth risking to save a list.

Read-only by design. Acting on the verdict — suppressing the addresses, stopping
the enrollments — belongs to ``bounce_handler``, which already owns that
machinery and does it in one place.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.bounce_record import BounceRecord
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.services.sql_search import LIKE_ESCAPE, contains_pattern, endswith_pattern

logger = logging.getLogger(__name__)

# Mailbox providers and the large freemail hosts. Never written off, however
# many of their mailboxes turn out to be dead: a bounce here is one wrong
# address out of a namespace of a billion, and the blast radius of being wrong
# is the entire send programme rather than one company.
#
# Kept deliberately short. This is a floor under clause 3, not an attempt to
# enumerate the internet — clause 2 (any engagement, ever) is what actually
# protects the long tail of real companies.
MAILBOX_PROVIDER_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "google.com",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com",
    "office365.com", "microsoft.com",
    "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "ymail.com", "rocketmail.com",
    "aol.com", "icloud.com", "me.com", "mac.com",
    "proton.me", "protonmail.com", "pm.me",
    "gmx.com", "gmx.net", "mail.com", "zoho.com",
    "yandex.com", "yandex.ru", "qq.com", "163.com", "126.com",
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "btinternet.com",
})

# Engagement is proof a domain accepts and reads our mail. Anything weaker
# ("sent", "delivered") is not: we record a send when the dispatcher returns,
# which tells us nothing about what the receiving host did with it afterwards.
_LIVE_EVENTS = ("opened", "clicked", "replied")


def domain_of(email: Optional[str]) -> Optional[str]:
    """The lower-cased domain part of an address, or ``None`` if unparseable."""
    if not email or "@" not in email:
        return None
    _, _, domain = email.strip().lower().rpartition("@")
    return domain or None


def _at_domain(column, domain: str):
    """SQL for "this address is at exactly ``domain``".

    A trailing-``@`` suffix match, not a substring: ``acme.com`` must not also
    match ``notacme.com``, nor an address whose *local* part contains the string.
    Mirrors ``bounce_analytics.recent``'s domain filter, for the same reason.

    Subdomains are deliberately distinct. ``mail.acme.com`` is a different mail
    host from ``acme.com`` and can be healthy while the parent is dead.
    """
    return func.lower(column).like(
        endswith_pattern(f"@{domain}"), escape=LIKE_ESCAPE
    )


async def hard_bounced_addresses(db: AsyncSession, domain: str) -> set[str]:
    """Distinct addresses at ``domain`` that have ever hard-bounced.

    Two sources, unioned, because neither is complete on its own:

    * ``BounceRecord`` is the ingestion ledger — it exists as the sweep's dedup
      key, so only the mailbox sweep writes it. The deliverability router's
      manual entry and the reply task's DSN handling both call ``record_bounce``
      directly and leave no ledger row.
    * ``Contact.last_bounce_type`` is stamped by ``record_bounce`` on every path,
      but only where a contact row exists — and a hard bounce for an address we
      no longer have a contact for is exactly what the address-level suppression
      list was added for.

    Counting either alone would let a domain keep its send budget purely because
    of which code path noticed it was dead.
    """
    ledger = (
        await db.execute(
            select(func.distinct(func.lower(BounceRecord.recipient_email))).where(
                BounceRecord.bounce_kind == "hard",
                _at_domain(BounceRecord.recipient_email, domain),
            )
        )
    ).scalars().all()

    stamped = (
        await db.execute(
            select(func.distinct(func.lower(Contact.email))).where(
                Contact.last_bounce_type == "hard",
                Contact.email.isnot(None),
                _at_domain(Contact.email, domain),
            )
        )
    ).scalars().all()

    return {a for a in list(ledger) + list(stamped) if a}


async def has_engagement(db: AsyncSession, domain: str) -> bool:
    """Whether any address at ``domain`` ever opened, clicked or replied.

    Both send tables are read. Sequence sends write ``OutreachEvent`` and no
    ``EmailSend`` row at all, so checking one would make a domain look silent
    purely because its relationship ran through the other path — the same split
    that made the unified inbox blind to every sequence send.
    """
    seq = (
        await db.execute(
            select(OutreachEvent.id)
            .join(
                OutreachEnrollment,
                OutreachEvent.enrollment_id == OutreachEnrollment.id,
            )
            .join(Contact, OutreachEnrollment.contact_id == Contact.id)
            .where(
                OutreachEvent.event_type.in_(_LIVE_EVENTS),
                Contact.email.isnot(None),
                _at_domain(Contact.email, domain),
            )
            .limit(1)
        )
    ).first()
    if seq is not None:
        return True

    direct = (
        await db.execute(
            select(EmailSend.id)
            .where(
                or_(
                    EmailSend.opened_at.isnot(None),
                    EmailSend.clicked_at.isnot(None),
                    EmailSend.replied_at.isnot(None),
                ),
                _at_domain(EmailSend.to_email, domain),
            )
            .limit(1)
        )
    ).first()
    return direct is not None


async def is_domain_dead(
    db: AsyncSession, domain: Optional[str], *, threshold: Optional[int] = None
) -> tuple[bool, str]:
    """Whether ``domain`` should be written off. Returns ``(dead, reason)``.

    ``reason`` is "" when the domain is fine, so a caller can log or surface the
    verdict without reconstructing it. See the module docstring for the three
    clauses and why each one is there.

    A ``threshold`` below 1 disables the rule outright — an operator who wants
    the old address-at-a-time behaviour gets it by configuration rather than by
    a value that silently writes off every domain with a single bounce.
    """
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        return False, ""

    if threshold is None:
        threshold = get_settings().dead_domain_bounce_threshold
    if threshold < 1:
        return False, ""

    if domain in MAILBOX_PROVIDER_DOMAINS:
        return False, ""

    dead_addresses = await hard_bounced_addresses(db, domain)
    if len(dead_addresses) < threshold:
        return False, ""

    if await has_engagement(db, domain):
        # Positive evidence outranks negative. We have bad addresses here, not a
        # dead domain, and the address-level suppression already handled them.
        return False, ""

    return True, (
        f"{len(dead_addresses)} distinct addresses at {domain} have hard-bounced "
        f"and nothing there has ever opened, clicked or replied"
    )


async def contacts_at_domain(
    db: AsyncSession, domain: str, *, only_mailable: bool = True
) -> list[Contact]:
    """Contacts whose address is at ``domain``.

    ``only_mailable`` skips rows already out of play, so a caller suppressing a
    dead domain does not re-walk contacts a previous bounce already handled and
    over-report what it took out.
    """
    query = select(Contact).where(
        Contact.email.isnot(None), _at_domain(Contact.email, domain)
    )
    if only_mailable:
        query = query.where(
            or_(Contact.suppressed.is_(False), Contact.suppressed.is_(None))
        )
    return list((await db.execute(query)).scalars().all())
