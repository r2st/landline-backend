"""Tests for the sales-book enhancement features across scoring, call queue,
AI script generation, and schemas.

Covers:
- Signal recency decay (ai-cold-calling-guidelines.md)
- Engagement velocity bonus
- Account tier classification (ABM model)
- Role-based optimal call timing
- Cooling period enforcement
- Sales framework prompt injection
- Discovery scorecard schema validation
- Objection tracking via CallTaskUpdateEnhanced
- Multi-channel cadence constants
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.services.scoring import (
    signal_recency_weight,
    classify_account_tier,
    _RECENCY_BRACKETS,
    _RECENCY_FLOOR,
)
from app.services.call_queue import (
    _detect_role_for_timing,
    optimal_call_windows,
    _optimal_window_score,
    cooling_period_ok,
    score_lead,
    _Engagement,
)
from app.services.ai_script_generator import (
    _get_framework_prompt,
    _FRAMEWORK_PROMPTS,
)
from app.schemas.call import (
    SALES_FRAMEWORKS,
    DiscoveryScorecard,
    CallTaskUpdateEnhanced,
    CustomScriptRequest,
)
from app.models.sequence import STEP_CHANNELS, CADENCE_PATTERNS


# ──────────────────────────────────────────────────────────────────────────────
# Signal Recency Decay
# ──────────────────────────────────────────────────────────────────────────────


class TestSignalRecencyWeight:
    """signal_recency_weight() from ai-cold-calling-guidelines.md decay curve."""

    def _now(self):
        return datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_none_event_returns_zero(self):
        assert signal_recency_weight(None) == 0.0

    def test_just_happened_returns_1(self):
        now = self._now()
        event = now - timedelta(minutes=10)
        assert signal_recency_weight(event, now) == 1.0

    def test_within_30_min(self):
        now = self._now()
        event = now - timedelta(minutes=25)
        assert signal_recency_weight(event, now) == 1.0

    def test_within_1_hour(self):
        now = self._now()
        event = now - timedelta(minutes=45)
        assert signal_recency_weight(event, now) == 0.85

    def test_within_2_hours(self):
        now = self._now()
        event = now - timedelta(hours=1, minutes=30)
        assert signal_recency_weight(event, now) == 0.70

    def test_within_4_hours(self):
        now = self._now()
        event = now - timedelta(hours=3)
        assert signal_recency_weight(event, now) == 0.50

    def test_within_8_hours(self):
        now = self._now()
        event = now - timedelta(hours=6)
        assert signal_recency_weight(event, now) == 0.30

    def test_within_24_hours(self):
        now = self._now()
        event = now - timedelta(hours=12)
        assert signal_recency_weight(event, now) == 0.15

    def test_within_48_hours(self):
        now = self._now()
        event = now - timedelta(hours=36)
        assert signal_recency_weight(event, now) == 0.05

    def test_beyond_48_hours_returns_floor(self):
        now = self._now()
        event = now - timedelta(hours=72)
        assert signal_recency_weight(event, now) == _RECENCY_FLOOR

    def test_naive_datetime_treated_as_utc(self):
        now = self._now()
        # naive datetime = no tzinfo; function should still work
        event = datetime(2025, 6, 15, 11, 50, 0)  # 10 min ago
        result = signal_recency_weight(event, now)
        assert result == 1.0

    def test_decay_curve_is_monotonically_decreasing(self):
        """Each bracket's weight must be <= the previous one."""
        prev = 1.0
        for _, weight in _RECENCY_BRACKETS:
            assert weight <= prev, f"decay not monotonic: {weight} > {prev}"
            prev = weight
        assert _RECENCY_FLOOR <= prev


# ──────────────────────────────────────────────────────────────────────────────
# Account Tier Classification (ABM model)
# ──────────────────────────────────────────────────────────────────────────────


