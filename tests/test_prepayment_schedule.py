"""Prepayment schedule (amortisation working paper) — grid + balance + reconcile.

Locked against the accountant's own worked example (Al Amana / Sultanate of Oman
prepaid govt fees): monthly = amount / term, released from the purchase month, the
carry-forward balance at year-end, and the schedule-vs-ledger validation.
"""
from datetime import date
from decimal import Decimal

from app.modules.healthcheck.checks.prepayment_schedule import build_prepayment_schedule


def _work_permit():
    return {
        "date": date(2025, 11, 4), "invoice_no": "WPPA-4154198",
        "supplier": "Sultanate of Oman", "account_code": "Govt fee",
        "description": "Work Permit Fee 04 Nov 2025 - 04 Feb 2027", "amount": "263.50",
    }


def test_grid_matches_worked_example():
    s = build_prepayment_schedule([_work_permit()], date(2026, 6, 30), months=12)
    assert s["columns"] == ["Jul-25", "Aug-25", "Sep-25", "Oct-25", "Nov-25", "Dec-25",
                            "Jan-26", "Feb-26", "Mar-26", "Apr-26", "May-26", "Jun-26"]
    row = s["rows"][0]
    assert row["total_months"] == 15
    assert row["monthly"] == "17.567"          # 263.50 / 15
    # Jul–Oct blank (before purchase), Nov–Jun released.
    assert row["cells"][:4] == [None, None, None, None]
    assert all(c == "17.567" for c in row["cells"][4:])
    # Carry-forward at 30 Jun 2026 ≈ 7 months left × 17.567 (PDF: 122.967).
    assert abs(Decimal(row["balance"]) - Decimal("122.967")) < Decimal("0.01")


def test_totals_and_reconciliation():
    # ledger_amount = the balance actually in the account for each line (i.e. the
    # accountant has been booking the monthly releases), so the schedule reconciles.
    items = [
        {**_work_permit(), "ledger_amount": "122.967"},
        {"date": date(2025, 11, 24), "supplier": "Sultanate of Oman", "account_code": "Govt fee",
         "description": "Medical fitness fee 24 Nov 2025 - 24 Feb 2027", "amount": "64.00", "ledger_amount": "29.867"},
        {"date": date(2025, 11, 24), "supplier": "Sultanate of Oman", "account_code": "Govt fee",
         "description": "Syed Visa fee 24 Nov 2025 - 24 Feb 2027", "amount": "157.10", "ledger_amount": "73.311"},
        {"date": date(2025, 11, 30), "supplier": "Sultanate of Oman", "account_code": "Govt fee",
         "description": "Syed family Visa fee 30 Nov 2025 - 28 Feb 2027", "amount": "137.00", "ledger_amount": "63.933"},
    ]
    s = build_prepayment_schedule(items, date(2026, 6, 30), months=12)
    # Each released month totals 41.440 across the four lines; Jul–Oct are nil.
    assert s["column_totals"][:4] == ["0.000", "0.000", "0.000", "0.000"]
    assert all(t == "41.440" for t in s["column_totals"][4:])
    # Total carry-forward ≈ PDF 290.078 (rounding within a unit).
    assert abs(Decimal(s["total_balance"]) - Decimal("290.078")) < Decimal("0.01")
    # Ledger (Zoho 290.075) vs schedule (290.078) → reconciles within rounding.
    assert s["validation"]["reconciled"] is True
    assert abs(Decimal(s["validation"]["difference"])) < Decimal("0.01")


def test_unscheduled_item_is_kept_not_dropped():
    # A line with no period ("extension works") can't be amortised, but it still
    # sits in the account — surface it, don't silently drop it.
    s = build_prepayment_schedule(
        [{"date": date(2026, 7, 14), "supplier": "ford", "account_code": "620",
          "description": "extension works", "amount": "30000"}],
        date(2027, 3, 31), months=12)
    row = s["rows"][0]
    assert row["unscheduled"] is True
    assert row["monthly"] is None
    assert row["balance"] == "30000.000"       # whole amount still on the account


def test_ledger_gap_is_flagged():
    # Account holds 530k of postings but the schedule says far less should remain
    # → the difference is the release that was never booked to the P&L.
    s = build_prepayment_schedule(
        [{"date": date(2025, 4, 1), "supplier": "x", "account_code": "620",
          "description": "subscription 01 Apr 2025 - 31 Mar 2026", "amount": "120000",
          "ledger_amount": "120000"}],
        date(2027, 3, 31), months=12)
    # Period fully before this FY → schedule balance 0, but 120k still in ledger.
    assert Decimal(s["validation"]["ledger_balance"]) == Decimal("120000")
    assert Decimal(s["validation"]["schedule_balance"]) == Decimal("0")
    assert s["validation"]["reconciled"] is False


def test_identifies_prepayment_account_by_type_or_name():
    from app.modules.healthcheck.checks.prepayment_schedule import is_prepayment_account
    assert is_prepayment_account("Prepayments", "CURRENT") is True     # by name
    assert is_prepayment_account("Deferred costs", "PREPAYMENT") is True  # by Xero type
    assert is_prepayment_account("General Expenses", "OVERHEADS") is False
    assert is_prepayment_account("Rent", "EXPENSE") is False


def test_collect_only_prepayment_account_lines():
    from app.modules.healthcheck.checks.prepayment_schedule import collect_prepayment_items
    docs = [{
        "Date": "2026-07-15", "Reference": "INV1", "Contact": {"Name": "Aviva"},
        "LineItems": [
            {"AccountCode": "620", "LineAmount": 1200, "Description": "Annual insurance"},
            {"AccountCode": "429", "LineAmount": 500, "Description": "Sundries"},  # expense — skip
        ],
    }]
    items = collect_prepayment_items(docs, {"620": "Prepayments", "429": "General Expenses"},
                                     {"620": "CURRENT", "429": "OVERHEADS"})
    assert len(items) == 1
    assert items[0]["account_code"] == "620"
    assert items[0]["supplier"] == "Aviva"


def test_ledger_balance_override_and_source():
    # When the real Xero balance is supplied it drives the reconciliation.
    items = [{"date": date(2026, 7, 15), "supplier": "Aviva", "account_code": "620",
              "description": "Annual insurance", "amount": "12000"}]
    s = build_prepayment_schedule(items, date(2027, 3, 31), months=12, ledger_balance="9000")
    assert s["validation"]["ledger_balance"] == "9000.000"
    assert s["validation"]["ledger_source"] == "xero_trial_balance"
    # Without it, the ledger side is the posted amount.
    s2 = build_prepayment_schedule(items, date(2027, 3, 31), months=12)
    assert s2["validation"]["ledger_source"] == "posted_amounts"


def test_prepayment_balance_from_trial_balance():
    from decimal import Decimal
    from app.modules.healthcheck.checks.prepayment_schedule import prepayment_balance_from_trial_balance
    parsed = {
        "acc-1": {"code": "620", "balance": Decimal("290.075")},
        "acc-2": {"code": "400", "balance": Decimal("5000")},   # not a prepayment acct
        "acc-3": {"code": "621", "balance": Decimal("100")},
    }
    assert prepayment_balance_from_trial_balance(parsed, {"620", "621"}) == Decimal("390.075")
    assert prepayment_balance_from_trial_balance(parsed, {"620"}) == Decimal("290.075")
