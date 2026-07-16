"""Missing accruals (pattern-based) — SOP examples locked."""
from datetime import date
from decimal import Decimal

from app.shared.transaction import BatchTransaction
from app.modules.healthcheck.checks.missing_accrual import find_missing_accruals

_COA = {"445": "Light & Heat", "469": "Rent", "433": "Insurance"}
_TYPES = {"445": "OVERHEADS", "469": "OVERHEADS", "433": "OVERHEADS", "620": "CURRENT"}


def _tx(code, y, m, amt="1000"):
    return BatchTransaction(
        transaction_id=f"{code}-{y}-{m}", date=date(y, m, 1), description="x",
        amount=Decimal(amt), vendor_name="V", type="ACCPAY", current_account_code=code)


def _year(code, present_months):  # months as (year, month) tuples
    return [_tx(code, y, m) for (y, m) in present_months]


def test_final_month_missing_is_high():
    # Light & Heat present Apr 2025 – Feb 2026 (11 months), March (final) missing.
    txns = _year("445", [(2025, m) for m in range(4, 13)] + [(2026, 1), (2026, 2)])
    f = find_missing_accruals(txns, _COA, _TYPES, date(2026, 3, 31))
    assert len(f) == 1
    assert f[0]["reason"] == "final_month_missing"
    assert f[0]["severity"] == "high"
    assert f[0]["missing_month"] == "Mar 2026"
    assert f[0]["avg_monthly_amount"] == "1000.00"


def test_post_year_cutoff():
    # Final month empty + a payment after year-end → accrue prior month.
    txns = _year("469", [(2025, m) for m in range(4, 13)] + [(2026, 1), (2026, 2)])
    txns += [_tx("469", 2026, 4)]   # April payment (post year-end)
    f = find_missing_accruals(txns, _COA, _TYPES, date(2026, 3, 31))
    assert len(f) == 1
    assert f[0]["reason"] == "post_year_cutoff"
    assert f[0]["post_year_payment"] is True
    assert f[0]["severity"] == "high"


def test_interim_month_missing_is_medium():
    # Insurance present all except December.
    txns = _year("433", [(2025, m) for m in (4, 5, 6, 7, 8, 9, 10, 11)] + [(2026, 1), (2026, 2), (2026, 3)])
    f = find_missing_accruals(txns, _COA, _TYPES, date(2026, 3, 31))
    assert len(f) == 1
    assert f[0]["reason"] == "missing_month"
    assert f[0]["severity"] == "medium"
    assert f[0]["missing_month"] == "Dec 2025"


def test_irregular_account_not_flagged():
    # Only 3 months of activity → not a regular monthly account.
    assert find_missing_accruals(_year("445", [(2025, 4), (2025, 8), (2025, 12)]),
                                 _COA, _TYPES, date(2026, 3, 31)) == []


def test_balance_sheet_account_ignored():
    # A Prepayments (CURRENT) account is not a P&L expense — never an accrual.
    txns = _year("620", [(2025, m) for m in range(4, 13)] + [(2026, 1), (2026, 2)])
    assert find_missing_accruals(txns, {"620": "Prepayments"}, _TYPES, date(2026, 3, 31)) == []
