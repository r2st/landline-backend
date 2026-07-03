"""A/B testing service with statistical significance analysis.

Implements proper A/B test evaluation using chi-squared tests and
confidence intervals, following 2025-2026 email marketing best practices:
- Minimum 200 sends per variant for significance
- 95% confidence threshold for winner declaration
- Tracks open, click, and reply rates separately
- Auto-declares winners when significance is reached
"""

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


def _z_score_two_proportions(p1: float, n1: int, p2: float, n2: int) -> float:
    """Compute the Z-score for a two-proportion z-test.

    Tests whether proportion p1 (from n1 samples) differs significantly
    from proportion p2 (from n2 samples).
    """
    if n1 == 0 or n2 == 0:
        return 0.0

    p_pool = (p1 * n1 + p2 * n2) / (n1 + n2)
    if p_pool == 0 or p_pool == 1:
        return 0.0

    se = math.sqrt(p_pool * (1 - p_pool) * (1/n1 + 1/n2))
    if se == 0:
        return 0.0

    return (p1 - p2) / se


def _z_to_confidence(z: float) -> float:
    """Convert Z-score to approximate confidence level (two-tailed).

    Uses a simple polynomial approximation of the normal CDF.
    """
    z = abs(z)
    # Approximation of the cumulative normal distribution
    t = 1.0 / (1.0 + 0.2316419 * z)
    d = 0.3989422804014327  # 1/sqrt(2*pi)
    p = d * math.exp(-z * z / 2.0) * (
        t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    )
    confidence = 1.0 - 2.0 * p  # two-tailed
    return max(0.0, min(1.0, confidence))


def evaluate_ab_test(
    a_sent: int,
    a_metric: int,  # opens, clicks, or replies for variant A
    b_sent: int,
    b_metric: int,  # opens, clicks, or replies for variant B
    min_sample_per_variant: int = 200,
    confidence_threshold: float = 0.95,
) -> dict:
    """Evaluate an A/B test for statistical significance.

    Args:
        a_sent: Number of emails sent for variant A
        a_metric: Number of desired outcomes (opens/clicks/replies) for A
        b_sent: Number of emails sent for variant B
        b_metric: Number of desired outcomes for B
        min_sample_per_variant: Minimum sends needed per variant
        confidence_threshold: Confidence level to declare a winner (default 95%)

    Returns:
        dict with: winner, confidence, is_significant,
        rate_a, rate_b, lift, sample_sufficient,
        remaining_samples
    """
    # Check sample size sufficiency
    sample_sufficient = a_sent >= min_sample_per_variant and b_sent >= min_sample_per_variant
    remaining_a = max(0, min_sample_per_variant - a_sent)
    remaining_b = max(0, min_sample_per_variant - b_sent)

    if a_sent == 0 and b_sent == 0:
        return {
            "winner": None,
            "confidence": 0.0,
            "is_significant": False,
            "rate_a": 0.0,
            "rate_b": 0.0,
            "lift": 0.0,
            "sample_sufficient": False,
            "remaining_samples_a": remaining_a,
            "remaining_samples_b": remaining_b,
            "status": "insufficient_data",
        }

    rate_a = a_metric / a_sent if a_sent > 0 else 0.0
    rate_b = b_metric / b_sent if b_sent > 0 else 0.0

    # Calculate lift (relative improvement)
    if rate_a > 0:
        lift = (rate_b - rate_a) / rate_a
    elif rate_b > 0:
        lift = 1.0  # B is infinitely better when A is 0
    else:
        lift = 0.0

    # Statistical test
    z = _z_score_two_proportions(rate_a, a_sent, rate_b, b_sent)
    confidence = _z_to_confidence(z)
    is_significant = confidence >= confidence_threshold and sample_sufficient

    # Determine winner
    winner = None
    if is_significant:
        winner = "A" if rate_a > rate_b else "B"

    status = "running"
    if is_significant:
        status = "completed"
    elif not sample_sufficient:
        status = "insufficient_data"

    return {
        "winner": winner,
        "confidence": round(confidence, 4),
        "is_significant": is_significant,
        "rate_a": round(rate_a, 4),
        "rate_b": round(rate_b, 4),
        "lift": round(lift, 4),
        "sample_sufficient": sample_sufficient,
        "remaining_samples_a": remaining_a,
        "remaining_samples_b": remaining_b,
        "z_score": round(z, 4),
        "status": status,
    }


def evaluate_experiment(experiment) -> dict:
    """Evaluate a full ABTestExperiment model instance across all metrics.

    Returns separate analyses for open rate, click rate, and reply rate.
    """
    open_result = evaluate_ab_test(
        experiment.variant_a_sent, experiment.variant_a_opened,
        experiment.variant_b_sent, experiment.variant_b_opened,
    )
    click_result = evaluate_ab_test(
        experiment.variant_a_sent, experiment.variant_a_clicked,
        experiment.variant_b_sent, experiment.variant_b_clicked,
    )
    reply_result = evaluate_ab_test(
        experiment.variant_a_sent, experiment.variant_a_replied,
        experiment.variant_b_sent, experiment.variant_b_replied,
    )

    # Overall winner uses reply rate as primary (most valuable), then opens
    primary = reply_result if reply_result["is_significant"] else open_result

    return {
        "experiment_id": str(experiment.id),
        "name": experiment.name,
        "open_rate": open_result,
        "click_rate": click_result,
        "reply_rate": reply_result,
        "overall_winner": primary["winner"],
        "overall_confidence": primary["confidence"],
        "overall_significant": primary["is_significant"],
        "status": primary["status"],
    }


def calculate_required_sample_size(
    baseline_rate: float,
    minimum_detectable_effect: float = 0.2,  # 20% relative improvement
    confidence_level: float = 0.95,
    power: float = 0.80,
) -> int:
    """Calculate the required sample size per variant for an A/B test.

    Args:
        baseline_rate: Current conversion rate (e.g., 0.15 for 15% open rate)
        minimum_detectable_effect: Minimum relative improvement to detect (e.g., 0.2 = 20%)
        confidence_level: Desired confidence level (default 0.95)
        power: Statistical power (default 0.80)

    Returns:
        Required sample size per variant
    """
    if baseline_rate <= 0 or baseline_rate >= 1:
        return 200  # Default minimum

    # Z-scores for common confidence/power levels
    z_alpha = {0.90: 1.645, 0.95: 1.96, 0.99: 2.576}.get(confidence_level, 1.96)
    z_beta = {0.70: 0.524, 0.80: 0.842, 0.90: 1.282}.get(power, 0.842)

    p1 = baseline_rate
    p2 = baseline_rate * (1 + minimum_detectable_effect)
    p2 = min(p2, 0.999)  # Cap at near-100%

    p_avg = (p1 + p2) / 2

    numerator = (z_alpha * math.sqrt(2 * p_avg * (1 - p_avg)) +
                 z_beta * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    denominator = (p2 - p1) ** 2

    if denominator == 0:
        return 200

    n = math.ceil(numerator / denominator)
    return max(n, 200)  # Never go below 200
