"""A/B testing service with statistical significance analysis.

Implements proper A/B test evaluation using chi-squared tests and
confidence intervals, following 2025-2026 email marketing best practices:
- Minimum 200 sends per variant for significance
- 95% confidence threshold for winner declaration
- Tracks open, click, and reply rates separately
- Auto-declares winners when significance is reached

Automation layer (quick win):
- ``generate_subject_variants`` — AI writes 2-3 subject-line variants for a
  sequence step (deterministic fallback when the API is unavailable).
- ``select_next_variant`` / ``select_variant_bandit`` — a UCB1 multi-armed
  bandit chooses which variant to send next, shifting traffic toward the
  better performer as data accrues instead of a fixed 50/50 split.
- ``auto_declare_winner`` — once a variant is statistically significant, it is
  declared the winner and the experiment is marked complete.
"""

import logging
import math
from datetime import datetime, timezone
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


def evaluate_experiment(experiment, metric: Optional[str] = None) -> dict:
    """Evaluate a full ABTestExperiment model instance across all metrics.

    Returns separate analyses for open rate, click rate, and reply rate.

    ``metric`` names the outcome the experiment is actually optimising and, when
    given, is the **only** one that can declare a winner. This matters for
    subject-line tests: the subject is the entire lever on whether a message gets
    opened and has almost no bearing on what happens after, so a subject test
    scored on reply rate is measuring the body's work and attributing it to the
    subject. Passing ``metric="opened"`` scores it on the thing under test.

    Omitting ``metric`` keeps the historical behaviour — reply rate first, falling
    back to open rate when replies aren't significant — for callers that never
    declared what they were testing.
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

    by_metric = {
        "opened": open_result,
        "clicked": click_result,
        "replied": reply_result,
    }
    if metric is not None:
        primary = by_metric[_METRIC_ALIASES.get(metric, "replied")]
    else:
        # Legacy: reply rate is the most valuable signal, opens are the fallback.
        primary = reply_result if reply_result["is_significant"] else open_result

    return {
        "experiment_id": str(experiment.id),
        "name": experiment.name,
        "primary_metric": _METRIC_ALIASES.get(metric, "replied") if metric else None,
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


# ---------------------------------------------------------------------------
# Automation: AI variant generation
# ---------------------------------------------------------------------------

# The metric each variant is optimised toward, mapped to its EmailSend column
# roots on the ABTestExperiment model (variant_<a|b>_<metric>).
_METRIC_ALIASES = {
    "opened": "opened",
    "open": "opened",
    "clicked": "clicked",
    "click": "clicked",
    "replied": "replied",
    "reply": "replied",
}


def _fallback_subject_variants(base_subject: str, count: int) -> list[str]:
    """Deterministic subject variants used when AI generation is unavailable.

    Applies a few well-worn subject-line transforms (question form, a lowercase
    "quick" opener) so the caller always gets distinct options to split on.
    """
    base = (base_subject or "").strip()
    candidates = [base]
    if base:
        stripped = base.rstrip("?.")
        candidates.append(f"{stripped}?")
        first = stripped[0].lower() + stripped[1:] if stripped else stripped
        candidates.append(f"quick question — {first}")
        candidates.append(f"{first}, worth a look?")
    # De-dupe while preserving order, drop empties.
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        c = c.strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out[: max(1, count)]


async def generate_subject_variants(
    base_subject: str,
    *,
    industry: Optional[str] = None,
    body_preview: str = "",
    count: int = 3,
    ai=None,
) -> list[str]:
    """Generate ``count`` (2-3) subject-line variants for a sequence step.

    Uses Claude to write short, distinct, non-spammy subject lines in the
    Apprend voice. Never raises: on any failure (no API key, parse error, API
    error) it falls back to :func:`_fallback_subject_variants` so the caller
    always gets usable variants to auto-split traffic across.
    """
    count = max(2, min(3, count))
    try:
        if ai is None:
            from app.services.ai_service import get_ai_service

            ai = get_ai_service()

        industry_line = f"Industry: {industry}\n" if industry else ""
        preview_line = f"Email opening: {body_preview[:200]}\n" if body_preview else ""
        prompt = f"""You run A/B tests on cold-email subject lines for Apprend Technologies