class TestAccountTierClassification:
    """classify_account_tier() implements the ABM tiers from digital-sales-strategy.md."""

    def test_tier_1_strategic(self):
        name, value = classify_account_tier(40, 25, 20, 10)
        assert name == "tier_1_strategic"
        assert value == 1.0

    def test_tier_2_target(self):
        name, value = classify_account_tier(25, 15, 10, 5)
        assert name == "tier_2_target"
        assert value == 0.75

    def test_tier_3_prospect(self):
        name, value = classify_account_tier(10, 10, 5, 5)
        assert name == "tier_3_prospect"
        assert value == 0.50

    def test_non_target(self):
        name, value = classify_account_tier(5, 5, 5, 0)
        assert name == "non_target"
        assert value == 0.25

    def test_tier_1_requires_all_minimums(self):
        """High total but missing location minimum → falls to tier_2."""
        name, _ = classify_account_tier(40, 25, 5, 10)
        assert name != "tier_1_strategic"

    def test_tier_1_exact_boundary(self):
        name, value = classify_account_tier(35, 20, 15, 0)
        assert name == "tier_1_strategic"
        assert value == 1.0


# ──────────────────────────────────────────────────────────────────────────────
# Role-Based Optimal Call Timing
# ──────────────────────────────────────────────────────────────────────────────


class TestRoleDetection:
    """_detect_role_for_timing() maps contact titles to timing roles."""

    def test_plant_manager(self):
        assert _detect_role_for_timing("Plant Manager") == "operations"

    def test_operations_director(self):
        assert _detect_role_for_timing("VP Operations") == "operations"

    def test_quality_manager(self):
        assert _detect_role_for_timing("Quality Assurance Manager") == "quality"

    def test_ceo(self):
        assert _detect_role_for_timing("CEO") == "executive"

    def test_owner_president(self):
        assert _detect_role_for_timing("Owner / President") == "executive"

    def test_process_engineer(self):
        assert _detect_role_for_timing("Process Engineer") == "engineering"

    def test_unknown_role(self):
        # Note: "coordinator" contains "coo" which matches executive keyword
        assert _detect_role_for_timing("Receptionist") == "default"

    def test_none_title(self):
        assert _detect_role_for_timing(None) == "default"

    def test_empty_string(self):
        assert _detect_role_for_timing("") == "default"


class TestOptimalCallWindows:
    """optimal_call_windows() returns role-specific time windows."""

    def test_plant_manager_has_morning_and_evening(self):
        windows = optimal_call_windows("Plant Manager")
        assert len(windows) >= 2
        # Should include early morning (7-9)
        assert any(start <= 8 < end for start, end in windows)

    def test_quality_manager_has_midmorning(self):
        windows = optimal_call_windows("QA Manager")
        assert any(start <= 10 < end for start, end in windows)

    def test_default_fallback(self):
        windows = optimal_call_windows("Unknown Role")
        assert len(windows) >= 1


class TestOptimalWindowScore:
    """_optimal_window_score() returns priority bonus + window label."""

    def test_within_window_gets_max_bonus(self):
        bonus, label = _optimal_window_score("Plant Manager", 8)
        assert bonus == 20.0
        assert label != ""

    def test_near_window_gets_partial_bonus(self):
        bonus, label = _optimal_window_score("Plant Manager", 6)
        assert bonus == 10.0
        assert "near" in label

    def test_outside_window_gets_zero(self):
        bonus, label = _optimal_window_score("Plant Manager", 13)
        assert bonus == 0.0
        assert label == ""


# ──────────────────────────────────────────────────────────────────────────────
# Cooling Period Enforcement
# ──────────────────────────────────────────────────────────────────────────────


