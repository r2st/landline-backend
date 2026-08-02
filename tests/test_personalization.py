"""Prospect personalization context — the facts handed to the copywriter.

The properties that matter:
  * every source is optional and one failing never costs the send;
  * only facts a row actually holds reach the prompt;
  * what was used is recorded, so a send is explainable after the fact.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.company_news import CompanyNews
from app.models.intent_signal import IntentSignal
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services import personalization


# ─── Pure helpers ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "count,expected_fragment",
    [
        (None, None),
        (0, None),
        (4, "very small"),
        (30, "small shop"),
        (150, "mid-size"),
        (600, "large"),
        (5000, "enterprise"),
    ],
)
def test_size_band(count, expected_fragment):
    band = personalization.size_band(count)
    if expected_fragment is None:
        assert band is None
    else:
        assert expected_fragment in band


@pytest.mark.parametrize(
    "title,expected",
    [
        (None, None),
        ("", None),
        ("Owner", "owner"),
        ("Founder & CEO", "owner"),
        ("VP of Operations", "executive"),
        ("Director of Quality", "executive"),
        ("Production Manager", "manager"),
        ("Shop Foreman", "manager"),
        ("Machinist", "individual contributor"),
    ],
)
def test_seniority_of(title, expected):
    assert personalization.seniority_of(title) == expected


def test_empty_context_renders_nothing():
    """No facts → no prompt block. An empty header would tell the model we know
    things about a prospect we know nothing about."""
    ctx = personalization.PersonalizationContext()
    assert ctx.as_prompt_block() == ""
    assert ctx.depth == 0


def test_prompt_block_forbids_extrapolation():
    """The anti-invention instruction is load-bearing, not decoration — a model
    handed a fact will happily write the plausible sentence next to it."""
    ctx = personalization.PersonalizationContext()
    ctx.add("company_news", "Recent company news: opened a second plant")
    block = ctx.as_prompt_block()
    assert "opened a second plant" in block
    assert "do NOT extrapolate" in block.lower() or "not extrapolate" in block.lower()


def test_add_ignores_blank_and_dedupes_points():
    ctx = personalization.PersonalizationContext()
    ctx.add("a", "  ")
    assert ctx.depth == 0
    ctx.add("a", "first")
    ctx.add("a", "second")
    assert ctx.depth == 1
    assert len(ctx.lines) == 2


# ─── Assembly against the database ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_firmographics_need_no_queries(db_session, sample_company, sample_contact):
    ctx = await personalization.build_context(
        db_session, contact=sample_contact, company=sample_company
    )
    # sample_company: 150 employees, Cleveland OH, "Forging and Stamping";
    # sample_contact: VP Manufacturing.
    assert "company_size" in ctx.points
    assert "location" in ctx.points
    assert "naics" in ctx.points
    assert "seniority" in ctx.points
    assert any("Cleveland" in line for line in ctx.lines)
    assert any("executive" in line for line in ctx.lines)


@pytest.mark.asyncio
async def test_actionable_news_reaches_the_prompt(db_session, sample_company, sample_contact):
    db_session.add(CompanyNews(
        id=uuid.uuid4(),
        company_id=sample_company.id,
        title="Acme Stamping opens second Ohio plant",
        summary="A 40,000 sq ft expansion adding two press lines.",
        category="expansion",
        relevance_score=9,
        is_actionable=True,
        published_at=datetime.now(timezone.utc) - timedelta(days=5),
    ))
    await db_session.flush()

    ctx = await personalization.build_context(
        db_session, contact=sample_contact, company=sample_company
    )
    assert "company_news" in ctx.points
    assert any("second Ohio plant" in line for line in ctx.lines)


@pytest.mark.asyncio
async def test_stale_and_non_actionable_news_is_ignored(
    db_session, sample_company, sample_contact
):
    """"Congrats on the news" about a year-old press release reads as someone who
    just found Google, not someone who follows the industry."""
    db_session.add_all([
        CompanyNews(
            id=uuid.uuid4(), company_id=sample_company.id,
            title="Ancient history", is_actionable=True,
            published_at=datetime.now(timezone.utc) - timedelta(days=400),
        ),
        CompanyNews(
            id=uuid.uuid4(), company_id=sample_company.id,
            title="Routine filing", is_actionable=False,
            published_at=datetime.now(timezone.utc),
        ),
    ])
    await db_session.flush()

    ctx = await personalization.build_context(
        db_session, contact=sample_contact, company=sample_company
    )
    assert "company_news" not in ctx.points


@pytest.mark.asyncio
async def test_live_intent_signal_and_angle(db_session, sample_company, sample_contact):
    db_session.add(IntentSignal(
        id=uuid.uuid4(),
        company_id=sample_company.id,
        signal_type="job_posting",
        title="Hiring 3 CNC machinists",
        description="Three open req's on the careers page.",
        relevance_score=80,
        urgency_score=70,
        outreach_angle="Scaling headcount usually means quoting load is the bottleneck.",
        expires_at=datetime.now(timezone.utc) + timedelta(days=10),
    ))
    await db_session.flush()

    ctx = await personalization.build_context(
        db_session, contact=sample_contact, company=sample_company
    )
    assert "intent_signal" in ctx.points
    assert "intent_angle" in ctx.points


@pytest.mark.asyncio
async def test_expired_intent_signal_is_ignored(db_session, sample_company, sample_contact):
    db_session.add(IntentSignal(
        id=uuid.uuid4(), company_id=sample_company.id, signal_type="funding",
        title="Raised a round", relevance_score=90, urgency_score=90,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    await db_session.flush()

    ctx = await personalization.build_context(
        db_session, contact=sample_contact, company=sample_company
    )
    assert "intent_signal" not in ctx.points


@pytest.mark.asyncio
async def test_prior_touches_and_click_engagement(db_session, sample_company, sample_contact):
    """A follow-up must know what we already said, and that they clicked."""
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db_session.add(seq)
    await db_session.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=3, status="active",
    )
    db_session.add(enr)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    db_session.add_all([
        OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enr.id, step_number=1, event_type="sent",
            occurred_at=now - timedelta(days=6),
            event_metadata={"subject": "Your quote turnaround"},
        ),
        OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enr.id, step_number=2, event_type="sent",
            occurred_at=now - timedelta(days=3),
            event_metadata={"subject": "The scrap math"},
        ),
        OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enr.id, step_number=2, event_type="clicked",
            occurred_at=now - timedelta(days=2),
        ),
    ])
    await db_session.flush()

    ctx = await personalization.build_context(
        db_session, contact=sample_contact, company=sample_company,
        enrollment=enr, current_step=3,
    )
    assert "prior_touches" in ctx.points
    assert "engagement" in ctx.points
    joined = " ".join(ctx.lines)
    assert "scrap math" in joined
    assert "clicked" in joined.lower()


@pytest.mark.asyncio
async def test_current_step_events_are_not_prior_touches(
    db_session, sample_company, sample_contact
):
    """A re-dispatch logs against the step being sent; counting it would tell the
    model it has already sent the email it is writing."""
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db_session.add(seq)
    await db_session.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=2, status="active",
    )
    db_session.add(enr)
    await db_session.flush()
    db_session.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=2, event_type="sent",
        occurred_at=datetime.now(timezone.utc), event_metadata={"subject": "This one"},
    ))
    await db_session.flush()

    ctx = await personalization.build_context(
        db_session, contact=sample_contact, company=sample_company,
        enrollment=enr, current_step=2,
    )
    assert "prior_touches" not in ctx.points


@pytest.mark.asyncio
async def test_first_touch_skips_engagement_lookup(db_session, sample_company, sample_contact):
    ctx = await personalization.build_context(
        db_session, contact=sample_contact, company=sample_company, current_step=1
    )
    assert "prior_touches" not in ctx.points
    assert "engagement" not in ctx.points


@pytest.mark.asyncio
async def test_a_broken_source_costs_a_paragraph_not_the_send(
    db_session, sample_company, sample_contact, monkeypatch
):
    """Rule 2 of the module: every source fails independently."""
    async def boom(*a, **k):
        raise RuntimeError("news table is on fire")

    monkeypatch.setattr(personalization, "_add_news", boom)

    ctx = await personalization.build_context(
        db_session, contact=sample_contact, company=sample_company
    )
    # Firmographics still made it; nothing raised.
    assert "company_size" in ctx.points


@pytest.mark.asyncio
async def test_metadata_is_the_audit_trail(db_session, sample_company, sample_contact):
    ctx = await personalization.build_context(
        db_session, contact=sample_contact, company=sample_company
    )
    meta = ctx.as_metadata()
    assert meta["depth"] == len(meta["points"]) == ctx.depth
    assert isinstance(meta["points"], list)


# ─── Wiring into composition ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_step_passes_personalization_to_the_generator(
    db_session, sample_company, sample_contact, monkeypatch
):
    """The whole point: the facts have to reach the prompt, and the record of
    which facts has to reach the caller."""
    from app.config import get_settings
    from app.services.ai_compose import compose_step

    settings = get_settings()
    monkeypatch.setattr(settings, "ai_compose_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_compose_review_batch_size", 0)  # skip the review gate

    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db_session.add(seq)
    await db_session.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
        subject_template="S", body_template="B",
    )
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db_session.add_all([step, enr])
    db_session.add(CompanyNews(
        id=uuid.uuid4(), company_id=sample_company.id,
        title="Acme adds a fourth press line", is_actionable=True,
        relevance_score=8, published_at=datetime.now(timezone.utc),
    ))
    await db_session.flush()

    captured: dict = {}

    async def fake_generate(lead_data, template_type="first_touch", custom_instructions=None,
                            research_context="", tone=None):
        captured["research_context"] = research_context
        return {"subject": "Fourth press line", "body": "<p>Noticed.</p>", "ai_generated": True}

    import app.services.ai_email_generator as gen
    monkeypatch.setattr(gen, "generate_email", fake_generate)

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, "S", "B"
    )

    assert result.action == "send"
    assert result.ai_generated is True
    assert "fourth press line" in captured["research_context"].lower()
    assert result.personalization is not None
    assert "company_news" in result.personalization["points"]
    assert result.personalization["depth"] >= 1


@pytest.mark.asyncio
async def test_static_fallback_reports_no_personalization(
    db_session, sample_company, sample_contact, monkeypatch
):
    """"AI wrote it from nothing" and "AI never ran" must stay distinguishable."""
    from app.config import get_settings
    from app.services.ai_compose import compose_step

    monkeypatch.setattr(get_settings(), "ai_compose_enabled", False)

    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db_session.add(seq)
    await db_session.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
        subject_template="S", body_template="B",
    )
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db_session.add_all([step, enr])
    await db_session.flush()

    result = await compose_step(
        db_session, enr, sample_contact, sample_company, step, "S", "B"
    )
    assert result.action == "send"
    assert result.personalization is None
