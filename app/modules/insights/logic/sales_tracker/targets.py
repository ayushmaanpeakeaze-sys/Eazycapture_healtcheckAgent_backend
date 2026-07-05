"""Resolve a monthly sales TARGET per period from the actual-sales history,
using the configured strategy. Returns one target per period (the chart's green
line); ``None`` where there isn't enough history yet (or "No Target").
"""
from __future__ import annotations

from typing import Optional

from app.modules.insights.logic.sales_tracker.config import (
    BASIS_AVERAGE_3,
    BASIS_AVERAGE_6,
    BASIS_AVERAGE_12,
    BASIS_MANUAL,
    BASIS_NONE,
    BASIS_PREVIOUS_MONTH,
    BASIS_SAME_MONTH_LAST_YEAR,
    BASIS_XERO_BUDGET,
    SalesTargetConfig,
)

_AVERAGE_WINDOW = {BASIS_AVERAGE_3: 3, BASIS_AVERAGE_6: 6, BASIS_AVERAGE_12: 12}


def _adjust(value: Optional[float], pct: float) -> Optional[float]:
    if value is None:
        return None
    return round(value * (1 + pct / 100.0), 2)


def compute_targets(
    sales: list[float],
    config: SalesTargetConfig,
    budget_values: Optional[list[float]] = None,
) -> list[Optional[float]]:
    """One target per period in ``sales`` (oldest → newest). The adjustment %
    applies to every basis EXCEPT the Xero budget (already an explicit figure).
    A basis that needs more history than we have yields ``None`` for the early
    periods (e.g. same-month-last-year needs 12 prior months)."""
    n = len(sales)
    if n == 0 or config.basis == BASIS_NONE:
        return [None] * n

    if config.basis == BASIS_MANUAL:
        value = _adjust(config.manual_value, config.adjustment_pct)
        return [value] * n

    if config.basis == BASIS_XERO_BUDGET:
        # Budget figures come per-period from Xero — already the explicit target,
        # so no %-adjust. A period with no budget → None.
        budget = budget_values or []
        return [
            round(budget[i], 2) if i < len(budget) and budget[i] is not None else None
            for i in range(n)
        ]

    targets: list[Optional[float]] = []
    for i in range(n):
        if config.basis == BASIS_PREVIOUS_MONTH:
            base = sales[i - 1] if i >= 1 else None
        elif config.basis == BASIS_SAME_MONTH_LAST_YEAR:
            base = sales[i - 12] if i >= 12 else None
        elif config.basis in _AVERAGE_WINDOW:
            window = sales[max(0, i - _AVERAGE_WINDOW[config.basis]):i]
            base = round(sum(window) / len(window), 2) if window else None
        else:
            base = None
        targets.append(_adjust(base, config.adjustment_pct))
    return targets
