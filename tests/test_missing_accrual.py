"""Missing accruals (pattern-based) — SOP examples locked."""
from datetime import date
from decimal import Decimal

from app.shared.transaction import BatchTransaction
from app.modules.healthcheck.checks.missing_accrual import find_missing_accruals

YE = date(2026, 3, 31)


def _tx(code, y, m, amt="1000"):
    return BatchTransaction(
        transaction_id=f"{code}-{y}-{m}-{amt}", date=date(y, m, 1), description="x",
        amount=Decimal(amt), vendor_name="V", type="ACCPAY", current_account_code=code)


def _year(code, present_months):  # months as (year, month) tuples
    return [_tx(code, y, m) for (y, m) in present_months]


def test_final_month_missing_is_high():
    # Light & Heat present Apr 2025 – Feb 2026 (11 months), March (final) missing.
    txns = _year("445", [(2025, m) for m in range(4, 13)] + [(2026, 1), (2026, 2)])
    f = find_missing_accruals(txns, {"445": "Light & Heat"}, {"445": "OVERHEADS"}, YE)
    assert len(f) == 1
    assert f[0]["reason"] == "final_month_missing"
    assert f[0]["severity"] == "high"
    assert f[0]["missing_month"] == "Mar 2026"
    assert f[0]["avg_monthly_amount"] == "1000.00"


def test_post_year_cutoff():
    # Final month empty + a payment after year-end → accrue prior month.
    txns = _year("469", [(2025, m) for m in range(4, 13)] + [(2026, 1), (2026, 2)])
    txns += [_tx("469", 2026, 4)]   # April payment (post year-end)
    f = find_missing_accruals(txns, {"469": "Rent"}, {"469": "OVERHEADS"}, YE)
    assert len(f) == 1
    assert f[0]["reason"] == "post_year_cutoff"
    assert f[0]["post_year_payment"] is True
    assert f[0]["severity"] == "high"


def test_interim_month_missing_is_medium():
    # Insurance present all except December.
    txns = _year("433", [(2025, m) for m in (4, 5, 6, 7, 8, 9, 10, 11)] + [(2026, 1), (2026, 2), (2026, 3)])
    f = find_missing_accruals(txns, {"433": "Insurance"}, {"433": "OVERHEADS"}, YE)
    assert len(f) == 1
    assert f[0]["reason"] == "missing_month"
    assert f[0]["severity"] == "medium"
    assert f[0]["missing_month"] == "Dec 2025"


def test_consecutive_gap_is_large_gap_high():
    txns = _year("445", [(2025, m) for m in (4, 5, 6, 7, 8)] + [(2025, 12), (2026, 1), (2026, 2), (2026, 3)])
    f = find_missing_accruals(txns, {"445": "Light & Heat"}, {"445": "OVERHEADS"}, YE)
    assert len(f) == 1
    assert f[0]["reason"] == "large_gap"
    assert f[0]["severity"] == "high"
    assert f[0]["missing_month"] == "Sep 2025 – Nov 2025"


def test_predefined_regular_flags_sparse_final_month():
    # Telephone is a predefined regular account: even with only Apr 2025 present,
    # the empty final month is flagged (SOP Part 3 — alone enough).
    f = find_missing_accruals(_year("489", [(2025, 4)]),
                              {"489": "Telephone & Internet"}, {"489": "OVERHEADS"}, YE)
    assert len(f) == 1
    assert f[0]["reason"] == "final_month_missing"
    assert f[0]["months_present"] == 1


def test_predefined_dormant_account_flags_final_month():
    # Rent is predefined; with no activity at all the final month is still flagged.
    f = find_missing_accruals([], {"469": "Rent"}, {"469": "OVERHEADS"}, YE)
    assert len(f) == 1
    assert f[0]["reason"] == "final_month_missing"
    assert f[0]["months_present"] == 0


def test_irregular_non_predefined_account_not_flagged():
    # A non-predefined account with only 3 months → not regular, never flagged.
    f = find_missing_accruals(_year("412", [(2025, 4), (2025, 8), (2025, 12)]),
                              {"412": "Consultancy Fees"}, {"412": "OVERHEADS"}, YE)
    assert f == []


def test_opening_reversal_does_not_mask_final_month():
    txns = _year("445", [(2025, m) for m in range(4, 13)] + [(2026, 1), (2026, 2)])
    txns.append(_tx("445", 2026, 3, amt="-1000"))
    f = find_missing_accruals(txns, {"445": "Light & Heat"}, {"445": "OVERHEADS"}, YE)
    assert len(f) == 1
    assert f[0]["reason"] == "final_month_missing"


def test_reversal_does_not_inflate_regularity():
    # 7 real months + a reversal in an 8th, on a NON-predefined account → not regular.
    txns = _year("412", [(2025, m) for m in range(4, 11)])
    txns.append(_tx("412", 2025, 11, amt="-500"))
    assert find_missing_accruals(txns, {"412": "Consultancy Fees"}, {"412": "OVERHEADS"}, YE) == []


def test_post_year_reversal_is_not_cutoff():
    txns = _year("469", [(2025, m) for m in range(4, 13)] + [(2026, 1), (2026, 2)])
    txns.append(_tx("469", 2026, 4, amt="-1000"))
    f = find_missing_accruals(txns, {"469": "Rent"}, {"469": "OVERHEADS"}, YE)
    assert len(f) == 1
    assert f[0]["reason"] == "final_month_missing"
    assert f[0]["post_year_payment"] is False


def test_balance_sheet_account_ignored():
    # A Prepayments (CURRENT) account is not a P&L expense — never an accrual.
    txns = _year("620", [(2025, m) for m in range(4, 13)] + [(2026, 1), (2026, 2)])
    assert find_missing_accruals(txns, {"620": "Prepayments"}, {"620": "CURRENT"}, YE) == []
