"""Sales Tracker orchestrator — assembles config + targets + chart + current-
month analysis into the single payload the Insights snapshot stores and the
dashboard renders. This is the only module callers import (via the package).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from app.modules.insights.logic.profitability import compute_profitability
from app.modules.insights.logic.sales_tracker.analysis import current_month_analysis
from app.modules.insights.logic.sales_tracker.chart import build_chart
from app.modules.insights.logic.sales_tracker.config import parse_config
from app.modules.insights.logic.sales_tracker.targets import compute_targets


def compute_sales_tracker(
    report: Optional[dict[str, Any]],
    config_raw: Optional[dict[str, Any]] = None,
    as_of: Optional[date] = None,
    budget_values: Optional[list[float]] = None,
) -> dict[str, Any]:
    """Full Sales Tracker payload.

    * ``report``       — the Xero P&L (same input as profitability).
    * ``config_raw``   — stored sales-target settings
                         (``company.audit_config['sales_target']``).
    * ``as_of``        — today (defaults to now) for the current-month day maths.
    * ``budget_values``— per-period Xero budget figures (only used when the basis
                         is ``xero_budget``; wired by the snapshot when available).
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc).date()
    config = parse_config(config_raw)

    pnl = compute_profitability(report)
    periods: list[str] = pnl["periods"]
    sales: list[float] = pnl["series"]["sales"]

    targets = compute_targets(sales, config, budget_values=budget_values)

    current_period = periods[-1] if periods else None
    current_actual = sales[-1] if sales else 0.0
    current_target = targets[-1] if targets else None

    # Per-period variance rows (kept so existing consumers keep working).
    rows = []
    for period, actual, target in zip(periods, sales, targets):
        variance = round(actual - target, 2) if target is not None else None
        rows.append({
            "period": period,
            "actual": actual,
            "target": target,
            "variance": variance,
            "variance_pct": round(variance / target * 100, 1) if target else None,
            "met_target": (actual >= target) if target is not None else None,
        })

    return {
        "target_basis": config.basis,
        "adjustment_pct": config.adjustment_pct,
        "chart": build_chart(periods, sales, targets),
        "current_month": current_month_analysis(
            current_period, current_actual, current_target, as_of,
        ),
        "total_sales": pnl["totals"]["sales"],
        # legacy-compatible flat fields (older consumers)
        "periods": periods,
        "actual": sales,
        "target": current_target,
        "rows": rows,
    }
