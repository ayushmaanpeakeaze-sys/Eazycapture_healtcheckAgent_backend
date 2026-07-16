"""Payment Anomalies (deterministic Pattern SOP) — R1, R3, R4, R8."""
from datetime import date
from decimal import Decimal

from app.shared.transaction import BatchTransaction
from app.modules.healthcheck.checks.unusual_payments import find_unusual_payments


def _tx(tid, desc, amt, contact, vendor="V", typ="SPEND", code="400", lines=None):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description=desc, amount=Decimal(str(amt)),
        vendor_name=vendor, type=typ, contact_id=contact, current_account_code=code,
        line_items=lines or [])


def test_recurring_supplier_unclear_description_flagged():
    txs = [_tx(f"P{i}", "payment", 500, "c1", vendor="Utility") for i in range(3)]
    f = find_unusual_payments(txs)
    assert len(f) == 3
    assert {r.match_reasons["reason"] for r in f} == {"unclear_description"}


def test_one_off_unclear_is_unidentified_supplier():
    f = find_unusual_payments([_tx("A", "payment", 80, "c1", vendor="Unknown")])
    assert len(f) == 1
    assert f[0].match_reasons["reason"] == "unidentified_supplier"


def test_clear_description_not_flagged_for_desc():
    txs = [_tx("A", "Office rent", 500, "c3", vendor="Landlord"),
           _tx("B", "Office rent", 500, "c3", vendor="Landlord"),
           _tx("C", "Office rent", 500, "c3", vendor="Landlord")]
    assert find_unusual_payments(txs, large_amount="1000") == []


def test_one_off_large_supplier_flagged():
    f = find_unusual_payments([_tx("D", "Consulting services", 40000, "c9", vendor="Rare Ltd")],
                              large_amount="1000")
    assert len(f) == 1
    assert f[0].match_reasons["reason"] == "one_off_supplier"
    assert f[0].match_reasons["amount"] == "40000.00"


def test_large_but_frequent_supplier_not_one_off():
    txs = [_tx(f"P{i}", "Monthly service", 5000, "c5", vendor="Regular Co") for i in range(3)]
    assert find_unusual_payments(txs, large_amount="1000", one_off_max_count=2) == []


def test_one_finding_per_payment():
    f = find_unusual_payments([_tx("A", "transfer", 9000, "c1", vendor="X")], large_amount="1000")
    assert len(f) == 1
    assert f[0].match_reasons["reason"] == "unidentified_supplier"


def test_only_payments_scanned():
    assert find_unusual_payments([_tx("S", "payment", 5000, "c1", typ="ACCREC")]) == []


def test_large_vs_account_outlier_flagged():
    small = [_tx(f"S{i}", "Stationery", 100, f"c{i}", vendor=f"Shop {i}", code="500")
             for i in range(4)]
    big = [_tx("B0", "Stationery order", 100, "big", vendor="Big Co", code="500"),
           _tx("B1", "Stationery order", 100, "big", vendor="Big Co", code="500"),
           _tx("B2", "Stationery bulk", 5000, "big", vendor="Big Co", code="500")]
    f = find_unusual_payments(small + big, large_amount="1000")
    outliers = [r for r in f if r.match_reasons["reason"] == "large_vs_account"]
    assert len(outliers) == 1
    assert outliers[0].transaction_id == "B2"
    assert outliers[0].match_reasons["account"] == "500"
