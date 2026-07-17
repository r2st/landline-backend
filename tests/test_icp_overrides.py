"""Human-pinned ICP fields, and the weekly re-learn that must not undo them.

The ICP is refit from won deals every Monday 03:30 UTC, and `learn_icp`
deactivates the active profile and inserts a new one. That is right for a
profile nobody has touched. It is wrong the moment an operator tunes it: without
the override carry-forward their weights would revert on a schedule, silently,
with the agent quietly going back to prospecting the wrong companies.

So the load-bearing test here is `test_relearn_preserves_pinned_fields` — it is
the entire reason the mechanism exists.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.deal import Deal
from app.models.prospecting import IcpProfile
from app.services.autonomous_prospector import (
    OVERRIDABLE_ICP_FIELDS,
    apply_manual_overrides,
    get_prospector,
)

PINNED_SIZE = {"min": 5, "max": 40, "ideal": 12}


async def _won_deal_company(db: AsyncSession, name: str, industry: str, employees: int):
    """A won deal the learner will fit on, so re-learn has real input."""
    company = Company(
        name=name, country="US", source="test",
        industry_segment=industry, employee_count=employees,
    )
    db.add(company)
    await db.flush()
    db.add(Deal(company_id=company.id, title=f"{name} deal", status="won", value=10000))
    await db.flush()
    return company


# --- API ---


@pytest.mark.asyncio
async def test_put_icp_pins_a_field(client: AsyncClient):
    resp = await client.put("/api/v1/prospecting/icp", json={"size_range": PINNED_SIZE})
    assert resp.status_code == 200
    body = resp.json()

    assert body["size_range"] == PINNED_SIZE
    # Pinned means recorded as human-owned, not merely written.
    assert body["manual_overrides"]["size_range"] == PINNED_SIZE
    assert body["edited_by"] == "_default_test_admin@landline.app"
    assert body["edited_at"] is not None


@pytest.mark.asyncio
async def test_put_icp_leaves_unsent_fields_learned(client: AsyncClient):
    # A first PUT fits the profile; GET is null before one exists.
    before = (await client.put("/api/v1/prospecting/icp", json={})).json()
    body = (await client.put("/api/v1/prospecting/icp", json={"size_range": PINNED_SIZE})).json()

    # Only what you sent is pinned; everything else keeps tracking won deals.
    assert set(body["manual_overrides"]) == {"size_range"}
    assert body["industry_weights"] == before["industry_weights"]


@pytest.mark.asyncio
async def test_empty_put_pins_nothing_and_claims_no_edit(client: AsyncClient):
    """An empty save must not brand the whole profile hand-tuned."""
    body = (await client.put("/api/v1/prospecting/icp", json={})).json()
    assert body["manual_overrides"] == {}
    assert body["edited_at"] is None
    assert body["edited_by"] is None


@pytest.mark.asyncio
async def test_put_icp_creates_a_profile_when_none_exists(client: AsyncClient, db_session: AsyncSession):
    existing = (await db_session.execute(select(IcpProfile))).scalars().all()
    assert existing == []

    resp = await client.put("/api/v1/prospecting/icp", json={"size_range": PINNED_SIZE})
    # A fresh install has nothing to edit; 404ing would make the UI's first save
    # fail for no reason the operator could act on.
    assert resp.status_code == 200
    assert resp.json()["size_range"] == PINNED_SIZE


@pytest.mark.asyncio
async def test_put_icp_updates_in_place_rather_than_stacking_profiles(
    client: AsyncClient, db_session: AsyncSession
):
    await client.put("/api/v1/prospecting/icp", json={"size_range": PINNED_SIZE})
    await client.put("/api/v1/prospecting/icp", json={"technology_signals": ["legacy ERP"]})

    active = (
        await db_session.execute(select(IcpProfile).where(IcpProfile.is_active == True))  # noqa: E712
    ).scalars().all()
    # An edit is a correction to the current ICP, not a new hypothesis — a
    # profile per edit would make learned_from_deal_count meaningless.
    assert len(active) == 1
    assert set(active[0].manual_overrides) == {"size_range", "technology_signals"}


@pytest.mark.asyncio
async def test_unpin_releases_a_field_back_to_the_learner(client: AsyncClient):
    await client.put("/api/v1/prospecting/icp", json={"size_range": PINNED_SIZE})
    resp = await client.put("/api/v1/prospecting/icp", json={"unpin": ["size_range"]})

    assert resp.status_code == 200
    assert resp.json()["manual_overrides"] == {}


@pytest.mark.asyncio
async def test_unpin_rejects_an_unknown_field(client: AsyncClient):
    resp = await client.put("/api/v1/prospecting/icp", json={"unpin": ["not_a_field"]})
    # Accepting it would leave a no-op sitting in the JSON forever.
    assert resp.status_code == 400
    assert "not_a_field" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_icp_exposes_what_is_pinned(client: AsyncClient):
    await client.put("/api/v1/prospecting/icp", json={"size_range": PINNED_SIZE})
    body = (await client.get("/api/v1/prospecting/icp")).json()

    # The UI can't answer "will Monday undo this?" without these.
    assert body["manual_overrides"]["size_range"] == PINNED_SIZE
    assert body["edited_by"] == "_default_test_admin@landline.app"


# --- The regression this exists to prevent ---


@pytest.mark.asyncio
async def test_relearn_preserves_pinned_fields(client: AsyncClient, db_session: AsyncSession):
    """A hand-tuned ICP must survive the Monday 03:30 UTC re-learn.

    Without the carry-forward, learn_icp deactivates the edited profile and
    inserts a freshly-fit one — reverting the operator's weights weekly, with no
    error and nothing on screen to explain why targeting drifted back.
    """
    await _won_deal_company(db_session, "BigCo", "Aerospace", 900)
    await db_session.commit()

    await client.put("/api/v1/prospecting/icp", json={"size_range": PINNED_SIZE})

    relearned = (await client.post("/api/v1/prospecting/icp/learn")).json()

    # The pin survived, on a brand-new profile row.
    assert relearned["size_range"] == PINNED_SIZE
    assert relearned["manual_overrides"]["size_range"] == PINNED_SIZE
    assert relearned["edited_by"] == "_default_test_admin@landline.app"


@pytest.mark.asyncio
async def test_relearn_still_refits_everything_not_pinned(
    client: AsyncClient, db_session: AsyncSession
):
    """Pinning one field must not freeze the rest.

    The point of re-learning is that the profile tracks what actually closes;
    an override that quietly froze the whole ICP would defeat it.
    """
    await client.put("/api/v1/prospecting/icp", json={"size_range": PINNED_SIZE})

    await _won_deal_company(db_session, "Aero One", "Aerospace", 800)
    await _won_deal_company(db_session, "Aero Two", "Aerospace", 700)
    await db_session.commit()

    relearned = (await client.post("/api/v1/prospecting/icp/learn")).json()

    assert relearned["size_range"] == PINNED_SIZE  # pinned: unchanged
    assert relearned["learned_from_deal_count"] == 2  # unpinned: refit
    assert "Aerospace" in relearned["industry_weights"]


@pytest.mark.asyncio
async def test_relearn_after_unpin_takes_the_learned_value(
    client: AsyncClient, db_session: AsyncSession
):
    await _won_deal_company(db_session, "Aero One", "Aerospace", 800)
    await db_session.commit()

    await client.put("/api/v1/prospecting/icp", json={"size_range": PINNED_SIZE})
    await client.put("/api/v1/prospecting/icp", json={"unpin": ["size_range"]})
    relearned = (await client.post("/api/v1/prospecting/icp/learn")).json()

    # Released, so the fit owns it again.
    assert relearned["manual_overrides"] == {}
    assert relearned["size_range"] != PINNED_SIZE


@pytest.mark.asyncio
async def test_the_agent_scores_on_the_pinned_value(client: AsyncClient, db_session: AsyncSession):
    """A pin that the scorer ignores is decoration.

    exclusion_criteria is the sharpest case: pinning a hard disqualifier has to
    actually disqualify, or the UI is offering a control that does nothing.
    """
    await client.put(
        "/api/v1/prospecting/icp",
        json={"exclusion_criteria": {"industries": ["Staffing"], "countries": [], "max_employees": None}},
    )
    await db_session.commit()

    prospector = get_prospector()
    icp = await prospector.get_active_icp(db_session)
    result = prospector.qualify(
        {"name": "Temp Agency", "industry": "Staffing", "employee_count": 50, "country": "US"}, icp
    )

    assert result["excluded"] is True
    assert result["fit_score"] == 0
    assert "Staffing" in result["reason"]


# --- The override helper ---


def test_apply_manual_overrides_ignores_an_unknown_field():
    """A renamed field must not sit in the JSON applying to nothing.

    Silently-inert config is the same failure as the review queue reading fields
    the API never sent: everything looks fine and nothing works.
    """
    profile = IcpProfile(
        id=uuid.uuid4(),
        name="t",
        industry_weights={"CNC": 0.5},
        size_range={"min": 1, "max": 2},
        location_preferences={},
        technology_signals=[],
        pain_point_indicators=[],
        exclusion_criteria={},
        manual_overrides={"size_range": PINNED_SIZE, "renamed_away": {"x": 1}},
    )
    apply_manual_overrides(profile)

    assert profile.size_range == PINNED_SIZE  # the real one still applies
    assert not hasattr(profile, "renamed_away")


def test_overridable_fields_are_scoring_inputs_only():
    """Provenance is not an operator's to set.

    learned_from_deal_count / model_used / edited_* describe how the profile was
    produced. Letting a pin rewrite those would let the UI claim a hand-tuned
    profile was fit on 40 won deals.
    """
    assert "learned_from_deal_count" not in OVERRIDABLE_ICP_FIELDS
    assert "model_used" not in OVERRIDABLE_ICP_FIELDS
    assert "edited_by" not in OVERRIDABLE_ICP_FIELDS
    assert "is_active" not in OVERRIDABLE_ICP_FIELDS