(manufacturing software). Write {count} DISTINCT subject-line variants to test
for this sequence step.

{industry_line}{preview_line}Current subject: {base_subject or '(none yet)'}

Rules:
- Short, lowercase, like something you'd actually type — not a headline.
- Each variant should take a genuinely different angle (question, curiosity,
  specific result, pain point) so the test is informative.
- No spam-trigger words, no exclamation marks, no ALL CAPS.

Return ONLY valid JSON: {{"variants": ["...", "...", "..."]}}"""

        result = await ai._complete_json(prompt, max_tokens=400)
        variants = result.get("variants") if isinstance(result, dict) else None
        if isinstance(variants, list):
            cleaned = [str(v).strip() for v in variants if str(v).strip()]
            # De-dupe case-insensitively, keep order.
            seen: set[str] = set()
            out: list[str] = []
            for v in cleaned:
                if v.lower() not in seen:
                    seen.add(v.lower())
                    out.append(v)
            if len(out) >= 2:
                return out[:count]
    except Exception as e:  # noqa: BLE001 — never fail the caller
        logger.warning("AI subject-variant generation failed: %s", e)

    return _fallback_subject_variants(base_subject, count)


def _fallback_body_variants(base_body: str, count: int) -> list[str]:
    """Deterministic body variants when AI generation is unavailable.

    Returns the original body first so the control is always present; the caller
    can accept/edit the alternatives. Never raises, always returns ≥1 option.
    """
    base = (base_body or "").strip()
    out = [base] if base else []
    if not out:
        out = ["Quick note — thought this might be relevant to your team."]
    return out[: max(1, count)]


async def generate_body_variants(
    base_body: str,
    *,
    industry: Optional[str] = None,
    subject: str = "",
    count: int = 3,
    ai=None,
) -> list[str]:
    """Generate ``count`` (2-3) email-body variants for a sequence step.

    Thin parallel to :func:`generate_subject_variants` for A/B-testing bodies.
    Uses Claude to write short, distinct cold-email bodies in the Apprend voice;
    never raises — falls back to :func:`_fallback_body_variants` on any failure.
    """
    count = max(2, min(3, count))
    try:
        if ai is None:
            from app.services.ai_service import get_ai_service

            ai = get_ai_service()

        industry_line = f"Industry: {industry}\n" if industry else ""
        subject_line = f"Subject line: {subject}\n" if subject else ""
        prompt = f"""You run A/B tests on cold-email bodies for Apprend Technologies
(manufacturing software). Write {count} DISTINCT short email-body variants to
test for this sequence step.

{industry_line}{subject_line}Current body: {base_body or '(none yet)'}

Rules:
- Short (2-4 sentences), conversational, like a human wrote it — not marketing copy.
- Each variant should take a genuinely different angle (pain point, specific
  result, curiosity, social proof) so the test is informative.
- No spam-trigger words, no exclamation marks, no ALL CAPS. Keep any template
  variables (e.g. {{{{first_name}}}}) intact if present in the current body.