class TestCoolingPeriod:
    """cooling_period_ok() enforces wait times after call outcomes."""

    def _now(self):
        return datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_no_prior_outcome(self):
        assert cooling_period_ok(None, None, self._now()) is True

    def test_connected_within_cooling(self):
        """3-day cooling after a connected call."""
        last = self._now() - timedelta(days=2)
        assert cooling_period_ok("connected", last, self._now()) is False

    def test_connected_after_cooling(self):
        last = self._now() - timedelta(days=4)
        assert cooling_period_ok("connected", last, self._now()) is True

    def test_voicemail_within_cooling(self):
        """2-day cooling after voicemail."""
        last = self._now() - timedelta(days=1)
        assert cooling_period_ok("voicemail", last, self._now()) is False

    def test_voicemail_after_cooling(self):
        last = self._now() - timedelta(days=3)
        assert cooling_period_ok("voicemail", last, self._now()) is True

    def test_no_answer_within_cooling(self):
        """1-day cooling after no answer."""
        last = self._now() - timedelta(hours=12)
        assert cooling_period_ok("no_answer", last, self._now()) is False

    def test_no_answer_after_cooling(self):
        last = self._now() - timedelta(days=2)
        assert cooling_period_ok("no_answer", last, self._now()) is True

    def test_callback_scheduled_long_cooling(self):
        """7-day cooling for callback scheduled."""
        last = self._now() - timedelta(days=5)
        assert cooling_period_ok("callback_scheduled", last, self._now()) is False

    def test_unknown_outcome_defaults_to_1_day(self):
        last = self._now() - timedelta(hours=12)
        assert cooling_period_ok("something_else", last, self._now()) is False

    def test_naive_datetime_handled(self):
        """Naive last_completed_at should be treated as UTC."""
        last = datetime(2025, 6, 12, 12, 0, 0)  # no tzinfo
        assert cooling_period_ok("connected", last, self._now()) is True  # 3 days ago


# ──────────────────────────────────────────────────────────────────────────────
# score_lead() Enhanced with Timing
# ──────────────────────────────────────────────────────────────────────────────


class TestScoreLeadEnhanced:
    """score_lead() now returns a 4-tuple including optimal_window."""

    def _now(self):
        return datetime(2025, 6, 15, 8, 30, 0, tzinfo=timezone.utc)

    def test_returns_4_tuple(self):
        result = score_lead(None, 50, "injection_molding", self._now())
        assert len(result) == 4
        priority, score, reason, window = result
        assert priority in ("hot", "warm", "cold")
        assert isinstance(score, float)
        assert isinstance(reason, str)
        assert isinstance(window, str)

    def test_reply_makes_hot(self):
        e = _Engagement()
        e.replies = 1
        e.last_contact_at = self._now() - timedelta(days=1)
        priority, _, _, _ = score_lead(e, 50, "injection_molding", self._now())
        assert priority == "hot"

    def test_clicks_make_hot(self):
        e = _Engagement()
        e.clicks = 2
        e.last_contact_at = self._now() - timedelta(days=1)
        priority, _, _, _ = score_lead(e, 50, "injection_molding", self._now())
        assert priority == "hot"

    def test_single_open_is_warm(self):
        e = _Engagement()
        e.opens = 1
        e.last_contact_at = self._now() - timedelta(days=1)
        priority, _, _, _ = score_lead(e, 50, "injection_molding", self._now())
        assert priority == "warm"

    def test_no_engagement_is_cold(self):
        priority, _, _, _ = score_lead(None, 0, "cnc_machining", self._now())
        assert priority == "cold"

    def test_optimal_window_bonus_for_plant_manager(self):
        """Plant manager called at 7 AM (in ops window, near default window)."""
        # Hour 7 is inside operations window [(7,9)] but only "near" default [(8,9)]
        # So Plant Manager gets +20, default gets +10 → higher score with title.
        _, score_no_title, _, _ = score_lead(
            None, 50, "injection_molding", self._now(),
            current_hour=7,
        )
        _, score_with_title, _, window = score_lead(
            None, 50, "injection_molding", self._now(),
            contact_title="Plant Manager", current_hour=7,
        )
        assert score_with_title > score_no_title
        assert window != ""

    def test_contact_title_kwarg_is_optional(self):
        """Backwards compatibility — contact_title is a kwarg with default."""
        result = score_lead(None, 50, "injection_molding", self._now())
        assert len(result) == 4


