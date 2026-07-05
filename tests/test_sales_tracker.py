"""Sales Tracker — unit tests for the modular pieces (config, targets, analysis)."""
from datetime import date

from app.modules.insights.logic.sales_tracker.analysis import current_month_analysis
from app.modules.insights.logic.sales_tracker.config import parse_config
from app.modules.insights.logic.sales_tracker.targets import compute_targets


# --- config -----------------------------------------------------------------

def test_config_defaults():
    c = parse_config(None)
    assert c.basis == "average_3"
    assert c.adjustment_pct == 0.0
    assert c.manual_value is None


def test_config_invalid_basis_falls_back_and_coerces():
    c = parse_config({"basis": "garbage", "adjustment_pct": "5", "manual_value": "1000"})
    assert c.basis == "average_3"
    assert c.adjustment_pct == 5.0
    assert c.manual_value == 1000.0


# --- targets (the 8 strategies) ---------------------------------------------

def test_targets_none():
    assert compute_targets([100, 200, 300], parse_config({"basis": "none"})) == [None, None, None]


def test_targets_manual_with_adjustment():
    t = compute_targets([1, 2, 3], parse_config(
        {"basis": "manual", "manual_value": 200, "adjustment_pct": 10}))
    assert t == [220.0, 220.0, 220.0]


def test_targets_previous_month():
    t = compute_targets([100, 200, 300, 400], parse_config({"basis": "previous_month"}))
    assert t == [None, 100, 200, 300]


def test_targets_average_3():
    t = compute_targets([100, 200, 300, 400, 500], parse_config({"basis": "average_3"}))
    assert t == [None, 100.0, 150.0, 200.0, 300.0]


def test_targets_same_month_last_year_insufficient_history():
    t = compute_targets([100, 200, 300], parse_config({"basis": "same_month_last_year"}))
    assert t == [None, None, None]


def test_targets_xero_budget_uses_provided_values():
    t = compute_targets([1, 2, 3], parse_config({"basis": "xero_budget"}),
                        budget_values=[10, 20, 30])
    assert t == [10.0, 20.0, 30.0]


# --- current-month analysis -------------------------------------------------

def test_analysis_behind_pace():
    a = current_month_analysis("Jun 2026", 200.0, 1000.0, date(2026, 6, 15))
    assert a["pct_of_target"] == 20.0
    assert a["remaining_value"] == 800.0
    assert a["days_in_month"] == 30
    assert a["days_remaining"] == 15
    assert a["met_target"] is False
    assert "Behind" in a["status"]


def test_analysis_target_met():
    a = current_month_analysis("Jun 2026", 1000.0, 1000.0, date(2026, 6, 15))
    assert a["met_target"] is True
    assert "smashed" in a["status"].lower()


def test_analysis_no_target():
    a = current_month_analysis("Jun 2026", 500.0, None, date(2026, 6, 15))
    assert a["pct_of_target"] is None
    assert a["target"] is None
    assert "No sales target" in a["status"]
