"""Cash Health Check orchestrator — assembles the current cash, the weighted
health indicator, the "Enough cash to pay?" checklist and the recent cash
movements into the single payload the Insights snapshot stores and the
dashboard renders. This is the only module callers import (via the package).
"""
from __future__ import annotations

from typing import Any, Optional

from app.modules.insights.logic.cash_health.accounts import extract_cash_and_liabilities
from app.modules.insights.logic.cash_health.config import parse_config
from app.modules.insights.logic.cash_health.indicator import health_indicator
from app.modules.insights.logic.cash_health.movements import compute_movements
from app.modules.insights.logic.cash_health.outgoings import build_outgoings


def compute_cash_health(
    chart_of_accounts: Optional[list[dict[str, Any]]],
    trial_balance: Optional[dict[str, Any]],
    balance_sheet_periods: Optional[dict[str, Any]] = None,
    corp_tax_estimate: float = 0.0,
    config_raw: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Full Cash Health Check payload.

    * ``chart_of_accounts``    — Xero COA (account Type/Class for classification)
    * ``trial_balance``        — Xero TrialBalance (account balances)
    * ``balance_sheet_periods``— multi-period (MONTH) BalanceSheet for movements
    * ``corp_tax_estimate``    — upcoming corporation-tax bill (folded into the
                                 Corporation Tax category)
    * ``config_raw``           — stored settings
                                 (``company.audit_config['cash_health']``)
    """
    config = parse_config(config_raw)

    parsed = extract_cash_and_liabilities(chart_of_accounts, trial_balance, config)
    current_cash = parsed["current_cash"]

    outgoings = build_outgoings(
        parsed["liabilities"], current_cash, config, corp_tax_estimate,
    )
    indicator = health_indicator(outgoings["categories"], current_cash)
    movements = compute_movements(balance_sheet_periods)

    return {
        "current_cash": current_cash,
        "health_score": indicator["score"],
        "rating": indicator["rating"],
        "bank_accounts": parsed["bank_accounts"],
        "outgoings": outgoings,
        "recent_movements": movements["movements"],
        "movement_points": movements["points"],
        "indicator": indicator,
    }
