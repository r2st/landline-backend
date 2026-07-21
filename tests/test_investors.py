"""Tests for the investor-outreach system: investor CRUD + search/filter,
interactions, documents, pipeline funnel, timeline, and AI outreach generation."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.models.investor import Investor
from app.services import ai_investor_outreach


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _make_investor(client: AsyncClient, **kw):
    payload = {"name": "Jane Capital"}
    payload.update(kw)
    return await client.post("/api/v1/investors", json=payload)


# --------------------------------------------------------------------------- #
# Investor CRUD
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_investor(client: AsyncClient):
    resp = await _make_investor(
        client,
        firm="Acme Ventures",
        email="jane@acme.vc",
        investment_focus=["manufacturing", "b2b_saas"],
        investment_stage="seed",
        typical_check_size_min=5_000_000_00,   # ₹50L in paise
        typical_check_size_max=20_000_000_00,  # ₹2Cr in paise
        location="Bangalore",
        status="researching",
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "Jane Capital"
    assert data["firm"] == "Acme Ventures"
    assert data["investment_focus"] == ["manufacturing", "b2b_saas"]
    assert data["investment_stage"] == "seed"
    assert data["typical_check_size_max"] == 20_000_000_00
    assert data["status"] == "researching"


@pytest.mark.asyncio
async def test_create_investor_invalid_status(client: AsyncClient):
    resp = await _make_investor(client, status="ghosted")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_investor_invalid_stage(client: AsyncClient):
    resp = await _make_investor(client, investment_stage="series-z")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_investor(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    resp = await client.get(f"/api/v1/investors/{iid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == iid


@pytest.mark.asyncio
async def test_get_unknown_investor(client: AsyncClient):
    assert (await client.get(f"/api/v1/investors/{_uuid.uuid4()}")).status_code == 404


@pytest.mark.asyncio
async def test_update_investor(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    resp = await client.put(
        f"/api/v1/investors/{iid}",
        json={"status": "interested", "notes": "Warm intro via Sam"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "interested"
    assert body["notes"] == "Warm intro via Sam"


@pytest.mark.asyncio
async def test_update_investor_invalid_status(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    resp = await client.put(f"/api/v1/investors/{iid}", json={"status": "nope"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_soft_delete_investor(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    assert (await client.delete(f"/api/v1/investors/{iid}")).status_code == 204
    # Gone from GET and from the list.
    assert (await client.get(f"/api/v1/investors/{iid}")).status_code == 404
    listed = await client.get("/api/v1/investors")
    assert all(i["id"] != iid for i in listed.json())


@pytest.mark.asyncio
async def test_delete_unknown_investor(client: AsyncClient):
    assert (await client.delete(f"/api/v1/investors/{_uuid.uuid4()}")).status_code == 404


# --------------------------------------------------------------------------- #
# List: search + filters
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_filter_by_status_and_stage(client: AsyncClient):
    await _make_investor(client, name="A", status="contacted", investment_stage="seed")
    await _make_investor(client, name="B", status="committed", investment_stage="series-a")

    by_status = await client.get("/api/v1/investors", params={"status": "committed"})
    assert [i["name"] for i in by_status.json()] == ["B"]

    by_stage = await client.get("/api/v1/investors", params={"stage": "seed"})
    assert [i["name"] for i in by_stage.json()] == ["A"]


@pytest.mark.asyncio
async def test_list_search(client: AsyncClient):
    await _make_investor(client, name="Ravi Founder", firm="Sequoia India")
    await _make_investor(client, name="Other Person", firm="Accel")
    resp = await client.get("/api/v1/investors", params={"search": "sequoia"})
    assert [i["name"] for i in resp.json()] == ["Ravi Founder"]


@pytest.mark.asyncio
async def test_list_filter_by_focus(client: AsyncClient):
    await _make_investor(client, name="MfgVC", investment_focus=["Manufacturing", "AI"])
    await _make_investor(client, name="FintechVC", investment_focus=["fintech"])
    resp = await client.get("/api/v1/investors", params={"focus": "manufacturing"})
    names = [i["name"] for i in resp.json()]
    assert names == ["MfgVC"]


@pytest.mark.asyncio
async def test_list_pagination(client: AsyncClient):
    for n in range(5):
        await _make_investor(client, name=f"Inv {n}")
    page1 = await client.get("/api/v1/investors", params={"page": 1, "page_size": 2})
    assert len(page1.json()) == 2


# --------------------------------------------------------------------------- #
# Pipeline funnel
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pipeline_funnel(client: AsyncClient):
    await _make_investor(client, name="A", status="researching")
    await _make_investor(client, name="B", status="committed",
                         typical_check_size_max=10_000_000_00)
    await _make_investor(client, name="C", status="committed",
                         typical_check_size_max=5_000_000_00)

    resp = await client.get("/api/v1/investors/pipeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    stages = {s["status"]: s for s in body["stages"]}
    # Every canonical status present.
    assert set(stages) == {
        "researching", "contacted", "interested", "meeting_scheduled",
        "due_diligence", "committed", "passed",
    }
    assert stages["committed"]["count"] == 2
    assert stages["committed"]["potential_paise"] == 15_000_000_00
    assert stages["contacted"]["count"] == 0


@pytest.mark.asyncio
async def test_pipeline_excludes_deleted(client: AsyncClient):
    iid = (await _make_investor(client, status="committed")).json()["id"]
    await client.delete(f"/api/v1/investors/{iid}")
    body = (await client.get("/api/v1/investors/pipeline")).json()
    assert body["total"] == 0


# --------------------------------------------------------------------------- #
# Interactions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_and_list_interactions(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    created = await client.post(
        f"/api/v1/investors/{iid}/interactions",
        json={"type": "meeting", "direction": "outbound",
              "subject": "Intro call", "outcome": "Positive",
              "next_steps": "Send deck"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["type"] == "meeting"

    listed = await client.get(f"/api/v1/investors/{iid}/interactions")
    assert listed.status_code == 200
    assert len(listed.json()) == 1


@pytest.mark.asyncio
async def test_outbound_interaction_stamps_last_contacted(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    await client.post(
        f"/api/v1/investors/{iid}/interactions",
        json={"type": "email", "direction": "outbound"},
    )
    inv = (await client.get(f"/api/v1/investors/{iid}")).json()
    assert inv["last_contacted_at"] is not None


@pytest.mark.asyncio
async def test_inbound_interaction_does_not_stamp_last_contacted(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    await client.post(
        f"/api/v1/investors/{iid}/interactions",
        json={"type": "email", "direction": "inbound"},
    )
    inv = (await client.get(f"/api/v1/investors/{iid}")).json()
    assert inv["last_contacted_at"] is None


@pytest.mark.asyncio
async def test_interaction_invalid_type(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    resp = await client.post(
        f"/api/v1/investors/{iid}/interactions", json={"type": "carrier_pigeon"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_interaction_on_unknown_investor(client: AsyncClient):
    resp = await client.post(
        f"/api/v1/investors/{_uuid.uuid4()}/interactions", json={"type": "call"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_and_delete_interaction(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    intid = (
        await client.post(
            f"/api/v1/investors/{iid}/interactions", json={"type": "call"}
        )
    ).json()["id"]

    upd = await client.put(
        f"/api/v1/investors/interactions/{intid}", json={"outcome": "Left voicemail"}
    )
    assert upd.status_code == 200
    assert upd.json()["outcome"] == "Left voicemail"

    assert (await client.delete(f"/api/v1/investors/interactions/{intid}")).status_code == 204
    assert (await client.put(
        f"/api/v1/investors/interactions/{intid}", json={"outcome": "x"}
    )).status_code == 404


@pytest.mark.asyncio
async def test_list_interactions_filter_by_direction(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    await client.post(f"/api/v1/investors/{iid}/interactions",
                      json={"type": "email", "direction": "outbound"})
    await client.post(f"/api/v1/investors/{iid}/interactions",
                      json={"type": "email", "direction": "inbound"})
    resp = await client.get(f"/api/v1/investors/{iid}/interactions",
                            params={"direction": "inbound"})
    assert len(resp.json()) == 1
    assert resp.json()[0]["direction"] == "inbound"


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_timeline_orders_recent_first(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    now = datetime.now(timezone.utc)
    # Insert two interactions with explicit dates via the API.
    await client.post(
        f"/api/v1/investors/{iid}/interactions",
        json={"type": "email", "subject": "older",
              "interaction_date": (now - timedelta(days=3)).isoformat()},
    )
    await client.post(
        f"/api/v1/investors/{iid}/interactions",
        json={"type": "call", "subject": "newer",
              "interaction_date": now.isoformat()},
    )
    resp = await client.get(f"/api/v1/investors/{iid}/timeline")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["investor"]["id"] == iid
    subjects = [e["subject"] for e in body["events"]]
    assert subjects == ["newer", "older"]


@pytest.mark.asyncio
async def test_timeline_unknown_investor(client: AsyncClient):
    assert (await client.get(f"/api/v1/investors/{_uuid.uuid4()}/timeline")).status_code == 404


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_document_for_investor(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    resp = await client.post(
        "/api/v1/investors/documents",
        json={"investor_id": iid, "type": "term_sheet",
              "title": "Draft term sheet", "content": "# Terms\n..."},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["type"] == "term_sheet"
    assert resp.json()["investor_id"] == iid


@pytest.mark.asyncio
async def test_create_general_document(client: AsyncClient):
    resp = await client.post(
        "/api/v1/investors/documents",
        json={"type": "pitch_deck", "title": "Master deck"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["investor_id"] is None
    assert resp.json()["version"] == 1


@pytest.mark.asyncio
async def test_document_invalid_type(client: AsyncClient):
    resp = await client.post(
        "/api/v1/investors/documents", json={"type": "napkin_sketch", "title": "x"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_document_for_unknown_investor(client: AsyncClient):
    resp = await client.post(
        "/api/v1/investors/documents",
        json={"investor_id": str(_uuid.uuid4()), "type": "other", "title": "x"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_documents_filters(client: AsyncClient):
    iid = (await _make_investor(client)).json()["id"]
    await client.post("/api/v1/investors/documents",
                      json={"investor_id": iid, "type": "cap_table", "title": "Cap"})
    await client.post("/api/v1/investors/documents",
                      json={"type": "pitch_deck", "title": "General deck"})

    by_investor = await client.get("/api/v1/investors/documents",
                                   params={"investor_id": iid})
    assert [d["title"] for d in by_investor.json()] == ["Cap"]

    general = await client.get("/api/v1/investors/documents",
                               params={"general_only": "true"})
    assert [d["title"] for d in general.json()] == ["General deck"]

    by_type = await client.get("/api/v1/investors/documents",
                               params={"type": "cap_table"})
    assert [d["title"] for d in by_type.json()] == ["Cap"]


@pytest.mark.asyncio
async def test_update_and_delete_document(client: AsyncClient):
    did = (
        await client.post("/api/v1/investors/documents",
                          json={"type": "pitch_deck", "title": "v1"})
    ).json()["id"]
    upd = await client.put(f"/api/v1/investors/documents/{did}",
                           json={"title": "v2", "version": 2})
    assert upd.status_code == 200
    assert upd.json()["title"] == "v2"
    assert upd.json()["version"] == 2

    assert (await client.delete(f"/api/v1/investors/documents/{did}")).status_code == 204
    assert (await client.get(f"/api/v1/investors/documents/{did}")).status_code == 404


# --------------------------------------------------------------------------- #
# AI outreach (no API key in test env → template fallback)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_outreach_falls_back_to_template(client: AsyncClient, monkeypatch):
    # Force the no-AI path deterministically: the test env may carry an
    # anthropic_api_key from .env, which would otherwise flip ai_available True
    # (and make a live API call). We're asserting the template-fallback shape.
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "")
    iid = (
        await _make_investor(client, name="Priya Angel",
                             investment_focus=["manufacturing"],
                             investment_stage="seed")
    ).json()["id"]
    resp = await client.post(f"/api/v1/investors/{iid}/outreach", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "template"
    assert body["ai_available"] is False
    assert body["subject"]
    assert "Priya" in body["body"]
    assert body["interaction_id"] is None


@pytest.mark.asyncio
async def test_outreach_logs_interaction(client: AsyncClient):
    iid = (await _make_investor(client, name="Sam VC")).json()["id"]
    resp = await client.post(
        f"/api/v1/investors/{iid}/outreach", json={"log_interaction": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["interaction_id"] is not None

    # The draft shows up on the timeline and stamps last_contacted_at.
    timeline = (await client.get(f"/api/v1/investors/{iid}/timeline")).json()
    assert len(timeline["events"]) == 1
    assert timeline["events"][0]["type"] == "email"
    inv = (await client.get(f"/api/v1/investors/{iid}")).json()
    assert inv["last_contacted_at"] is not None


@pytest.mark.asyncio
async def test_outreach_unknown_investor(client: AsyncClient):
    resp = await client.post(f"/api/v1/investors/{_uuid.uuid4()}/outreach", json={})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Service-level unit tests
# --------------------------------------------------------------------------- #


def test_format_check_size_ranges():
    f = ai_investor_outreach._format_check_size
    assert f(None, None) == "unspecified"
    # ₹2 Cr = 20,000,000 paise... 2 Cr rupees = 2e7 rupees = 2e9 paise
    assert "Cr" in f(None, 2_000_000_000_00)
    assert "L" in f(50_00_000_00, None)  # ₹50L


@pytest.mark.asyncio
async def test_generate_outreach_template_shape(monkeypatch):
    # Clear any env-provided key so we deterministically exercise the no-AI
    # (template) branch without a live API call. See _available().
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "")
    inv = Investor(
        id=_uuid.uuid4(), name="Deepa Rao", firm="North Ventures",
        investment_focus=["ai", "manufacturing"], investment_stage="seed",
        status="researching",
    )
    draft = await ai_investor_outreach.generate_outreach(inv)
    assert draft["source"] == "template"
    assert draft["ai_available"] is False
    assert "Deepa" in draft["body"]
    assert draft["subject"]
    assert isinstance(draft["generated_at"], datetime)
