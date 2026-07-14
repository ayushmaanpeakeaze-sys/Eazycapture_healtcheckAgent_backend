"""Prepayment review (description-based, pure deterministic) — the SOP engine.

A P&L EXPENSE line whose description shows a service period extending beyond the
financial year-end: the portion after year-end may be a prepayment. An explicit
date range wins; else a period keyword (annual / subscription / insurance …)
implies a 12-month term from the transaction date. Straight-line estimate is
guidance only — review-only, never auto-posts.
"""
from datetime import date
from decimal import Decimal

from app.shared.transaction import BatchLineItem, BatchTransaction
from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.checks.prepayments import _find_prepayments

# 429 = P&L expense; 710 = FIXED; 200 = REVENUE.
_TYPES = {"429": "EXPENSE", "710": "FIXED", "200": "REVENUE"}
_NAMES = {"429": "General Expenses", "710": "Computer Equipment", "200": "Sales"}


def _tx(tid, d, desc, amt, *, code="429"):
    return BatchTransaction(
        transaction_id=tid, date=d, description=desc, amount=Decimal(str(amt)),
        vendor_name="Vendor", type="ACCPAY", contact_id="C1", current_account_code=code,
    )


def test_explicit_range_flags_3_months_300():
    # SOP Example 1: "01 Apr 2025 – 31 Mar 2026", year-end 31 Dec 2025 → Jan-Mar = 3.
    hits = _find_prepayments(
        [_tx("1", date(2025, 4, 1), "Microsoft subscription 01 Apr 2025 - 31 Mar 2026", 1200)],
        _NAMES, _TYPES)
    assert len(hits) == 1
    mr = hits[0].match_reasons
    assert hits[0].issue_type == "prepayment_review"
    assert mr["period_end"] == "2026-03-31"
    assert mr["months_after_year_end"] == 3
    assert mr["prepaid_estimate"] == "300.00"
    assert mr["recommended_action"] == "prepay_future_portion"


def test_annual_keyword_infers_12_months_6_after():
    # SOP Example 2: "Annual subscription", tx 15 Jul 2025, year-end 31 Dec → 6 months.
    hits = _find_prepayments(
        [_tx("1", date(2025, 7, 15), "Annual software subscription", 1200)], _NAMES, _TYPES)
    assert len(hits) == 1
    mr = hits[0].match_reasons
    assert mr["months_after_year_end"] == 6
    assert mr["prepaid_estimate"] == "600.00"


def test_month_year_range():
    # "Apr 2025 to Mar 2026" (month-only tokens; end bumps to 31 Mar) → 3 months.
    hits = _find_prepayments(
        [_tx("1", date(2025, 4, 1), "Support Apr 2025 to Mar 2026", 1200)], _NAMES, _TYPES)
    assert len(hits) == 1
    assert hits[0].match_reasons["months_after_year_end"] == 3


def test_insurance_keyword_flags():
    hits = _find_prepayments(
        [_tx("1", date(2025, 9, 1), "Annual insurance premium", 3000)], _NAMES, _TYPES)
    assert len(hits) == 1


def test_no_period_signal_ignored():
    assert _find_prepayments(
        [_tx("1", date(2025, 6, 1), "Office supplies", 1200)], _NAMES, _TYPES) == []


def test_period_within_year_ignored():
    # Bought early enough that the annual term ends <1 month after year-end.
    assert _find_prepayments(
        [_tx("1", date(2025, 1, 1), "Annual subscription", 1200)], _NAMES, _TYPES) == []


def test_range_fully_within_year_ignored():
    # Period ends before year-end → nothing to prepay.
    assert _find_prepayments(
        [_tx("1", date(2025, 2, 1), "Cover 01 Feb 2025 - 31 May 2025", 1200)], _NAMES, _TYPES) == []


def test_below_threshold_ignored():
    assert _find_prepayments(
        [_tx("1", date(2025, 7, 1), "Annual subscription", 100)], _NAMES, _TYPES) == []


def test_non_expense_account_ignored():
    # A capital-looking FIXED account or a revenue account is out of scope.
    assert _find_prepayments(
        [_tx("1", date(2025, 7, 1), "Annual subscription", 1200, code="710")], _NAMES, _TYPES) == []
    assert _find_prepayments(
        [_tx("1", date(2025, 7, 1), "Annual subscription", 1200, code="200")], _NAMES, _TYPES) == []


def test_year_end_override_from_settings():
    # March year-end: an annual sub bought 1 Jun 2025 runs to 1 Jun 2026, past the
    # 31 Mar 2026 year-end → ~2 months after.
    s = AuditSettings.from_config({"financial_year_end_month": 3, "financial_year_end_day": 31})
    hits = _find_prepayments([_tx("1", date(2025, 6, 1), "Annual licence", 1200)], _NAMES, _TYPES, s)
    assert len(hits) == 1
    assert hits[0].match_reasons["year_end"] == "2026-03-31"


def test_threshold_setting_respected():
    s = AuditSettings.from_config({"prepayment_min_amount": "2000"})
    assert len(_find_prepayments([_tx("1", date(2025, 7, 1), "Annual subscription", 2500)], _NAMES, _TYPES, s)) == 1
    assert _find_prepayments([_tx("1", date(2025, 7, 1), "Annual subscription", 1500)], _NAMES, _TYPES, s) == []


def test_period_on_line_description_flags():
    # Xero writes the description on the LINE ITEM, not the document — a period
    # there must still be caught (was doc-description-only before).
    tx = BatchTransaction(
        transaction_id="L1", date=date(2025, 7, 15), description="bill",
        amount=Decimal("1200"), vendor_name="Aviva", type="ACCPAY", contact_id="C1",
        line_items=[BatchLineItem(
            account_code="429", amount=Decimal("1200"),
            description="Annual software subscription")],
    )
    hits = _find_prepayments([tx], _NAMES, _TYPES)
    assert len(hits) == 1
    assert hits[0].match_reasons["line_no"] == 1
    assert hits[0].match_reasons["months_after_year_end"] == 6
    assert hits[0].match_reasons["prepaid_estimate"] == "600.00"
