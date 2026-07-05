"""Bank reconciliation calculator — counts unreconciled + finds last reconciled
from the BankTransactions IsReconciled flag."""
from __future__ import annotations

from app.modules.insights.logic.bank import compute_bank_reconciliation


def _txn(date_ms, reconciled, total="100.00"):
    return {
        "Date": f"/Date({date_ms}+0000)/",
        "IsReconciled": "true" if reconciled else "false",
        "Total": total,
    }


def test_counts_unreconciled_and_last_reconciled():
    txns = [
        _txn(1700000000000, True, "50.00"),     # reconciled (older)
        _txn(1710000000000, True, "75.00"),     # reconciled (newest reconciled)
        _txn(1720000000000, False, "200.00"),   # unreconciled
        _txn(1725000000000, False, "30.00"),    # unreconciled (newest overall)
    ]
    out = compute_bank_reconciliation(txns)
    assert out["total_transactions"] == 4
    assert out["unreconciled_count"] == 2
    assert out["unreconciled_value"] == 230.0
    # newest reconciled, not the newest overall
    assert out["last_reconciled_date"] == "2024-03-09"
    # most recent of any transaction
    assert out["most_recent_transaction"] == "2024-08-30"


def test_all_reconciled():
    out = compute_bank_reconciliation([_txn(1700000000000, True)])
    assert out["unreconciled_count"] == 0
    assert out["unreconciled_value"] == 0.0
    assert out["last_reconciled_date"] is not None


def test_extra_dates_extend_most_recent():
    # An invoice dated later than any bank txn should win most_recent.
    out = compute_bank_reconciliation(
        [_txn(1700000000000, True)],
        extra_dates=["2030-01-15"],
    )
    assert out["most_recent_transaction"] == "2030-01-15"


def test_empty():
    out = compute_bank_reconciliation(None)
    assert out["total_transactions"] == 0
    assert out["unreconciled_count"] == 0
    assert out["last_reconciled_date"] is None
