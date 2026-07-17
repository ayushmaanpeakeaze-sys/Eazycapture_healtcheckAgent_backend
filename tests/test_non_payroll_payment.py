"""Bank payments to people not in payroll (SOP)."""
from datetime import date
from decimal import Decimal

from app.shared.transaction import BatchTransaction
from app.modules.healthcheck.checks.payroll import find_non_payroll_payments


def _spend(tid, payee, amt=1000, typ="SPEND"):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description="Payment",
        amount=Decimal(str(amt)), vendor_name=payee, type=typ,
        contact_id=tid, current_account_code="477")


def test_payee_not_in_payroll_flagged():
    f = find_non_payroll_payments([_spend("t1", "Amit Kumar")], ["Rajesh Sharma"])
    assert len(f) == 1
    assert f[0].issue_type == "non_payroll_payment"
    assert f[0].match_reasons["payee"] == "Amit Kumar"


def test_payee_in_payroll_not_flagged():
    assert find_non_payroll_payments([_spend("t1", "Amit Kumar")], ["Amit Kumar"]) == []


def test_match_is_case_and_space_insensitive():
    assert find_non_payroll_payments([_spend("t1", "  amit   KUMAR ")], ["Amit Kumar"]) == []


def test_no_payroll_list_is_silent():
    assert find_non_payroll_payments([_spend("t1", "Amit Kumar")], []) == []


def test_only_spend_scanned():
    assert find_non_payroll_payments([_spend("t1", "Amit Kumar", typ="RECEIVE")], ["X"]) == []


def test_supplier_payee_also_flagged():
    f = find_non_payroll_payments([_spend("t1", "Acme Ltd")], ["Amit Kumar"])
    assert len(f) == 1 and f[0].match_reasons["payee"] == "Acme Ltd"
