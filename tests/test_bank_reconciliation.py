"""Bank Reconciliation Summary — unit tests (pure logic, no infra)."""
from app.modules.healthcheck.engine.bank_reconciliation import (
    compute_bank_reconciliation_summary,
)


def _coa():
    return [
        {"Code": "090", "Name": "Business Bank Account", "Type": "BANK", "AccountID": "id-090"},
        {"Code": "091", "Name": "Savings", "Type": "BANK", "AccountID": "id-091"},
        {"Code": "200", "Name": "Sales", "Type": "REVENUE", "AccountID": "id-200"},  # not a bank
    ]


def _tb_row(name, code, debit_bal, credit_bal, acc_id):
    return {"RowType": "Row", "Cells": [
        {"Value": f"{name} ({code})", "Attributes": [{"Id": "account", "Value": acc_id}]},
        {"Value": "0.00"}, {"Value": "0.00"},
        {"Value": str(debit_bal)}, {"Value": str(credit_bal)},
    ]}


def _tb():
    return {"Rows": [{"RowType": "Section", "Rows": [
        _tb_row("Business Bank Account", "090", "5000", "0", "id-090"),   # +5000
        _tb_row("Savings", "091", "0", "0", "id-091"),                    # 0
    ]}]}


def _txn(acc_id, code, kind, total, reconciled, date, contact):
    return {
        "BankAccount": {"AccountID": acc_id, "Code": code, "Name": "Business Bank Account"},
        "Type": kind, "Total": str(total), "IsReconciled": reconciled,
        "Date": date, "Contact": {"Name": contact},
    }


def _txns():
    return [
        _txn("id-090", "090", "SPEND", "100", "false", "2026-05-01", "Vendor A"),
        _txn("id-090", "090", "SPEND", "50", "false", "2026-05-02", "Vendor B"),
        _txn("id-090", "090", "RECEIVE", "300", "false", "2026-05-03", "Customer X"),
        _txn("id-090", "090", "SPEND", "999", "true", "2026-05-04", "Reconciled"),  # skip
    ]


def test_summary_basic():
    res = compute_bank_reconciliation_summary(_tb(), _coa(), _txns())
    a = {x["account_code"]: x for x in res["accounts"]}["090"]
    assert a["balance_in_xero"] == 5000.0
    assert a["unreconciled_received"] == 300.0
    assert a["unreconciled_spent"] == 150.0          # reconciled 999 NOT counted
    assert a["unreconciled_lines_total"] == 150.0    # 300 - 150
    assert a["unreconciled_count"] == 3
    assert a["needs_reconciliation"] is True


def test_statement_balance_calculated_formula():
    res = compute_bank_reconciliation_summary(_tb(), _coa(), _txns())
    a = {x["account_code"]: x for x in res["accounts"]}["090"]
    # calculated = balance_in_xero + net unreconciled = 5000 + 150
    assert a["statement_balance_calculated"] == 5150.0


def test_voided_transaction_excluded():
    # a voided (cancelled) unreconciled txn must NOT count towards the total
    voided = _txn("id-090", "090", "SPEND", "5000", "false", "2026-05-05", "Voided Vendor")
    voided["Status"] = "VOIDED"
    res = compute_bank_reconciliation_summary(_tb(), _coa(), _txns() + [voided])
    a = {x["account_code"]: x for x in res["accounts"]}["090"]
    assert a["unreconciled_spent"] == 150.0     # 5000 voided NOT added
    assert a["unreconciled_count"] == 3         # voided line not listed


def test_imported_statement_always_none():
    res = compute_bank_reconciliation_summary(_tb(), _coa(), _txns())
    assert all(a["imported_statement_balance"] is None for a in res["accounts"])
    assert res["imported_statement_available"] is False


def test_non_bank_account_ignored():
    codes = {a["account_code"] for a in
             compute_bank_reconciliation_summary(_tb(), _coa(), _txns())["accounts"]}
    assert codes == {"090", "091"}   # Sales (200) excluded


def test_exclude_codes():
    res = compute_bank_reconciliation_summary(_tb(), _coa(), _txns(), exclude_codes={"091"})
    codes = {a["account_code"] for a in res["accounts"]}
    assert "091" not in codes and "090" in codes


def test_lines_have_signed_amounts():
    res = compute_bank_reconciliation_summary(_tb(), _coa(), _txns())
    a = {x["account_code"]: x for x in res["accounts"]}["090"]
    amounts = {(ln["contact"], ln["amount"]) for ln in a["lines"]}
    assert ("Vendor A", -100.0) in amounts    # SPEND → negative
    assert ("Customer X", 300.0) in amounts   # RECEIVE → positive


def test_fully_reconciled_account_not_flagged():
    res = compute_bank_reconciliation_summary(_tb(), _coa(), _txns())
    a = {x["account_code"]: x for x in res["accounts"]}["091"]
    assert a["needs_reconciliation"] is False
    assert a["unreconciled_count"] == 0
    assert a["statement_balance_calculated"] == 0.0


def test_empty_inputs():
    res = compute_bank_reconciliation_summary(None, None, None)
    assert res["accounts"] == []
    assert res["total_unreconciled_count"] == 0
    assert res["imported_statement_available"] is False
