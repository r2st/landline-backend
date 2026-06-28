"""Tests for the industry-vertical framework: registry copy quality, routing,
idempotent seeding, and the /sequences industry API."""

import re
import uuid as _uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.email_compose import render
from app.services import industry_verticals as iv

CTX = {"first_name": "Raj", "company_name": "OneNet Plastics",
       "title": "VP Operations", "demo_link": "https://demo.pages.dev"}

SPAM_WORDS = ["act now", "limited time", "buy now", "click here", "winner",
              "congratulations", "100% free", "risk-free", "guarantee", "urgent"]


def _strip(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


@pytest.fixture(autouse=True)
def _no_celery_dispatch(monkeypatch):
    """Keep enroll endpoints from hitting a real broker."""
    import app.tasks.outreach as outreach_tasks
    monkeypatch.setattr(outreach_tasks.send_sequence_step, "delay", lambda *a, **k: None)


# ── Registry shape ──


class TestRegistryShape:
    def test_has_at_least_four_verticals(self):
        assert len(iv.VERTICALS) >= 4

    def test_includes_required_industries(self):
        keys = {v.key for v in iv.VERTICALS}
        assert {"injection_molding", "cnc_machining", "metal_stamping", "food_beverage"} <= keys

    def test_keys_unique(self):
        keys = [v.key for v in iv.VERTICALS]
        assert len(keys) == len(set(keys))

    def test_sequence_names_unique(self):
        names = [v.sequence_name for v in iv.VERTICALS]
        assert len(names) == len(set(names))

    def test_molding_is_flagship_default(self):
        assert iv.DEFAULT_VERTICAL.key == "injection_molding"
        assert iv.VERTICALS[0].key == "injection_molding"

    def test_demo_links_valid(self):
        for v in iv.VERTICALS:
            assert v.demo_link.startswith("https://")
            assert ".pages.dev" in v.demo_link

    def test_each_has_pain_points_and_keywords(self):
        for v in iv.VERTICALS:
            assert len(v.pain_points) >= 4
            assert len(v.keywords) >= 3


# ── Copy quality (applied to EVERY vertical, same gates as molding) ──


class TestEveryVerticalCopy:
    def test_four_steps_non_decreasing_delays(self):
        for v in iv.VERTICALS:
            assert len(v.steps) == 4
            delays = [s[1] for s in v.steps]
            assert delays == sorted(delays)
            assert [s[0] for s in v.steps] == [1, 2, 3, 4]

    def test_subjects_short_and_lowercase(self):
        for v in iv.VERTICALS:
            for _, _, subj, _ in v.steps:
                assert len(render(subj, CTX)) < 60, f"{v.key}: {subj}"
                cleaned = subj.replace("{{first_name}}", "").replace("{{company_name}}", "")
                first = next((c for c in cleaned if c.isalpha()), None)
                if first:
                    assert first.islower() or first == "I", f"{v.key}: {subj}"

    def test_bodies_start_with_greeting_and_are_short(self):
        for v in iv.VERTICALS:
            for _, _, _, body in v.steps:
                rendered = render(body, CTX)
                assert rendered.startswith("<p>Hi Raj,</p>"), v.key
                assert len(_strip(rendered).split()) < 200, v.key

    def test_no_spam_no_caps_no_bang(self):
        for v in iv.VERTICALS:
            for _, _, subj, body in v.steps:
                text = render(subj + " " + body, CTX)
                assert "!" not in text, v.key
                for w in SPAM_WORDS:
                    assert w not in text.lower(), f"{v.key}: {w}"
                caps = [w for w in _strip(text).split() if len(w) > 3 and w.isupper() and w.isalpha()]
                assert not caps, f"{v.key}: {caps}"

    def test_no_images_or_raw_urls(self):
        for v in iv.VERTICALS:
            for _, _, _, body in v.steps:
                assert "<img" not in body.lower(), v.key
                plain = _strip(render(body, CTX))
                assert not re.search(r"https?://\S+", plain), f"{v.key} raw url"

    def test_links_use_demo_link_merge_field(self):
        for v in iv.VERTICALS:
            assert 'href="https://demo.pages.dev"' in render(v.steps[0][3], CTX), v.key

    def test_step4_offers_free_pilot_with_reply_cta(self):
        for v in iv.VERTICALS:
            body = render(v.steps[3][3], CTX).lower()
            assert "free" in body and "pilot" in body and "reply" in body, v.key

    def test_emotional_arc_present(self):
        for v in iv.VERTICALS:
            s2 = render(v.steps[1][3], CTX)
            s4 = render(v.steps[3][3], CTX).lower()
            assert "$" in s2, f"{v.key} step2 needs a cost figure"
            assert "last" in s4 or "stop after this" in s4, f"{v.key} step4 breakup"

    def test_no_fabricated_testimonials(self):
        for v in iv.VERTICALS:
            text = " ".join(render(b, CTX) for _, _, _, b in v.steps).lower()
            assert "told me:" not in text, v.key
            assert "a shop in michigan" not in text, v.key


# ── Routing ──


class TestRouting:
    @pytest.mark.parametrize("industry,company,expected", [
        ("Plastic Injection Molding", "Apex Mold", "injection_molding"),
        ("CNC Machining", "Precision Parts", "cnc_machining"),
        ("Metal Stamping", "Valley Press", "metal_stamping"),
        ("Progressive Die Stamping", "X", "metal_stamping"),
        ("Dairy Processing", "FreshMilk", "food_beverage"),
        ("Craft Brewery", "Hops Inc", "food_beverage"),
    ])
    def test_resolves_known_industries(self, industry, company, expected):
        v = iv.resolve_vertical(industry, company)
        assert v is not None and v.key == expected

    def test_unknown_returns_none(self):
        assert iv.resolve_vertical("Artisanal Candles", "Serenity Scents") is None

    def test_default_fallback(self):
        assert iv.resolve_vertical_or_default("Artisanal Candles", "X").key == "injection_molding"

    def test_specific_keyword_beats_generic(self):
        # "injection molding" (specific) should win even though "molding" also matches.
        v = iv.resolve_vertical("Plastic Injection Molding", "")
        assert v.key == "injection_molding"

    def test_get_vertical(self):
        assert iv.get_vertical("cnc_machining").name == "CNC Machining"
        assert iv.get_vertical("nope") is None


# ── Idempotent seeding (raw sqlite, mirrors the migration) ──


class TestSeedIdempotent:
    def _make_db(self):
        import sqlalchemy as sa
        eng = sa.create_engine("sqlite://")
        with eng.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE outreach_sequences (id TEXT PRIMARY KEY, name TEXT, "
                "description TEXT, industry TEXT, is_active BOOLEAN, status TEXT)"
            )
            conn.exec_driver_sql(
                "CREATE TABLE sequence_steps (id TEXT PRIMARY KEY, sequence_id TEXT, "
                "step_number INTEGER, delay_days INTEGER, subject_template TEXT, "
                "body_template TEXT)"
            )
        return eng

    def test_seed_creates_all_then_is_idempotent(self):
        import sqlalchemy as sa
        eng = self._make_db()
        with eng.begin() as conn:
            first = iv.seed_all_verticals(conn)
        assert len(first) == len(iv.VERTICALS)

        with eng.begin() as conn:
            seqs = conn.exec_driver_sql("SELECT count(*) FROM outreach_sequences").scalar()
            steps = conn.exec_driver_sql("SELECT count(*) FROM sequence_steps").scalar()
        assert seqs == len(iv.VERTICALS)
        assert steps == len(iv.VERTICALS) * 4

        # Run again — no duplicates.
        with eng.begin() as conn:
            iv.seed_all_verticals(conn)
            seqs2 = conn.exec_driver_sql("SELECT count(*) FROM outreach_sequences").scalar()
            steps2 = conn.exec_driver_sql("SELECT count(*) FROM sequence_steps").scalar()
        assert seqs2 == seqs
        assert steps2 == steps

    def test_seed_tags_industry(self):
        eng = self._make_db()
        with eng.begin() as conn:
            iv.seed_all_verticals(conn)
            rows = conn.exec_driver_sql(
                "SELECT industry FROM outreach_sequences ORDER BY industry"
            ).fetchall()
        industries = {r[0] for r in rows}
        assert {"injection_molding", "cnc_machining", "metal_stamping", "food_beverage"} <= industries


# ── API ──


async def _seed_verticals_async(db: AsyncSession):
    """Seed all verticals via the ORM (test DB is built from models, not alembic)."""
    for v in iv.VERTICALS:
        seq = OutreachSequence(
            id=_uuid.uuid4(), name=v.sequence_name, description=v.description,
            industry=v.key, is_active=True, status="active",
        )
        db.add(seq)
        await db.flush()
        for n, d, subj, body in v.steps:
            db.add(SequenceStep(
                id=_uuid.uuid4(), sequence_id=seq.id, step_number=n,
                delay_days=d, subject_template=subj, body_template=body,
            ))
    await db.commit()


class TestIndustryAPI:
    @pytest.mark.asyncio
    async def test_list_industries(self, client: AsyncClient, db_session: AsyncSession):
        await _seed_verticals_async(db_session)
        resp = await client.get("/api/v1/sequences/industries")
        assert resp.status_code == 200
        data = resp.json()
        keys = {d["key"] for d in data}
        assert {"injection_molding", "cnc_machining", "metal_stamping", "food_beverage"} <= keys
        molding = next(d for d in data if d["key"] == "injection_molding")
        assert molding["sequence_id"] is not None
        assert molding["step_count"] == 4
        assert len(molding["pain_points"]) >= 4
        assert "true-molding" in molding["demo_link"]

    @pytest.mark.asyncio
    async def test_suggest_industry_by_text(self, client: AsyncClient, db_session: AsyncSession):
        await _seed_verticals_async(db_session)
        resp = await client.get("/api/v1/sequences/suggest-industry", params={"industry": "CNC Machining"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["matched"] is True
        assert body["industry"] == "cnc_machining"
        assert body["sequence_id"] is not None

    @pytest.mark.asyncio
    async def test_suggest_industry_by_company(
        self, client: AsyncClient, db_session: AsyncSession, sample_company: Company
    ):
        await _seed_verticals_async(db_session)
        resp = await client.get(
            "/api/v1/sequences/suggest-industry", params={"company_id": str(sample_company.id)}
        )
        assert resp.status_code == 200
        # sample_company.industry_segment == "Metal Stamping"
        assert resp.json()["industry"] == "metal_stamping"

    @pytest.mark.asyncio
    async def test_suggest_industry_no_match(self, client: AsyncClient, db_session: AsyncSession):
        await _seed_verticals_async(db_session)
        resp = await client.get("/api/v1/sequences/suggest-industry", params={"industry": "Artisanal Candles"})
        assert resp.status_code == 200
        assert resp.json()["matched"] is False

    @pytest.mark.asyncio
    async def test_enroll_by_industry(
        self, client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
    ):
        await _seed_verticals_async(db_session)
        resp = await client.post(
            "/api/v1/sequences/enroll-by-industry",
            json={"industry": "metal_stamping", "contact_ids": [str(sample_contact.id)]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["enrolled_count"] == 1
        assert str(sample_contact.id) in body["enrolled"]

    @pytest.mark.asyncio
    async def test_enroll_by_unknown_industry_404(
        self, client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
    ):
        await _seed_verticals_async(db_session)
        resp = await client.post(
            "/api/v1/sequences/enroll-by-industry",
            json={"industry": "underwater_basket_weaving", "contact_ids": [str(sample_contact.id)]},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_sequences_includes_industry(self, client: AsyncClient, db_session: AsyncSession):
        await _seed_verticals_async(db_session)
        resp = await client.get("/api/v1/sequences")
        assert resp.status_code == 200
        industries = {s.get("industry") for s in resp.json()}
        assert "cnc_machining" in industries
