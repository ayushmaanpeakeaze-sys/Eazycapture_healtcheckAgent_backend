"""The Cash Health % indicator — a weighted measure of how well the current
cash covers the upcoming outgoings, with SHORT-TERM outgoings weighted more
heavily than long-term ones (being unable to pay this week's wages matters more
than a multi-year loan).

Cash is applied down the categories in priority order; each category's coverage
(0–1) is weighted by ``category_weight`` and averaged. Categories you owe
nothing in don't count. No outgoings at all → 100%.
"""
from __future__ import annotations

from typing import Any

from app.modules.insights.logic.cash_health.config import category_weight


def _rating(score: float) -> str:
    if score >= 75:
        return "strong"
    if score >= 50:
        return "moderate"
    if score >= 25:
        return "weak"
    return "critical"


def health_indicator(
    categories: list[dict[str, Any]],
    current_cash: float,
) -> dict[str, Any]:
    """Weighted cash-coverage score (0–100) for the included, non-zero
    categories, consumed in priority order."""
    items = [c for c in categories if c.get("included") and c.get("amount", 0.0) > 0]
    if not items:
        return {
            "score": 100.0,
            "rating": "strong",
            "weighted_outgoings": 0.0,
            "note": "No upcoming outgoings recorded.",
        }

    running = max(0.0, current_cash)
    weighted_sum = 0.0
    weight_total = 0.0
    weighted_outgoings = 0.0
    for c in items:   # already in short-term-first priority order
        amount = c["amount"]
        weight = category_weight(c["category"])
        paid = min(amount, running)
        coverage = paid / amount if amount else 1.0
        running -= paid
        weighted_sum += weight * coverage
        weight_total += weight
        weighted_outgoings += weight * amount

    # items is non-empty here (the no-outgoings case returned above), so every
    # entry contributed a positive weight — weight_total is always > 0.
    score = round(weighted_sum / weight_total * 100, 1)
    return {
        "score": score,
        "rating": _rating(score),
        "weighted_outgoings": round(weighted_outgoings, 2),
        "note": (
            "Higher = better placed to meet short-term outgoings. "
            "Not a guarantee against future cash-flow issues."
        ),
    }
