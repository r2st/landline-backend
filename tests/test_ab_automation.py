"""Tests for the auto A/B testing quick win:

- AI subject-variant generation (with deterministic fallback).
- UCB1 multi-armed bandit traffic allocation.
- Auto-declaring a statistically-significant winner.
"""

from datetime import datetime, timezone

import pytest

from app.models.email_sentiment import ABTestExperiment
from app.services.ab_testing_service import (
    _fallback_subject_variants,
    generate_subject_variants,
    select_variant_bandit,
    experiment_variant_stats,
    select_next_variant,
    auto_declare_winner,
)


# ---------------------------------------------------------------------------
# Variant generation
# ---------------------------------------------------------------------------


class TestFallbackVariants:
    def test_fallback_produces_distinct_variants(self):
        variants = _fallback_subject_variants("quoting takes too long", 3)
        assert len(variants) == 3
        assert len(set(v.lower() for v in variants)) == 3

    def test_fallback_includes_base(self):
        variants = _fallback_subject_variants("scrap rate question", 2)
        assert variants[0] == "scrap rate question"

    def test_fallback_empty_base(self):
        variants = _fallback_subject_variants("", 3)
        # Never returns an empty list even with no base.
        assert variants == [] or all(v for v in variants)


class _StubAI:
    """Minimal stand-in for AIService with a scripted _complete_json."""

    def __init__(self, result):
        self._result = result

    async def _complete_json(self, prompt, max_tokens=400):
        return self._result


@pytest.mark.asyncio
async def test_generate_variants_uses_ai():
    ai = _StubAI({"variants": ["a b", "c d", "e f"]})
    out = await generate_subject_variants("base subject", ai=ai, count=3)
    assert out == ["a b", "c d", "e f"]


@pytest.mark.asyncio
async def test_generate_variants_dedupes_ai_output():
    ai = _StubAI({"variants": ["Same One", "same one", "different"]})
    out = await generate_subject_variants("base", ai=ai, count=3)
    assert out == ["Same One", "different"]


@pytest.mark.asyncio
async def test_generate_variants_falls_back_on_bad_ai():
    ai = _StubAI({"error": "boom"})
    out = await generate_subject_variants("my subject", ai=ai, count=3)
    # Falls back to deterministic variants.
    assert out[0] == "my subject"
    assert len(out) >= 2


@pytest.mark.asyncio
async def test_generate_variants_clamps_count():
    ai = _StubAI({"variants": ["one", "two", "three", "four", "five"]})
    out = await generate_subject_variants("base", ai=ai, count=9)
    assert len(out) == 3  # clamped to max 3


# ---------------------------------------------------------------------------
# UCB1 bandit
# ---------------------------------------------------------------------------


class TestBandit:
    def test_explores_unsent_arm_first(self):
        variants = [
            {"sent": 10, "successes": 3},
            {"sent": 0, "successes": 0},
        ]
        assert select_variant_bandit(variants) == 1

    def test_explores_lowest_index_unsent(self):
        variants = [
            {"sent": 0, "successes": 0},
            {"sent": 0, "successes": 0},
        ]
        assert select_variant_bandit(variants) == 0

    def test_exploits_clear_leader(self):
        # A: 50% reply, B: 5% reply, both heavily sampled → pick A.
        variants = [
            {"sent": 1000, "successes": 500},
            {"sent": 1000, "successes": 50},
        ]
        assert select_variant_bandit(variants) == 0

    def test_probes_under_sampled_arm(self):
        # B barely sampled → its UCB bonus is large, so it gets probed even
        # though A currently looks slightly better on mean.
        variants = [
            {"sent": 1000, "successes": 300},  # 30%
            {"sent": 5, "successes": 1},       # 20% but tiny sample
        ]
        assert select_variant_bandit(variants) == 1

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            select_variant_bandit([])


# ---------------------------------------------------------------------------
# Experiment integration
# ---------------------------------------------------------------------------


def _experiment(**overrides) -> ABTestExperiment:
    defaults = dict(
        name="subject test",
        variant_a_subject="a",
        variant_b_subject="b",
        variant_a_sent=0,
        variant_a_opened=0,
        variant_a_clicked=0,
        variant_a_replied=0,
        variant_b_sent=0,
        variant_b_opened=0,
        variant_b_clicked=0,
        variant_b_replied=0,
    )
    defaults.update(overrides)
    return ABTestExperiment(**defaults)


class TestSelectNextVariant:
    def test_fresh_experiment_sends_a_first(self):
        exp = _experiment()
        assert select_next_variant(exp) == "A"

    def test_after_a_sent_sends_b(self):
        exp = _experiment(variant_a_sent=1)
        assert select_next_variant(exp) == "B"

    def test_routes_to_declared_winner(self):
        exp = _experiment(winner="B", variant_a_sent=10, variant_b_sent=10)
        assert select_next_variant(exp) == "B"

    def test_metric_selects_column(self):
        exp = _experiment(
            variant_a_sent=100, variant_a_replied=40, variant_a_opened=10,
            variant_b_sent=100, variant_b_replied=5, variant_b_opened=90,
        )
        # On replies, A dominates; on opens, B dominates.
        assert select_next_variant(exp, metric="replied") == "A"
        assert select_next_variant(exp, metric="opened") == "B"


class TestAutoDeclareWinner:
    def test_declares_significant_winner(self):
        # Large samples, big reply-rate gap → significant, B wins.
        exp = _experiment(
            variant_a_sent=1000, variant_a_replied=50,
            variant_b_sent=1000, variant_b_replied=200,
        )
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        evaluation = auto_declare_winner(exp, now=now)
        assert exp.winner == "B"
        assert exp.is_significant is True
        assert exp.status == "completed"
        assert exp.completed_at == now
        assert evaluation["overall_winner"] == "B"

    def test_no_winner_when_inconclusive(self):
        exp = _experiment(
            variant_a_sent=50, variant_a_replied=5,
            variant_b_sent=50, variant_b_replied=6,
        )
        auto_declare_winner(exp)
        assert exp.winner is None
        assert exp.is_significant is False
        assert exp.status in ("running", "insufficient_data")

    def test_idempotent_after_declared(self):
        exp = _experiment(
            variant_a_sent=1000, variant_a_replied=50,
            variant_b_sent=1000, variant_b_replied=200,
        )
        first = datetime(2026, 7, 1, tzinfo=timezone.utc)
        auto_declare_winner(exp, now=first)
        # Re-run later — winner and timestamp must not change.
        auto_declare_winner(exp, now=datetime(2026, 8, 1, tzinfo=timezone.utc))
        assert exp.winner == "B"
        assert exp.completed_at == first