Return ONLY valid JSON: {{"variants": ["...", "...", "..."]}}"""

        result = await ai._complete_json(prompt, max_tokens=800)
        variants = result.get("variants") if isinstance(result, dict) else None
        if isinstance(variants, list):
            cleaned = [str(v).strip() for v in variants if str(v).strip()]
            seen: set[str] = set()
            out: list[str] = []
            for v in cleaned:
                if v.lower() not in seen:
                    seen.add(v.lower())
                    out.append(v)
            if len(out) >= 2:
                return out[:count]
    except Exception as e:  # noqa: BLE001 — never fail the caller
        logger.warning("AI body-variant generation failed: %s", e)

    return _fallback_body_variants(base_body, count)


# ---------------------------------------------------------------------------
# Automation: multi-armed bandit traffic allocation (UCB1)
# ---------------------------------------------------------------------------


def select_variant_bandit(
    variants: list[dict], *, exploration: float = 2.0
) -> int:
    """Pick the next variant to send using the UCB1 multi-armed bandit.

    ``variants`` is a list of ``{"sent": int, "successes": int}`` dicts. UCB1 is
    deterministic: any never-sent arm is explored first (lowest index wins ties);
    otherwise the arm maximising ``mean + sqrt(exploration * ln(total) / sent)``
    is chosen, so traffic shifts toward the leader while still occasionally
    probing the other arm. Returns the index of the chosen variant.
    """
    if not variants:
        raise ValueError("variants must be non-empty")

    # Explore any arm that has never been sent (deterministic: first such arm).
    for i, v in enumerate(variants):
        if int(v.get("sent", 0)) <= 0:
            return i

    total = sum(int(v.get("sent", 0)) for v in variants)
    ln_total = math.log(total) if total > 0 else 0.0

    best_idx = 0
    best_score = float("-inf")
    for i, v in enumerate(variants):
        sent = int(v.get("sent", 0))
        successes = int(v.get("successes", 0))
        mean = successes / sent
        bonus = math.sqrt(exploration * ln_total / sent)
        score = mean + bonus
        if score > best_score:
            best_score = score
            best_idx = i
    return best_idx


def experiment_variant_stats(experiment, metric: str = "replied") -> list[dict]:
    """Extract ``[{name, sent, successes}, ...]`` for A and B from an experiment.

    ``metric`` selects which outcome counts as a "success" (opened/clicked/
    replied); reply rate is the default because a reply is the most valuable
    signal.
    """
    m = _METRIC_ALIASES.get(metric, "replied")
    return [
        {
            "name": "A",
            "sent": experiment.variant_a_sent,
            "successes": getattr(experiment, f"variant_a_{m}"),
        },
        {
            "name": "B",
            "sent": experiment.variant_b_sent,
            "successes": getattr(experiment, f"variant_b_{m}"),
        },
    ]


def select_next_variant(experiment, metric: str = "replied") -> str:
    """Return ``"A"`` or ``"B"`` — the variant the bandit would send next.

    Once a winner has been declared, always route to the winner (exploitation
    is over). Otherwise use UCB1 over the chosen metric.
    """
    if experiment.winner in ("A", "B"):
        return experiment.winner
    stats = experiment_variant_stats(experiment, metric)
    return stats[select_variant_bandit(stats)]["name"]


# ---------------------------------------------------------------------------
# Automation: auto-declare the winner
# ---------------------------------------------------------------------------


def auto_declare_winner(
    experiment, now: Optional[datetime] = None, metric: Optional[str] = None
) -> dict:
    """Evaluate an experiment and, if a variant is significant, declare it.

    Mutates the experiment in place: sets ``winner``, ``confidence_level``,
    ``is_significant`` and ``status``, and stamps ``completed_at`` when a winner
    is reached. Returns the full evaluation dict from :func:`evaluate_experiment`.
    Idempotent — re-running after a winner is set leaves it unchanged.

    ``metric`` is passed straight through: a test declares its winner on the
    outcome it was set up to optimise, not on whichever outcome happens to reach
    significance first.
    """
    evaluation = evaluate_experiment(experiment, metric)

    experiment.confidence_level = evaluation["overall_confidence"]
    experiment.is_significant = evaluation["overall_significant"]

    if evaluation["overall_significant"] and evaluation["overall_winner"]:
        if experiment.winner not in ("A", "B"):
            experiment.winner = evaluation["overall_winner"]
            experiment.status = "completed"
            experiment.completed_at = now or datetime.now(timezone.utc)
            logger.info(
                "A/B experiment %s auto-declared winner %s (confidence %.3f)",
                getattr(experiment, "id", "?"),
                experiment.winner,
                evaluation["overall_confidence"],
            )
    elif experiment.winner not in ("A", "B"):
        # No winner yet — keep the running/insufficient status in sync.
        experiment.status = evaluation["status"]

    return evaluation
