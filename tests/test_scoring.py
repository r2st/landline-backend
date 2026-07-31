"""Tests for the lead scoring service."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.services.scoring import (
    _industry_score,
    _size_score,
    _location_score,
    _completeness_score,
    score_company_rule_based,
)


class TestIndustryScore:
    """Test NAICS-based industry scoring."""

    def test_manufacturing_naics_31(self):
        score, reason = _industry_score("3111")
        assert score == 40
        assert "Manufacturing" in reason

    def test_manufacturing_naics_32(self):
        score, reason = _industry_score("3261")
        assert score == 40

    def test_manufacturing_naics_33(self):
        score, reason = _industry_score("3321")
        assert score == 40
        assert "Manufacturing" in reason

    def test_related_wholesale(self):
        score, reason = _industry_score("4231")
        assert score == 20
        assert "Wholesale" in reason

    def test_unrelated_industry(self):
        score, reason = _industry_score("7211")
        assert score == 0

    def test_no_naics(self):
        score, reason = _industry_score(None)
        assert score == 0
        assert "No NAICS" in reason


class TestIndustrySegmentFallback:
    """No NAICS (the Places-sourced shape) falls back to the segment taxonomy."""

    def test_cnc_machining_segment_scores_full(self):
        score, reason = _industry_score(None, "CNC Machining")
        assert score == 40
        assert "CNC Machining" in reason

    def test_metal_stamping_segment_scores_full(self):
        score, _ = _industry_score(None, "Metal Stamping")
        assert score == 40

    def test_injection_molding_segment_scores_full(self):
        score, _ = _industry_score(None, "Plastic Injection Molding")
        assert score == 40

    def test_generic_manufacturing_segment_scores_full(self):
        score, _ = _industry_score(None, "Precision Sheet Metal Fabrication")
        assert score == 40

    def test_company_name_alone_can_match(self):
        score, _ = _industry_score(None, None, "Acme CNC Machining LLC")
        assert score == 40

    def test_unrelated_segment_scores_zero(self):
        score, reason = _industry_score(None, "Restaurants")
        assert score == 0
        assert "Restaurants" in reason

    def test_manufacturing_naics_still_wins(self):
        score, reason = _industry_score("3327", "Restaurants")
        assert score == 40
        assert "NAICS" in reason

    def test_unrelated_naics_defers_to_segment(self):
        # A wrong/unrelated NAICS shouldn't zero a company whose segment is
        # squarely in the ICP.
        score, _ = _industry_score("7211", "CNC Machining")
        assert score == 40

    def test_unrelated_naics_and_segment_keeps_naics_reason(self):
        score, reason = _industry_score("7211", "Hotels")
        assert score == 0
        assert "NAICS 7211" in reason

    def test_related_naics_still_scores_partial(self):
        score, _ = _industry_score("4231", "Hotels")
        assert score == 20


class TestSizeScore:
    """Test employee count scoring."""

    def test_sweet_spot_50_500(self):
        score, _ = _size_score(200)
        assert score == 25

    def test_large_500_2000(self):
        score, _ = _size_score(1000)
        assert score == 20

    def test_small_10_50(self):
        score, _ = _size_score(30)
        assert score == 15

    def test_enterprise_2000_plus(self):
        score, _ = _size_score(5000)
        assert score == 10

    def test_very_small(self):
        score, _ = _size_score(5)
        assert score == 5

    def test_unknown(self):
        score, _ = _size_score(None)
        assert score == 5


class TestLocationScore:
    """Test country-based location scoring."""

    def test_us(self):
        score, _ = _location_score("US")
        assert score == 20

    def test_canada(self):
        score, _ = _location_score("CA")
        assert score == 15

    def test_other(self):
        score, _ = _location_score("MX")
        assert score == 0


class TestCompletenessScore:
    """Test data completeness scoring."""

    def test_full_data(self, sample_company_obj, sample_contacts_list):
        score, breakdown = _completeness_score(sample_company_obj, sample_contacts_list)
        assert score == 15  # email(5) + phone(3) + website(3) + name(4)
        assert breakdown["email"] == 5
        assert breakdown["phone"] == 3
        assert breakdown["website"] == 3
        assert breakdown["contact_name"] == 4

    def test_no_contacts(self, sample_company_obj):
        score, breakdown = _completeness_score(sample_company_obj, [])
        # phone(3) + website(3) = 6
        assert score == 6

    def test_no_data(self):
        company = Company(
            name="Empty Co", source="test", country="US",
        )
        score, breakdown = _completeness_score(company, [])
        assert score == 0


@pytest.fixture
def sample_company_obj():
    """A company object (not persisted) for unit tests."""
    return Company(
        name="Test Mfg",
        domain="testmfg.com",
        country="US",
        employee_count=100,
        naics_code="3321",
        source="test",
        website="https://testmfg.com",
        phone="555-0100",
    )


@pytest.fixture
def sample_contacts_list():
    """A list of contact objects for unit tests."""
    return [
        Contact(
            first_name="Jane",
            last_name="Doe",
            email="jane@testmfg.com",
            title="VP Ops",
            source="test",
        )
    ]


@pytest.mark.asyncio
async def test_score_company_full(
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    """Integration test: score a company with full data."""
    score = await score_company_rule_based(db_session, sample_company)

    assert score.score_type == "rule_based"
    assert score.fit_score > 0
    assert score.industry_score == 40  # NAICS 3321 = manufacturing
    assert score.size_score == 25  # 150 employees = sweet spot
    assert score.location_score == 20  # US
    assert score.fit_score == 40 + 25 + 20 + score.engagement_score


@pytest.mark.asyncio
async def test_score_company_minimal(db_session: AsyncSession):
    """Score a company with minimal data."""
    company = Company(
        name="Unknown LLC",
        country="US",
        source="test",
    )
    db_session.add(company)
    await db_session.flush()

    score = await score_company_rule_based(db_session, company)
    assert score.fit_score == 0 + 5 + 20 + 0  # no industry, unknown size, US, no data
    assert score.industry_score == 0
    assert score.size_score == 5


@pytest.mark.asyncio
async def test_score_includes_engagement_signals(
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    """A reply on a company's contact should add an engagement bonus to its score."""
    from datetime import datetime, timezone
    from app.models.email_template import EmailSend

    # Baseline score with no engagement.
    base = await score_company_rule_based(db_session, sample_company)
    base_engagement = base.factors["engagement"]["score"]
    assert base_engagement == 0

    # Record an opened + replied email for the contact.
    db_session.add(EmailSend(
        contact_id=sample_contact.id,
        to_email=sample_contact.email,
        subject="hi",
        status="replied",
        opened_at=datetime.now(timezone.utc),
        replied_at=datetime.now(timezone.utc),
    ))
    await db_session.flush()

    rescored = await score_company_rule_based(db_session, sample_company)
    eng = rescored.factors["engagement"]
    # replied(12) + opened(3) = 15
    assert eng["score"] == 15
    assert eng["breakdown"]["replied"] == 1
    assert eng["breakdown"]["opened"] == 1
    # engagement folds into the engagement_score column (sample_company is already
    # a perfect 100 lead, so the capped fit_score itself can't climb further).
    assert rescored.engagement_score > base.engagement_score
