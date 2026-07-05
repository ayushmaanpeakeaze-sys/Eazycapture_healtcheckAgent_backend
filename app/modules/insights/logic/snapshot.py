"""Compute a full Insights snapshot for one company.

Fetches the three Xero reports (P&L, Balance Sheet, Trial Balance) and runs all
the KPI calculators. Returns a dict with flat summary fields (for the firm
rollup) + a ``payload`` holding the full per-KPI data (for instant per-org
serve). Network I/O lives here; persistence is the caller's job.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.modules.integrations.service import IntegrationService
from app.modules.insights.logic.balance_sheet import compute_financial_position
from app.modules.insights.logic.bank import compute_bank_balance, compute_bank_reconciliation
from app.modules.insights.logic.cash_health import compute_cash_health
from app.modules.insights.logic.corp_tax import estimate_corporation_tax
from app.modules.insights.logic.directors_loans import find_director_loans
from app.modules.insights.logic.profitability import compute_profitability
from app.modules.insights.logic.sales_tracker import compute_sales_tracker

# how many trailing months of bank balance the Cash Health Check movements show
_MOVEMENT_PERIODS = 4


async def compute_company_snapshot(
    connection_id: str,
    tenant_id: str,
    periods: int = 11,
    sales_target_config: Optional[dict[str, Any]] = None,
    cash_health_config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    integ = IntegrationService()
    pnl, bs, tb, bank_txns, bank_summary, coa, bs_periods = await asyncio.gather(
        integ.fetch_profit_and_loss(connection_id, tenant_id, periods=periods),
        integ.fetch_balance_sheet(connection_id, tenant_id),
        integ.fetch_trial_balance(connection_id, tenant_id),
        integ.fetch_all_bank_transactions(connection_id, tenant_id),
        integ.fetch_bank_summary(connection_id, tenant_id),
        integ.fetch_chart_of_accounts(connection_id, tenant_id),
        integ.fetch_balance_sheet(
            connection_id, tenant_id,
            periods=_MOVEMENT_PERIODS, timeframe="MONTH",
        ),
    )

    # A missing P&L (the core report behind profit and sales) means the fetch
    # failed; raise so the caller keeps the previous snapshot instead of zeros.
    if pnl is None:
        raise RuntimeError(
            "Xero ProfitAndLoss report unavailable — connection may need re-auth"
        )

    profitability = compute_profitability(pnl)
    sales_tracker = compute_sales_tracker(pnl, config_raw=sales_target_config)
    position = compute_financial_position(bs)
    net_profit = profitability["totals"]["net_profit"]
    corp_tax = estimate_corporation_tax(net_profit)
    dla = find_director_loans(tb)
    bank = compute_bank_reconciliation(bank_txns)
    bank_balance = compute_bank_balance(tb, bank_summary, bank_txns)
    cash_health = compute_cash_health(
        chart_of_accounts=coa,
        trial_balance=tb,
        balance_sheet_periods=bs_periods,
        corp_tax_estimate=corp_tax["tax_estimate"],
        config_raw=cash_health_config,
    )

    return {
        # summary (firm rollup)
        "net_profit": net_profit,
        "tax_estimate": corp_tax["tax_estimate"],
        "cash": position["position"]["cash"],
        "cash_coverage": position["cash_health"]["coverage_ratio"],
        "working_capital": position["working_capital"]["working_capital"],
        "working_capital_healthy": position["working_capital"]["healthy"],
        "distributable_reserves": position["dividend"]["distributable_reserves"],
        "net_asset_value": position["valuation"]["net_asset_value"],
        "dla_detected": dla["detected"],
        "dla_overdrawn": any(a.get("overdrawn") for a in dla["accounts"]),
        # full payload (per-org serve)
        "payload": {
            "profitability": profitability,
            "sales_tracker": sales_tracker,
            "financial_position": position,
            "corporation_tax": {
                "period_basis": "trailing 12 months",
                **corp_tax,
            },
            "directors_loans": dla,
            "bank_reconciliation": bank,
            "bank_balance": bank_balance,
            # the full Cash Health Check (distinct from financial_position's
            # balance-sheet "cash_health" liquidity ratio)
            "cash_health_check": cash_health,
        },
    }
