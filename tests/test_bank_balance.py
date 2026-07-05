"""Bank Balance Check (check #30).

Per bank account, flag where the statement balance (Xero BankSummary closing)
differs from the GL balance (Xero TrialBalance) by more than a tolerance. Pure
compute — the live BankSummary/TrialBalance fetch is integration glue validated
against a real connection separately.
"""
from __future__ import annotations

from decimal import Decimal

from app.services.healthcheck.audit_settings import AuditSettings
from app.modules.insights.logic.bank import (
    compute_bank_balance,
    compute_bank_balance_gaps,
)


def _acct_id(code):
    """Deterministic fake accountID GUID derived from the code (real reports
    join on the accountID in cell Attributes, not the code)."""
    return f"00000000-0000-0000-0000-{int(code):012d}"


def _tb_report(rows):
    """Synthetic Xero TrialBalance (real shape): Cells = [name(code), Debit,
    Credit, DebitBal, CreditBal]; accountID GUID in cell0 Attributes (Id='account')."""
    return {"Rows": [{
        "RowType": "Section",
        "Rows": [
            {"RowType": "Row", "Cells": [
                {"Value": f"{name} ({code})",
                 "Attributes": [{"Id": "account", "Value": _acct_id(code)}]},
                {"Value": ""}, {"Value": ""},
                {"Value": str(debit)}, {"Value": str(credit)},
            ]}
            for (name, code, debit, credit) in rows
        ],
    }]}


def _bs_report(rows):
    """Synthetic Xero BankSummary (real shape): Cells = [name, Opening, In, Out,
    Closing] — NO code in label; accountID GUID in cell0 Attributes (Id='accountID').
    Plus a trailing Total SummaryRow that must be skipped."""
    return {"Rows": [{
        "RowType": "Section",
        "Rows": [
            {"RowType": "Row", "Cells": [
                {"Value": name,
                 "Attributes": [{"Id": "accountID", "Value": _acct_id(code)}]},
                {"Value": "0"}, {"Value": "0"}, {"Value": "0"},
                {"Value": str(closing)},
            ]}
            for (name, code, closing) in rows
        ] + [
            {"RowType": "SummaryRow", "Cells": [
                {"Value": "Total"}, {"Value": "0"}, {"Value": "0"},
                {"Value": "0"}, {"Value": "0"},
            ]}
        ],
    }]}


def _acc(code, name, statement, gl, unrec=None):
    return {
        "code": code, "name": name,
        "statement_balance": statement, "gl_balance": gl,
        "unreconciled_count": unrec,
    }


def test_gap_above_tolerance_flagged():
    rows = compute_bank_balance_gaps([_acc("090", "Current", 10000, 9400, unrec=3)])
    assert len(rows) == 1
    r = rows[0]
    assert r["account_code"] == "090"
    assert r["gap"] == 600.0
    assert r["unreconciled_count"] == 3   # our root-cause extra


def test_matching_balances_not_flagged():
    assert compute_bank_balance_gaps([_acc("090", "Current", 10000, 10000)]) == []


def test_within_tolerance_not_flagged():
    # 0.005 rounding gap < default 0.01 tolerance.
    assert compute_bank_balance_gaps([_acc("090", "Current", 100.005, 100.0)]) == []


def test_tolerance_override():
    rows = [_acc("090", "Current", 10005, 10000)]   # £5 gap
    assert compute_bank_balance_gaps(rows) != []                              # default 0.01
    assert compute_bank_balance_gaps(rows, tolerance=Decimal("10")) == []     # £10 tol


def test_excluded_account_skipped():
    rows = [_acc("091", "Director Personal", 5000, 0)]
    assert compute_bank_balance_gaps(rows) != []
    assert compute_bank_balance_gaps(rows, exclude_codes={"091"}) == []


def test_negative_gap_and_sort_order():
    rows = compute_bank_balance_gaps([
        _acc("090", "Current", 100, 200),     # gap -100
        _acc("091", "Savings", 5000, 4000),   # gap +1000
    ])
    # largest absolute gap first
    assert [r["account_code"] for r in rows] == ["091", "090"]
    assert rows[1]["gap"] == -100.0


def test_settings_coercion_for_bank_fields():
    s = AuditSettings.from_config({
        "bank_balance_tolerance": "5.00",
        "bank_exclude_accounts": ["091", "092"],
    })
    assert s.bank_balance_tolerance == Decimal("5.00")
    assert s.bank_exclude_accounts == ("091", "092")


def test_empty_or_malformed_input_safe():
    assert compute_bank_balance_gaps(None) == []
    assert compute_bank_balance_gaps([{"not": "an account"}]) == []


# --- report-join orchestrator (parsers + compute) ------------------------

def test_compute_bank_balance_joins_reports():
    # Bank account 090: statement 10,000 (closing) vs GL 9,400 (debit-credit) → gap 600.
    tb = _tb_report([
        ("Business Current", "090", 9400, 0),
        ("Sales", "200", 0, 50000),   # non-bank account ignored (not in BankSummary)
    ])
    bs = _bs_report([("Business Current", "090", 10000)])
    aid = _acct_id("090")
    bank_txns = [
        {"IsReconciled": False, "BankAccount": {"AccountID": aid}},
        {"IsReconciled": False, "BankAccount": {"AccountID": aid}},
        {"IsReconciled": True, "BankAccount": {"AccountID": aid}},
    ]
    res = compute_bank_balance(tb, bs, bank_txns)
    assert res["accounts_checked"] == 1
    assert res["gap_count"] == 1
    gap = res["gaps"][0]
    assert gap["account_code"] == "090"
    assert gap["gap"] == 600.0
    assert gap["unreconciled_count"] == 2   # root-cause hint


def test_compute_bank_balance_no_gap_when_matched():
    tb = _tb_report([("Current", "090", 10000, 0)])
    bs = _bs_report([("Current", "090", 10000)])
    res = compute_bank_balance(tb, bs, [])
    assert res["gap_count"] == 0


def test_compute_bank_balance_failopen_on_missing_reports():
    res = compute_bank_balance(None, None, None)
    assert res == {"accounts_checked": 0, "gap_count": 0, "gaps": []}
