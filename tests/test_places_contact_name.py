"""Keep the person Hunter names, not just the address.

Places yields businesses, never people, so Hunter's ``/domain-search`` is the
only hop in this pipeline that can attach a name to an address — and it returns
``first_name``/``last_name``/``position`` alongside every result, ranked
personal-first. ``extract_email`` read ``value`` and discarded the rest.

The cost was visible in production on 2026-07-24: 29 of 39 contacts had no first
name, so every cold email opened "Hi there," — against a copy prompt whose
central instruction is "write to ONE person, not a segment".

A scraped address still has no name, and that is correct: inventing one is worse
than a generic greeting.
"""

import uuid

import pytest

from app.models.discovered_business import DiscoveredBusiness

from app.services.places_discovery import PROVIDER, get_places_service


class _FakeHunter:
    """Stands in for the Hunter client. ``enabled`` mirrors a configured key."""

    def __init__(self, payload, enabled=True):
        self.payload = payload
        self.enabled = enabled

    async def domain_search(self, db, domain):
        return self.payload


def _business(**kw):
    defaults = dict(
        id=uuid.uuid4(),
        place_id=f"place-{uuid.uuid4()}",
        business_name="Acme Molding",
        domain="acmemolding.com",
        website="https://acmemolding.com",
        phone="+1 555 0100",
        city="Toledo",
        state="OH",
        country="US",
        status="new",
        discovered_via="google_places",
        search_query="plastic injection molding manufacturer",
    )
    defaults.update(kw)
    return DiscoveredBusiness(**defaults)


HUNTER_HIT = {
    "status": "ok",
    "email": "dave.kowalski@acmemolding.com",
    "score": 92,
    "emails": [
        {
            "email": "dave.kowalski@acmemolding.com",
            "type": "personal",
            "score": 92,
            "first_name": "Dave",
            "last_name": "Kowalski",
            "position": "Plant Manager",
        },
        {
            "email": "info@acmemolding.com",
            "type": "generic",
            "score": 97,
            "first_name": None,
            "last_name": None,
            "position": None,
        },
    ],
}


@pytest.mark.asyncio
async def test_hunter_name_is_persisted(db_session, monkeypatch):
    # ``extract_email`` imports the Hunter client inside the function body, so
    # the patch has to land on its defining module, not on places_discovery.
    import app.services.hunter_verification as hv

    monkeypatch.setattr(hv, "get_hunter_service", lambda: _FakeHunter(HUNTER_HIT))

    business = _business()
    db_session.add(business)
    await db_session.flush()

    result = await get_places_service().extract_email(db_session, business)

    assert result["status"] == "enriched"
    assert result["source"] == "hunter"
    assert business.contact_first_name == "Dave"
    assert business.contact_last_name == "Kowalski"
    assert business.contact_title == "Plant Manager"


@pytest.mark.asyncio
async def test_name_belongs_to_the_winning_address_not_the_first_listed(
    db_session, monkeypatch
):
    """Hunter ranks personal-first; the name must come off the chosen entry.

    Reading the name off the top of the *unranked* list would attach Dave's name
    to the generic ``info@`` inbox — a merge field that greets a shared mailbox
    by a stranger's first name.
    """
    reordered = {
        **HUNTER_HIT,
        "emails": list(reversed(HUNTER_HIT["emails"])),
    }
    import app.services.hunter_verification as hv

    monkeypatch.setattr(hv, "get_hunter_service", lambda: _FakeHunter(reordered))

    business = _business()
    db_session.add(business)
    await db_session.flush()

    await get_places_service().extract_email(db_session, business)

    assert business.email == "dave.kowalski@acmemolding.com"
    assert business.contact_first_name == "Dave"


@pytest.mark.asyncio
async def test_a_nameless_hunter_result_stays_nameless(db_session, monkeypatch):
    """Only a generic inbox was found. Do not invent a person for it."""
    generic_only = {
        "status": "ok",
        "email": "info@acmemolding.com",
        "score": 97,
        "emails": [
            {
                "email": "info@acmemolding.com",
                "type": "generic",
                "score": 97,
                "first_name": None,
                "last_name": None,
                "position": None,
            }
        ],
    }
    import app.services.hunter_verification as hv

    monkeypatch.setattr(hv, "get_hunter_service", lambda: _FakeHunter(generic_only))

    business = _business()
    db_session.add(business)
    await db_session.flush()

    await get_places_service().extract_email(db_session, business)

    assert business.email == "info@acmemolding.com"
    assert business.contact_first_name is None


# ---------------------------------------------------------------------------
# The name has to survive the hop into an actual Contact
# ---------------------------------------------------------------------------
def test_candidate_carries_the_name_through_to_the_contact():
    business = _business(
        email="dave.kowalski@acmemolding.com",
        email_source="hunter",
        contact_first_name="Dave",
        contact_last_name="Kowalski",
        contact_title="Plant Manager",
    )

    candidate = get_places_service().to_candidate(business)

    assert len(candidate.contacts) == 1
    contact = candidate.contacts[0]
    assert contact.first_name == "Dave"
    assert contact.last_name == "Kowalski"
    assert contact.title == "Plant Manager"
    assert contact.has_name is True
    assert contact.source == PROVIDER


def test_a_business_with_no_email_still_yields_no_contact():
    """Unchanged: Places gives no people, so a business with no address is nobody."""
    candidate = get_places_service().to_candidate(_business(email=None))
    assert candidate.contacts == []


def test_nameless_contact_is_honestly_nameless():
    business = _business(email="info@acmemolding.com", email_source="scrape")
    contact = get_places_service().to_candidate(business).contacts[0]
    assert contact.first_name is None
    assert contact.has_name is False
