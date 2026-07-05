"""Profitability KPI calculator — computes gross/net from Xero P&L section
totals (Xero's API doesn't emit Gross/Net rows)."""
from __future__ import annotations

from app.modules.insights.logic.profitability import compute_profitability


def _report():
    return {
        "ReportName": "Profit and Loss",
        "Rows": [
            {"RowType": "Header", "Cells": [{"Value": ""}, {"Value": "Apr 2026"}, {"Value": "May 2026"}]},
            {"RowType": "Section", "Title": "Income", "Rows": [
                {"RowType": "Row", "Cells": [{"Value": "Sales"}, {"Value": "1000.00"}, {"Value": "1200.00"}]},
                {"RowType": "SummaryRow", "Cells": [{"Value": "Total Income"}, {"Value": "1000.00"}, {"Value": "1200.00"}]},
            ]},
            {"RowType": "Section", "Title": "Less Cost of Sales", "Rows": [
                {"RowType": "SummaryRow", "Cells": [{"Value": "Total Cost of Sales"}, {"Value": "400.00"}, {"Value": "500.00"}]},
            ]},
            {"RowType": "Section", "Title": "Less Operating Expenses", "Rows": [
                {"RowType": "SummaryRow", "Cells": [{"Value": "Total Operating Expenses"}, {"Value": "200.00"}, {"Value": "250.00"}]},
            ]},
        ],
    }


def test_computes_gross_and_net_from_sections():
    out = compute_profitability(_report())
    assert out["periods"] == ["Apr 2026", "May 2026"]
    assert out["series"]["sales"] == [1000.0, 1200.0]
    assert out["series"]["gross_profit"] == [600.0, 700.0]      # income - cogs
    assert out["series"]["net_profit"] == [400.0, 450.0]        # gross - opex
    assert out["totals"] == {"sales": 2200.0, "gross_profit": 1300.0, "net_profit": 850.0}


def test_no_cost_of_sales_gross_equals_sales():
    rep = {
        "Rows": [
            {"RowType": "Header", "Cells": [{"Value": ""}, {"Value": "Jan"}]},
            {"RowType": "Section", "Title": "Income", "Rows": [
                {"RowType": "SummaryRow", "Cells": [{"Value": "Total Income"}, {"Value": "500.00"}]},
            ]},
            {"RowType": "Section", "Title": "Less Operating Expenses", "Rows": [
                {"RowType": "SummaryRow", "Cells": [{"Value": "Total Operating Expenses"}, {"Value": "120.00"}]},
            ]},
        ],
    }
    out = compute_profitability(rep)
    assert out["series"]["gross_profit"] == [500.0]   # no COGS
    assert out["series"]["net_profit"] == [380.0]


def test_other_income_and_loss_month():
    rep = {
        "Rows": [
            {"RowType": "Header", "Cells": [{"Value": ""}, {"Value": "Q1"}]},
            {"RowType": "Section", "Title": "Income", "Rows": [
                {"RowType": "SummaryRow", "Cells": [{"Value": "Total Income"}, {"Value": "100.00"}]},
            ]},
            {"RowType": "Section", "Title": "Less Operating Expenses", "Rows": [
                {"RowType": "SummaryRow", "Cells": [{"Value": "Total Operating Expenses"}, {"Value": "300.00"}]},
            ]},
            {"RowType": "Section", "Title": "Plus Other Income", "Rows": [
                {"RowType": "SummaryRow", "Cells": [{"Value": "Total Other Income"}, {"Value": "50.00"}]},
            ]},
        ],
    }
    out = compute_profitability(rep)
    assert out["series"]["net_profit"] == [-150.0]   # 100 - 300 + 50


def test_empty_report():
    assert compute_profitability(None)["series"]["sales"] == []
    assert compute_profitability({})["series"]["net_profit"] == []
