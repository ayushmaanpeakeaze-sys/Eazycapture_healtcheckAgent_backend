"""Balance-Sheet-derived insights (pure logic, no DB/HTTP).

One Xero BalanceSheet report unlocks several KPIs:
  * Financial position  — assets / liabilities / net assets / cash
  * Cash Health         — cash vs short-term liabilities
  * Working Capital     — current assets − current liabilities, current ratio
  * Dividend            — distributable reserves (retained + current-year earnings)
  * Valuation           — net-asset model (= total equity)

Xero's BalanceSheet is a nested Rows tree of section SummaryRows
("Total Bank", "Total Current Assets", "Total Liabilities", "Total Equity", …)
plus equity line items ("Retained Earnings", "Current Year Earnings"). We
flatten it to a label→value map and read the totals we need.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Optional


def _num(v: Any) -> Decimal:
    s = str(v or "").replace(",", "").strip()
    if not s:
        return Decimal("0")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def parse_balance_sheet(report: Optional[dict[str, Any]]) -> dict[str, float]:
    """Flatten every Row/SummaryRow into ``{lowercased label: value}``."""
    out: dict[str, float] = {}

    def _walk(rows: Optional[list]) -> None:
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if r.get("Rows"):
                _walk(r.get("Rows"))
            if r.get("RowType") in ("Row", "SummaryRow"):
                cells = r.get("Cells") or []
                if len(cells) >= 2:
                    label = (cells[0].get("Value") or "").strip().lower()
                    if label:
                        out[label] = float(_num(cells[1].get("Value")))

    if isinstance(report, dict):
        _walk(report.get("Rows"))
    return out


def _r(x: float) -> float:
    return round(x, 2)


def compute_financial_position(report: Optional[dict[str, Any]]) -> dict[str, Any]:
    m = parse_balance_sheet(report)

    def g(*keys: str) -> float:
        for k in keys:
            if k in m:
                return m[k]
        return 0.0

    cash = g("total bank", "total cash and cash equivalents", "total cash")
    current_assets = g("total current assets")
    fixed_assets = g("total fixed assets", "total non-current assets")
    total_assets = g("total assets") or (current_assets + fixed_assets)
    current_liab = g("total current liabilities")
    total_liab = g("total liabilities") or current_liab
    equity = g("total equity")
    retained = g("retained earnings")
    cy_earnings = g("current year earnings")

    net_assets = equity or (total_assets - total_liab)
    working_capital = current_assets - current_liab
    current_ratio = _r(current_assets / current_liab) if current_liab else None
    has_reserve_rows = bool(retained or cy_earnings)
    distributable = (retained + cy_earnings) if has_reserve_rows else equity

    return {
        "position": {
            "total_assets": _r(total_assets),
            "total_liabilities": _r(total_liab),
            "net_assets": _r(net_assets),
            "cash": _r(cash),
            "current_assets": _r(current_assets),
            "fixed_assets": _r(fixed_assets),
            "current_liabilities": _r(current_liab),
        },
        "cash_health": {
            "cash": _r(cash),
            "current_liabilities": _r(current_liab),
            "coverage_ratio": _r(cash / current_liab) if current_liab else None,
            "shortfall": _r(current_liab - cash),
        },
        "working_capital": {
            "current_assets": _r(current_assets),
            "current_liabilities": _r(current_liab),
            "working_capital": _r(working_capital),
            "current_ratio": current_ratio,
            "healthy": working_capital >= 0,
        },
        "dividend": {
            "retained_earnings": _r(retained),
            "current_year_earnings": _r(cy_earnings),
            "distributable_reserves": _r(distributable),
            "basis": (
                "retained earnings + current-year earnings" if has_reserve_rows
                else "total equity (retained/current-year rows not found)"
            ),
        },
        "valuation": {
            "model": "net_asset",
            "net_asset_value": _r(net_assets),
        },
    }
