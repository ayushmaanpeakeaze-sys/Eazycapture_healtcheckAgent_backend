"""Unusual payments (deterministic Pattern SOP) — Rule 1 + Rule 4/8."""
from datetime import date
from decimal import Decimal

from app.shared.transaction import BatchTransaction
from app.modules.healthcheck.checks.unusual_payments import find_unusual_payments


def _tx(tid, desc, amt, contact, vendor="V", typ="SPEND"):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description=desc, amount=Decimal(str(amt)),
        vendor_name=vendor, type=typ, contact_id=contact, current_account_code="400")


def test_generic_description_flagged():
    f = find_unusual_payments([_tx("A", "payment", 5000, "c1", vendor="Unknown")])
    assert len(f) == 1
    assert f[0]["reason"] == "unclear_description"


def test_clear_description_not_flagged_for_desc():
    # A normal-frequency supplier with a clear description → nothing.
    txs = [_tx("A", "Office rent", 500, "c3", vendor="Landlord"),
           _tx("B", "Office rent", 500, "c3", vendor="Landlord"),
           _tx("C", "Office rent", 500, "c3", vendor="Landlord")]
    assert find_unusual_payments(txs, large_amount="1000") == []


def test_one_off_large_supplier_flagged():
    f = find_unusual_payments([_tx("D", "Consulting services", 40000, "c9", vendor="Rare Ltd")],
                              large_amount="1000")
    assert len(f) == 1
    assert f[0]["reason"] == "one_off_supplier"
    assert f[0]["amount"] == "40000.00"


def test_large_but_frequent_supplier_not_one_off():
    # Same supplier 3x large payments → not one-off.
    txs = [_tx(f"P{i}", "Monthly service", 5000, "c5", vendor="Regular Co") for i in range(3)]
    assert find_unusual_payments(txs, large_amount="1000", one_off_max_count=2) == []


def test_one_finding_per_payment():
    # A generic-description one-off payment → ONE finding (unclear wins), not two.
    f = find_unusual_payments([_tx("A", "transfer", 9000, "c1", vendor="X")], large_amount="1000")
    assert len(f) == 1
    assert f[0]["reason"] == "unclear_description"


def test_only_payments_scanned():
    # Sales invoice (ACCREC) is not a payment.
    assert find_unusual_payments([_tx("S", "payment", 5000, "c1", typ="ACCREC")]) == []
