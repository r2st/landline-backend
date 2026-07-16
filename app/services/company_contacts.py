"""Resolving which contact represents a company.

Every surface that shows a company in a list — the companies table, the
prospecting review queue, the Dialtone activity feed — needs to answer "who
would we actually email here?". That question has one answer, so it lives in
one place rather than being re-derived (differently) per router.

``is_primary`` is *not* sufficient on its own: it is set by hand, and the
autonomously-discovered companies (Places + enrichment) never set it, so
ranking on that flag alone leaves the majority of companies looking like they
have no contact at all. The ordering below falls back to the emailable
contact, which is the one outreach would pick anyway.
"""

import uuid
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact


def _rank(contact: Contact) -> tuple:
    """Sort key: whoever outreach would actually email, first.

    Reachability outranks the ``is_primary`` flag deliberately, to match the
    enrolment path in ``autonomous_prospector`` — it selects on a non-empty
    email and only then prefers ``is_primary``. Ranking the flag higher would
    let the dashboard name one contact while a sequence emailed a different
    one. Blocked contacts (opted out / suppressed) are not reachable, so they
    never front a company.
    """
    emailable = bool(contact.email and contact.email.strip())
    blocked = bool(contact.suppressed or contact.unsubscribed)
    return (
        not (emailable and not blocked),  # False (0) sorts first
        not bool(contact.is_primary),
        not emailable,
        contact.created_at,
    )


def rank_contacts(contacts) -> list[Contact]:
    """The company's contacts, best-first. Never mutates the caller's list —
    sorting ``Company.contacts`` in place would dirty the ORM relationship.
    """
    return sorted(contacts, key=_rank)


async def load_company_contacts(
    db: AsyncSession, company_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[Contact]]:
    """Bulk-load contacts for these companies, best-first within each company.

    For callers that hold candidates rather than Company rows (the review queue,
    the activity feed) and so can't lean on the eagerly-loaded relationship.
    One query for the whole page rather than one per row. ``card_image`` is a
    deferred column, so the base64 blobs are not dragged along.
    """
    ids = [i for i in company_ids if i is not None]
    if not ids:
        return {}
    rows = (
        await db.execute(select(Contact).where(Contact.company_id.in_(ids)))
    ).scalars().all()

    by_company: dict[uuid.UUID, list[Contact]] = defaultdict(list)
    for contact in rows:
        by_company[contact.company_id].append(contact)
    return {cid: rank_contacts(cs) for cid, cs in by_company.items()}


def pick_primary(contacts: list[Contact]) -> Contact | None:
    """The one contact that represents the company (see ``_rank``)."""
    return contacts[0] if contacts else None


async def resolve_primary_contacts(
    db: AsyncSession, company_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Contact]:
    """The representative contact per company, for list-shaped views."""
    by_company = await load_company_contacts(db, company_ids)
    resolved = {}
    for company_id, contacts in by_company.items():
        primary = pick_primary(contacts)
        if primary is not None:
            resolved[company_id] = primary
    return resolved


def display_name(contact: Contact) -> str | None:
    """"First Last" when we have it — enrichment often finds only an address."""
    name = " ".join(p for p in (contact.first_name, contact.last_name) if p).strip()
    return name or None