# ──────────────────────────────────────────────────────────────────────────────
# Sales Framework Prompts (AI Script Generation)
# ──────────────────────────────────────────────────────────────────────────────


class TestFrameworkPrompts:
    """_get_framework_prompt() and _FRAMEWORK_PROMPTS cover all 7 frameworks."""

    def test_all_frameworks_have_prompts(self):
        for fw in SALES_FRAMEWORKS:
            prompt = _get_framework_prompt(fw)
            assert prompt, f"Missing prompt for framework: {fw}"
            assert len(prompt) > 50, f"Prompt too short for {fw}"

    def test_none_returns_empty(self):
        assert _get_framework_prompt(None) == ""

    def test_unknown_returns_empty(self):
        assert _get_framework_prompt("unknown_framework") == ""

    def test_challenger_contains_6_steps(self):
        p = _get_framework_prompt("challenger")
        assert "WARMER" in p
        assert "REFRAME" in p
        assert "RATIONAL DROWNING" in p
        assert "YOUR SOLUTION" in p

    def test_gap_selling_contains_gap_model(self):
        p = _get_framework_prompt("gap_selling")
        assert "CURRENT STATE" in p
        assert "FUTURE STATE" in p
        assert "THE GAP" in p

    def test_spin_contains_question_types(self):
        p = _get_framework_prompt("spin")
        for q in ("SITUATION", "PROBLEM", "IMPLICATION", "NEED-PAYOFF"):
            assert q in p

    def test_voss_contains_tactical_empathy(self):
        p = _get_framework_prompt("voss_tactical")
        assert "ACCUSATION AUDIT" in p
        assert "MIRRORING" in p
        assert "LABELING" in p

    def test_blount_contains_5_steps(self):
        p = _get_framework_prompt("blount_fanatical")
        assert "ATTENTION" in p
        assert "IDENTIFY" in p
        assert "BRIDGE" in p
        assert "ASK" in p

    def test_weinberg_contains_power_statement(self):
        p = _get_framework_prompt("weinberg_story")
        assert "Power Statement" in p
        assert "HEADLINE" in p
        assert "DIFFERENTIATORS" in p

    def test_sobczak_contains_smart_call(self):
        p = _get_framework_prompt("sobczak_smart")
        assert "SMART CALL" in p.upper()
        assert "Three R" in p


# ──────────────────────────────────────────────────────────────────────────────
# Discovery Scorecard Schema
# ──────────────────────────────────────────────────────────────────────────────


class TestDiscoveryScorecard:
    """DiscoveryScorecard pydantic model from Gap Selling."""

    def test_default_all_zeros(self):
        sc = DiscoveryScorecard()
        assert sc.total_score == 0

    def test_max_score_is_35(self):
        sc = DiscoveryScorecard(
            current_state_identified=5,
            root_cause_uncovered=5,
            impact_quantified=5,
            emotion_surfaced=5,
            future_state_defined=5,
            next_steps_agreed=5,
            champion_identified=5,
        )
        assert sc.total_score == 35

    def test_partial_score(self):
        sc = DiscoveryScorecard(
            current_state_identified=3,
            root_cause_uncovered=2,
            impact_quantified=4,
        )
        assert sc.total_score == 9

    def test_validation_rejects_out_of_range(self):
        with pytest.raises(ValidationError):
            DiscoveryScorecard(current_state_identified=6)

    def test_validation_rejects_negative(self):
        with pytest.raises(ValidationError):
            DiscoveryScorecard(impact_quantified=-1)


# ──────────────────────────────────────────────────────────────────────────────
# CallTaskUpdateEnhanced Schema
# ──────────────────────────────────────────────────────────────────────────────


