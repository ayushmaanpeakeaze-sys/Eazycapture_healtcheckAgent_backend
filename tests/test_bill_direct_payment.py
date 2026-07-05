"""Bill-or-Direct-Payment: an unpaid supplier bill matched with a direct SPEND
bank payment to the same supplier (same contact, within a date window, same
amount). Surfaced as a POSSIBLE mismatch, not a confirmed error.
"""
from datetime import date
from decimal import Decimal

from app.schemas.transaction import BatchTransaction
from app.services.healthcheck.audit_settings import AuditSettings
from app.modules.healthcheck.checks.bank import _find_bill_direct_payments


def _bill(tid, cid, due, d=date(2026, 6, 1), amount=None, desc="Cleaning - Bill #B100"):
    amount = amount if amount is not None else due
    return BatchTransaction(
        transaction_id=tid, date=d, description=desc, amount=Decimal(str(amount)),
        vendor_name="ABC Cleaning", type="ACCPAY", contact_id=cid, status="AUTHORISED",
        amount_due=Decimal(str(due)), amount_paid=Decimal("0"),
    )


def _spend(tid, cid, amt, d=date(2026, 6, 10), desc="Direct payment - cleaning"):
    return BatchTransaction(
        transaction_id=tid, date=d, description=desc, amount=Decimal(str(amt)),
        vendor_name="ABC Cleaning", type="SPEND", contact_id=cid,
    )


def test_unpaid_bill_with_matching_spend_flagged():
    hits = _find_bill_direct_payments([_bill("B1", "C1", "10000")], [_spend("P1", "C1", "10000")])
    assert len(hits) == 1
    mr = hits[0].match_reasons
    assert hits[0].issue_type == "bill_direct_payment"
    # the UNPAID BILL row
    assert mr["bill_transaction_id"] == "B1"
    assert mr["bill_date"] == "2026-06-01"
    assert mr["bill_amount"] == "10000.00"
    assert mr["bill_description"] == "Cleaning - Bill #B100"
    # the DIRECT PAYMENT row
    assert mr["payment_transaction_id"] == "P1"
    assert mr["payment_date"] == "2026-06-10"
    assert mr["payment_amount"] == "10000.00"
    assert mr["payment_description"] == "Direct payment - cleaning"
    assert mr["days_apart"] == 9


def test_different_contact_not_flagged():
    assert _find_bill_direct_payments([_bill("B1", "C1", "10000")], [_spend("P1", "C2", "10000")]) == []


def test_outside_date_window_not_flagged():
    pay = _spend("P1", "C1", "10000", d=date(2026, 7, 11))   # 40 days after bill
    assert _find_bill_direct_payments([_bill("B1", "C1", "10000")], [pay]) == []


def test_different_amount_not_flagged():
    assert _find_bill_direct_payments([_bill("B1", "C1", "10000")], [_spend("P1", "C1", "9000")]) == []


def test_paid_bill_not_flagged():
    # AmountDue 0 → nothing outstanding to mis-pay.
    assert _find_bill_direct_payments([_bill("B1", "C1", "0", amount=10000)], [_spend("P1", "C1", "10000")]) == []


def test_payment_before_bill_not_flagged():
    bill = _bill("B1", "C1", "10000", d=date(2026, 6, 15))
    pay = _spend("P1", "C1", "10000", d=date(2026, 6, 10))   # before the bill
    assert _find_bill_direct_payments([bill], [pay]) == []


def test_window_setting_extends_match():
    pay = _spend("P1", "C1", "10000", d=date(2026, 7, 11))   # 40 days after
    s = AuditSettings.from_config({"bill_direct_window_days": 60})
    assert len(_find_bill_direct_payments([_bill("B1", "C1", "10000")], [pay], s)) == 1


def test_no_bank_txns_silent():
    assert _find_bill_direct_payments([_bill("B1", "C1", "10000")], []) == []
