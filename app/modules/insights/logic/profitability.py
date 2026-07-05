"""Profitability KPI — turn a Xero ProfitAndLoss report into a monthly
Sales Income / Gross Profit / Net Profit series.

Pure logic: no DB, no HTTP. Takes the already-fetched Xero report dict (a
single entry from the API's ``Reports`` array) and returns chart-ready data.

Xero's API P&L does NOT emit computed "Gross Profit" / "Net Profit" rows — it
gives per-section SummaryRows (Total Income, Total Cost of Sales, Total
Operating Expenses, ...). So we read the section totals and compute:

    gross_profit = income - cost_of_sales
    net_profit   = gross_profit - other_expenses + other_income

Sections are classified by their Title prefix the way Xero formats them:
"Income"/"…Income" → income, "Less …" → an expense (Cost of Sales handled
separately for gross), "Plus …" → other income.
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


def _empty() -> dict[str, Any]:
    return {
        "periods": [],
        "series": {"sales": [], "gross_profit": [], "net_profit": []},
        "totals": {"sales": 0.0, "gross_profit": 0.0, "net_profit": 0.0},
    }


def _section_total(section: dict, n: int) -> list[float]:
    """The per-period values from a section's SummaryRow (Total …)."""
    for sub in section.get("Rows") or []:
        if isinstance(sub, dict) and sub.get("RowType") == "SummaryRow":
            cells = sub.get("Cells") or []
            return [float(_num(c.get("Value"))) for c in cells[1:1 + n]]
    return [0.0] * n


def compute_profitability(report: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Return ``{periods, series:{sales,gross_profit,net_profit}, totals}``."""
    if not isinstance(report, dict):
        return _empty()
    rows = report.get("Rows") or []

    # Period column labels come from the Header row (skip cell 0 = label column).
    periods: list[str] = []
    for r in rows:
        if isinstance(r, dict) and r.get("RowType") == "Header":
            cells = r.get("Cells") or []
            periods = [(c.get("Value") or "").strip() for c in cells[1:]]
            break
    n = len(periods)
    if n == 0:
        return _empty()

    zeros = [0.0] * n
    income = list(zeros)
    cogs = list(zeros)
    other_exp = list(zeros)
    other_inc = list(zeros)

    def _add(acc: list[float], vals: list[float]) -> list[float]:
        return [a + b for a, b in zip(acc, vals)]

    for r in rows:
        if not isinstance(r, dict) or r.get("RowType") != "Section":
            continue
        title = (r.get("Title") or "").strip().lower()
        vals = _section_total(r, n)
        if title.startswith("less "):
            if "cost of sales" in title:
                cogs = _add(cogs, vals)
            else:                       # operating expenses, other expenses, depreciation…
                other_exp = _add(other_exp, vals)
        elif title.startswith("plus "):
            other_inc = _add(other_inc, vals)
        elif "income" in title or "revenue" in title or "turnover" in title:
            income = _add(income, vals)
        # else: skip unknown / already-computed sections

    gross = [s - c for s, c in zip(income, cogs)]
    net = [g - e + i for g, e, i in zip(gross, other_exp, other_inc)]

    def _r(xs: list[float]) -> list[float]:
        return [round(x, 2) for x in xs]

    sales, gross, net = _r(income), _r(gross), _r(net)
    return {
        "periods": periods,
        "series": {"sales": sales, "gross_profit": gross, "net_profit": net},
        "totals": {
            "sales": round(sum(sales), 2),
            "gross_profit": round(sum(gross), 2),
            "net_profit": round(sum(net), 2),
        },
    }
