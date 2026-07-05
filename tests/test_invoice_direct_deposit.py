"""Invoice-or-Direct-Deposit: an unpaid customer invoice (ACCREC) matched with a
direct RECEIVE bank deposit from the same customer (same contact, within a date
window, same amount). Sales-side mirror of Bill-or-Direct-Payment. POSSIBLE
mismatch — AR / profit overstated if the invoice is left falsely unpaid.
"""
from datetime import date
from decimal import Decimal

from app.shared.transaction import BatchTransaction
from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.checks.bank import _find_invoice_direct_deposits


def _inv(tid, cid, due, d=date(2026, 6, 1), amount=None, desc="Consulting - INV-100"):
    amount = amount if amount is not None else due
    return BatchTransaction(
        transaction_id=tid, date=d, description=desc, amount=Decimal(str(amount)),
        vendor_name="ABC Ltd", type="ACCREC", contact_id=cid, status="AUTHORISED",
        amount_due=Decimal(str(due)), amount_paid=Decimal("0"),
    )


def _deposit(tid, cid, amt, d=date(2026, 6, 10), desc="Direct deposit"):
    return BatchTransaction(
        transaction_id=tid, date=d, description=desc, amount=Decimal(str(amt)),
        vendor_name="ABC Ltd", type="RECEIVE", contact_id=cid,
    )


def test_unpaid_invoice_with_matching_deposit_flagged():
    hits = _find_invoice_direct_deposits([_inv("I1", "C1", "10000")], [_deposit("D1", "C1", "10000")])
    assert len(hits) == 1
    mr = hits[0].match_reasons
    assert hits[0].issue_type == "invoice_direct_deposit"
    # the unpaid INVOICE row
    assert mr["invoice_transaction_id"] == "I1"
    assert mr["invoice_date"] == "2026-06-01"
    assert mr["invoice_amount"] == "10000.00"
    assert mr["invoice_description"] == "Consulting - INV-100"
    # the matching DEPOSIT row
    assert mr["deposit_transaction_id"] == "D1"
    assert mr["deposit_date"] == "2026-06-10"
    assert mr["deposit_amount"] == "10000.00"
    assert mr["deposit_description"] == "Direct deposit"
    assert mr["days_apart"] == 9


def test_different_contact_not_flagged():
    assert _find_invoice_direct_deposits([_inv("I1", "C1", "10000")], [_deposit("D1", "C2", "10000")]) == []


def test_outside_window_not_flagged():
    dep = _deposit("D1", "C1", "10000", d=date(2026, 7, 11))   # 40 days after
    assert _find_invoice_direct_deposits([_inv("I1", "C1", "10000")], [dep]) == []


def test_different_amount_not_flagged():
    assert _find_invoice_direct_deposits([_inv("I1", "C1", "10000")], [_deposit("D1", "C1", "9000")]) == []


def test_paid_invoice_not_flagged():
    assert _find_invoice_direct_deposits([_inv("I1", "C1", "0", amount=10000)], [_deposit("D1", "C1", "10000")]) == []


def test_window_setting_extends_match():
    dep = _deposit("D1", "C1", "10000", d=date(2026, 7, 11))
    s = AuditSettings.from_config({"invoice_direct_window_days": 60})
    assert len(_find_invoice_direct_deposits([_inv("I1", "C1", "10000")], [dep], s)) == 1


def test_spend_money_out_never_matches_invoice():
    # An invoice is RECEIVE-only — a SPEND (money out) must NOT match it.
    spend = BatchTransaction(
        transaction_id="S1", date=date(2026, 6, 10), description="x",
        amount=Decimal("10000"), vendor_name="ABC Ltd", type="SPEND", contact_id="C1",
    )
    assert _find_invoice_direct_deposits([_inv("I1", "C1", "10000")], [spend]) == []
