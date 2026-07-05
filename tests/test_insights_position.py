"""Balance-sheet + sales-tracker insight calculators (pure)."""
from __future__ import annotations

from app.modules.insights.logic.balance_sheet import compute_financial_position
from app.modules.insights.logic.sales_tracker import compute_sales_tracker


def _bs():
    # Mirrors Xero's nested BalanceSheet shape (section SummaryRows + equity rows).
    def row(label, val):
        return {"RowType": "Row", "Cells": [{"Value": label}, {"Value": val}]}

    def summ(label, val):
        return {"RowType": "SummaryRow", "Cells": [{"Value": label}, {"Value": val}]}

    return {"Rows": [
        {"RowType": "Header", "Cells": [{"Value": ""}, {"Value": "31 May 2026"}]},
        {"RowType": "Section", "Title": "Bank", "Rows": [summ("Total Bank", "2000.00")]},
        {"RowType": "Section", "Title": "Current Assets", "Rows": [summ("Total Current Assets", "8000.00")]},
        {"RowType": "Section", "Title": "Fixed Assets", "Rows": [
            summ("Total Fixed Assets", "5000.00"),
            summ("Total Assets", "13000.00"),
        ]},
        {"RowType": "Section", "Title": "Current Liabilities", "Rows": [
            summ("Total Current Liabilities", "6000.00"),
            summ("Total Liabilities", "6000.00"),
        ]},
        {"RowType": "Section", "Title": "Equity", "Rows": [
            row("Retained Earnings", "5000.00"),
            row("Current Year Earnings", "2000.00"),
            summ("Total Equity", "7000.00"),
        ]},
    ]}


def test_financial_position_core():
    d = compute_financial_position(_bs())
    p = d["position"]
    assert p["total_assets"] == 13000.0
    assert p["total_liabilities"] == 6000.0
    assert p["net_assets"] == 7000.0          # equity
    assert p["cash"] == 2000.0


def test_working_capital_and_cash_health():
    d = compute_financial_position(_bs())
    assert d["working_capital"]["working_capital"] == 2000.0   # 8000 - 6000
    assert d["working_capital"]["current_ratio"] == 1.33       # 8000/6000
    assert d["working_capital"]["healthy"] is True
    assert d["cash_health"]["coverage_ratio"] == 0.33          # 2000/6000


def test_dividend_distributable_reserves():
    d = compute_financial_position(_bs())["dividend"]
    assert d["retained_earnings"] == 5000.0
    assert d["current_year_earnings"] == 2000.0
    assert d["distributable_reserves"] == 7000.0               # retained + current-year
    assert "retained" in d["basis"]


def test_valuation_net_asset():
    assert compute_financial_position(_bs())["valuation"]["net_asset_value"] == 7000.0


def test_empty_balance_sheet():
    d = compute_financial_position(None)
    assert d["position"]["total_assets"] == 0.0


# --- Sales tracker ---------------------------------------------------------

def _pnl():
    return {"Rows": [
        {"RowType": "Header", "Cells": [{"Value": ""}, {"Value": "Apr"}, {"Value": "May"}, {"Value": "Jun"}]},
        {"RowType": "Section", "Title": "Income", "Rows": [
            {"RowType": "SummaryRow", "Cells": [{"Value": "Total Income"}, {"Value": "1000"}, {"Value": "2000"}, {"Value": "0"}]},
        ]},
    ]}


def test_sales_tracker_default_average():
    d = compute_sales_tracker(_pnl())
    assert d["actual"] == [1000.0, 2000.0, 0.0]
    assert d["target_basis"] == "average_3"
    # default avg_3: last month's target = avg of the 2 prior months = 1500
    assert d["target"] == 1500.0
    assert d["rows"][1]["met_target"] is True   # 2000 >= 1000 (prev-avg target)


def test_sales_tracker_manual_target():
    d = compute_sales_tracker(_pnl(), config_raw={"basis": "manual", "manual_value": 2500.0})
    assert d["target"] == 2500.0
    assert d["target_basis"] == "manual"
    assert d["rows"][0]["met_target"] is False   # 1000 < 2500
