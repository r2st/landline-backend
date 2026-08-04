"""Optimal send timing — analyzes open data to suggest best send times.

A rate needs a denominator before it means anything
---------------------------------------------------
``get_optimal_times`` ranks days, hours and (day, hour) cells by open rate and
hands the top few to the Console as "your best send times". It ranked them on
the rate alone, with no floor under the sample, so one send that happened to be
opened was a 100% cell and outranked every hour with real traffic behind it.

That is not a rare tail case, it is the *default* state of the surface. Open
rates live around 30-60%, so no busy hour ever reaches 100% — the top of this
list was structurally reserved for the thinnest cells on the grid. A 7x24 grid
has 168 cells and a new tenant fills them one send at a time, which means the
recommendation was at its most confident exactly when it knew least, and it
pointed at whichever near-empty cell got lucky. An operator following it moves
their sending into the hour with the least evidence for it.

``get_heatmap`` right below already refuses to nominate a ``best_cell`` under
:data:`MIN_SAMPLE` — "not a lucky 1/1", as its own comment puts it. The same
floor now applies to all three rankings here, and the cells that don't clear it
are still *returned* (they are real data, and the counts are what show the grid
filling up) — they are only barred from being called best. When nothing clears
the floor the caller is told so explicitly, rather than being handed noise
formatted as a recommendation.
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, extract, and_

from app.models.outreach import OutreachEvent

logger = logging.getLogger(__name__)

# Sends a bucket needs before its open rate may be ranked as a "best" time.
# Shared by ``get_optimal_times`` and ``get_heatmap`` so the two surfaces cannot
# recommend different hours from the same data.
MIN_SAMPLE = 3


def _rate(stats: dict) -> float:
    """Open rate for a ``{"total", "opened"}`` bucket, 0.0 when nothing was sent."""
    total = stats.get("total") or 0
    return (stats.get("opened") or 0) / total if total else 0.0


def _rank(buckets, *, min_sample: int = MIN_SAMPLE):
    """``buckets`` sorted best-rate-first, keeping only those with enough sends.

    Sorted on ``(rate, total)`` so that among buckets tied on rate — which is
    common at these sample sizes, where most cells are some small fraction with
    a small denominator — the one with more sends behind it wins. Without the
    tiebreak the order among ties is whatever the dict happened to yield.
    """
    qualified = [(key, stats) for key, stats in buckets if (stats.get("total") or 0) >= min_sample]
    qualified.sort(key=lambda kv: (_rate(kv[1]), kv[1].get("total") or 0), reverse=True)
    return qualified


class SendTimingAnalyzer:
    """Analyzes email open patterns to find optimal send times."""

    async def get_optimal_times(self, db: AsyncSession) -> dict:
        """Analyze sent vs opened events to find the best send times."""
        # Get all 'sent' events with their timestamps, and count how many
        # led to an 'opened' event for the same enrollment+step.
        sent_subq = (
            select(
                OutreachEvent.enrollment_id,
                OutreachEvent.step_number,
                OutreachEvent.occurred_at.label("sent_at"),
            )
            .where(OutreachEvent.event_type == "sent")
            .subquery()
        )

        opened_subq = (
            select(
                OutreachEvent.enrollment_id,
                OutreachEvent.step_number,
            )
            .where(OutreachEvent.event_type == "opened")
            .distinct()
            .subquery()
        )

        # Join sent events with opened events
        result = await db.execute(
            select(
                extract("dow", sent_subq.c.sent_at).label("day_of_week"),
                extract("hour", sent_subq.c.sent_at).label("hour"),
                func.count().label("total"),
                func.count(opened_subq.c.enrollment_id).label("opened"),
            )
            .outerjoin(
                opened_subq,
                and_(
                    sent_subq.c.enrollment_id == opened_subq.c.enrollment_id,
                    sent_subq.c.step_number == opened_subq.c.step_number,
                ),
            )
            .group_by("day_of_week", "hour")
        )
        rows = result.all()

        if not rows:
            return {
                "has_data": False,
                "recommendation": "Not enough data yet. Send more emails to build timing insights.",
                "default_best_times": [
                    {"day": "Tuesday", "hour": 10, "reason": "Industry average"},
                    {"day": "Wednesday", "hour": 14, "reason": "Industry average"},
                    {"day": "Thursday", "hour": 9, "reason": "Industry average"},
                ],
            }

        day_names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

        # Calculate open rates by day and hour
        by_day = defaultdict(lambda: {"total": 0, "opened": 0})
        by_hour = defaultdict(lambda: {"total": 0, "opened": 0})
        by_day_hour = {}

        for row in rows:
            day = int(row.day_of_week)
            hour = int(row.hour)
            total = row.total
            opened = row.opened

            by_day[day]["total"] += total
            by_day[day]["opened"] += opened
            by_hour[hour]["total"] += total
            by_hour[hour]["opened"] += opened
            by_day_hour[(day, hour)] = {
                "total": total,
                "opened": opened,
                "rate": round(opened / max(total, 1) * 100, 1),
            }

        # Ranked, and floored at MIN_SAMPLE sends. A bucket below the floor is
        # still counted in the totals below — it happened — but it is not
        # eligible to be called a best time on the strength of one open.
        best_days = _rank(by_day.items())
        best_hours = _rank(by_hour.items())
        best_combos = _rank(by_day_hour.items())[:5]

        return {
            "has_data": True,
            # Explicit, so the Console can say "not enough data yet" instead of
            # rendering an empty list as though the analysis found nothing
            # interesting. Data exists; none of it is thick enough to rank.
            "min_sample": MIN_SAMPLE,
            "has_ranking": bool(best_days or best_hours or best_combos),
            "total_emails_analyzed": sum(d["total"] for d in by_day.values()),
            "overall_open_rate": round(
                sum(d["opened"] for d in by_day.values()) /
                max(sum(d["total"] for d in by_day.values()), 1) * 100, 1
            ),
            "best_days": [
                {
                    "day": day_names[d],
                    "open_rate": round(stats["opened"] / max(stats["total"], 1) * 100, 1),
                    "emails_sent": stats["total"],
                }
                for d, stats in best_days[:3]
            ],
            "best_hours": [
                {
                    "hour": h,
                    "hour_label": f"{h}:00" if h >= 10 else f"0{h}:00",
                    "open_rate": round(stats["opened"] / max(stats["total"], 1) * 100, 1),
                    "emails_sent": stats["total"],
                }
                for h, stats in best_hours[:5]
            ],
            "best_combinations": [
                {
                    "day": day_names[combo[0]],
                    "hour": combo[1],
                    "hour_label": f"{combo[1]}:00" if combo[1] >= 10 else f"0{combo[1]}:00",
                    "open_rate": stats["rate"],
                    "sample_size": stats["total"],
                }
                for combo, stats in best_combos
            ],
        }


    async def get_heatmap(self, db: AsyncSession) -> dict:
        """Full (day-of-week, hour) grid of send/open counts and open rates.

        Unlike ``get_optimal_times`` (which returns only the top few combos),
        this returns every populated cell so the Console can render a complete
        7×24 heatmap. Days are 0=Sunday..6=Saturday to match the rest of the
        send-timing surface.
        """
        sent_subq = (
            select(
                OutreachEvent.enrollment_id,
                OutreachEvent.step_number,
                OutreachEvent.occurred_at.label("sent_at"),
            )
            .where(OutreachEvent.event_type == "sent")
            .subquery()
        )
        opened_subq = (
            select(
                OutreachEvent.enrollment_id,
                OutreachEvent.step_number,
            )
            .where(OutreachEvent.event_type == "opened")
            .distinct()
            .subquery()
        )
        result = await db.execute(
            select(
                extract("dow", sent_subq.c.sent_at).label("day_of_week"),
                extract("hour", sent_subq.c.sent_at).label("hour"),
                func.count().label("total"),
                func.count(opened_subq.c.enrollment_id).label("opened"),
            )
            .outerjoin(
                opened_subq,
                and_(
                    sent_subq.c.enrollment_id == opened_subq.c.enrollment_id,
                    sent_subq.c.step_number == opened_subq.c.step_number,
                ),
            )
            .group_by("day_of_week", "hour")
        )
        rows = result.all()

        cells = []
        total_sent = 0
        total_opened = 0
        best = None
        for row in rows:
            sent = int(row.total)
            opened = int(row.opened)
            if sent <= 0:
                continue
            rate = round(opened / sent, 4)
            cell = {
                "dow": int(row.day_of_week),
                "hour": int(row.hour),
                "sent": sent,
                "opened": opened,
                "open_rate": rate,
            }
            cells.append(cell)
            total_sent += sent
            total_opened += opened
            # Best cell needs enough sample to be meaningful, not a lucky 1/1.
            if sent >= MIN_SAMPLE and (best is None or rate > best["open_rate"]):
                best = cell

        cells.sort(key=lambda c: (c["dow"], c["hour"]))
        return {
            "has_data": total_sent > 0,
            "cells": cells,
            "total_sent": total_sent,
            "total_opened": total_opened,
            "overall_open_rate": round(total_opened / max(total_sent, 1), 4),
            "best_cell": best,
            "min_sample": MIN_SAMPLE,
        }


def get_send_timing() -> SendTimingAnalyzer:
    return SendTimingAnalyzer()