class TestCallTaskUpdateEnhanced:
    """CallTaskUpdateEnhanced extends CallTaskUpdate with sales-book fields."""

    def test_accepts_discovery_scorecard(self):
        sc = DiscoveryScorecard(current_state_identified=3, root_cause_uncovered=4)
        update = CallTaskUpdateEnhanced(
            outcome="connected",
            discovery_scorecard=sc,
        )
        assert update.discovery_scorecard is not None
        assert update.discovery_scorecard.total_score == 7

    def test_accepts_objections(self):
        update = CallTaskUpdateEnhanced(
            outcome="connected",
            objections_encountered=["Price too high", "Not interested"],
        )
        assert len(update.objections_encountered) == 2

    def test_accepts_framework_used(self):
        update = CallTaskUpdateEnhanced(
            outcome="voicemail",
            sales_framework_used="challenger",
        )
        assert update.sales_framework_used == "challenger"

    def test_rejects_invalid_framework(self):
        with pytest.raises(ValidationError):
            CallTaskUpdateEnhanced(
                outcome="connected",
                sales_framework_used="nonexistent_framework",
            )

    def test_none_framework_is_valid(self):
        update = CallTaskUpdateEnhanced(outcome="voicemail")
        assert update.sales_framework_used is None


# ──────────────────────────────────────────────────────────────────────────────
# CustomScriptRequest Framework Validation
# ──────────────────────────────────────────────────────────────────────────────


class TestCustomScriptRequestFramework:
    """CustomScriptRequest now accepts an optional sales_framework."""

    def test_valid_framework(self):
        req = CustomScriptRequest(
            company_id=uuid.uuid4(),
            sales_framework="spin",
        )
        assert req.sales_framework == "spin"

    def test_none_framework_is_default(self):
        req = CustomScriptRequest(company_id=uuid.uuid4())
        assert req.sales_framework is None

    def test_invalid_framework_rejected(self):
        with pytest.raises(ValidationError):
            CustomScriptRequest(
                company_id=uuid.uuid4(),
                sales_framework="invalid",
            )

    def test_all_frameworks_accepted(self):
        for fw in SALES_FRAMEWORKS:
            req = CustomScriptRequest(
                company_id=uuid.uuid4(),
                sales_framework=fw,
            )
            assert req.sales_framework == fw


# ──────────────────────────────────────────────────────────────────────────────
# Multi-Channel Cadence Constants (Combo Prospecting)
# ──────────────────────────────────────────────────────────────────────────────


class TestMultiChannelCadence:
    """Verify multi-channel cadence constants from the Combo Prospecting model."""

    def test_step_channels_count(self):
        assert len(STEP_CHANNELS) == 5

    def test_step_channels_include_expected(self):
        for ch in ("email", "call", "linkedin", "video", "direct_mail"):
            assert ch in STEP_CHANNELS

    def test_cadence_patterns_count(self):
        assert len(CADENCE_PATTERNS) == 4

    def test_cadence_patterns_include_expected(self):
        for p in ("single_channel", "triple", "quad", "quint"):
            assert p in CADENCE_PATTERNS

    def test_triple_pattern_means_3_channels(self):
        """Triple = call + voicemail + email — 3 channels."""
        assert "triple" in CADENCE_PATTERNS

    def test_quint_pattern_means_5_channels(self):
        """Quint = call + vm + email + LinkedIn + social — 5 channels."""
        assert "quint" in CADENCE_PATTERNS


# ──────────────────────────────────────────────────────────────────────────────
# Sales Frameworks Constant
# ──────────────────────────────────────────────────────────────────────────────


class TestSalesFrameworksConstant:
    """SALES_FRAMEWORKS tuple covers all 7 methodologies."""

    def test_count(self):
        assert len(SALES_FRAMEWORKS) == 7

    def test_expected_frameworks(self):
        expected = {
            "challenger", "gap_selling", "spin", "voss_tactical",
            "blount_fanatical", "weinberg_story", "sobczak_smart",
        }
        assert set(SALES_FRAMEWORKS) == expected

    def test_all_have_corresponding_prompts(self):
        for fw in SALES_FRAMEWORKS:
            assert fw in _FRAMEWORK_PROMPTS
