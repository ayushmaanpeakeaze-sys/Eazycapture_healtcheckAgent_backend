"""Current-month sales analysis — actual-vs-target progress, days remaining, and
a motivational status narrative for the dashboard's right-hand panel.

The sales bar (actual vs target) and the time bar (days elapsed vs days in
month) together tell the user whether they're ahead of, on, or behind pace.
"""
from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Optional

_AHEAD = 10.0   # sales% must lead time% by this much to be "ahead of pace"


def _status_narrative(
    pct_of_target: Optional[float],
    time_pct: float,
    has_target: bool,
    met: bool,
) -> str:
    if not has_target:
        return "No sales target set for this month."
    if met:
        return "Target smashed — you've already hit this month's goal."
    p = pct_of_target or 0.0
    remaining_days_note = "keep it up" if p >= time_pct else "time to push"
    if p >= time_pct + _AHEAD:
        return f"Ahead of pace — sales are running faster than the month ({remaining_days_note})."
    if p >= time_pct - _AHEAD:
        return "On track — sales are keeping pace with the month."
    return "Behind pace — sales need to pick up to reach the target this month."


def current_month_analysis(
    period: Optional[str],
    actual: float,
    target: Optional[float],
    as_of: date,
) -> dict[str, Any]:
    """High-level progress for the current month as of ``as_of`` (today)."""
    days_in_month = calendar.monthrange(as_of.year, as_of.month)[1]
    days_elapsed = min(as_of.day, days_in_month)
    days_remaining = max(0, days_in_month - days_elapsed)
    time_pct = round(days_elapsed / days_in_month * 100, 1)

    has_target = target is not None and target > 0
    pct_of_target = round(actual / target * 100, 1) if has_target else None
    remaining_value = round(max(0.0, target - actual), 2) if has_target else None
    met = bool(has_target and actual >= target)

    return {
        "period": period,
        "actual": round(actual, 2),
        "target": round(target, 2) if target is not None else None,
        "pct_of_target": pct_of_target,
        "remaining_value": remaining_value,
        "days_in_month": days_in_month,
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        # bar fills for the two progress bars (0-100)
        "sales_bar_pct": min(100.0, pct_of_target) if pct_of_target is not None else None,
        "time_bar_pct": time_pct,
        "met_target": met,
        "status": _status_narrative(pct_of_target, time_pct, has_target, met),
    }
