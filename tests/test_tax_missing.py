"""Sales / Purchase Tax Missing.

Rule: VAT-registered org + a line on an in-scope account (Sales/Other-Income for
sales, Expense/Asset for purchase) coded No-VAT / Outside-Scope → flag for review.
Account-TYPE driven, per line, across bills/invoices AND Money In / Money Out.
Zero-rated / exempt are deliberate 0% treatments → NOT flagged. Purchase ignores
no-VAT-by-nature accounts (wages, depreciation, tax, donations…).
"""
from datetime import date
from decimal import Decimal

from app.schemas.transaction import BatchTransaction
from app.modules.healthcheck.checks.tax import (
    _find_purchase_tax_missing,
    _find_sales_tax_missing,
)

_COA = {
    "200": "Sales", "210": "Interest Income", "400": "Office Expenses",
    "477": "Wages & Salaries", "710": "Computer Equipment",
}
_TYPES = {
    "200": "REVENUE", "210": "OTHERINCOME", "400": "EXPENSE",
    "477": "EXPENSE", "710": "FIXEDASSET",
}


def _tx(tid, acct, tax, dtype="ACCREC"):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description="x",
        amount=Decimal("100"), vendor_name="Acme", type=dtype,
        current_account_code=acct, tax_code=tax, contact_id="C1",
    )


# --- sales -----------------------------------------------------------------

def test_sales_income_no_vat_flagged():
    hits = _find_sales_tax_missing([_tx("1", "200", "NONE")], _COA, _TYPES)
    assert [h.issue_type for h in hits] == ["sales_tax_missing"]


def test_sales_income_with_vat_not_flagged():
    assert _find_sales_tax_missing([_tx("1", "200", "OUTPUT2")], _COA, _TYPES) == []


def test_sales_expense_line_not_flagged():
    # An expense-type line is out of scope for the SALES check.
    assert _find_sales_tax_missing([_tx("1", "400", "NONE")], _COA, _TYPES) == []


def test_sales_money_in_flagged():
    # Money In (RECEIVE) to Other-Income with No VAT (e.g. interest received).
    hits = _find_sales_tax_missing([_tx("1", "210", "NONE", dtype="RECEIVE")], _COA, _TYPES)
    assert len(hits) == 1


# --- purchase --------------------------------------------------------------

def test_purchase_expense_no_vat_flagged():
    hits = _find_purchase_tax_missing([_tx("1", "400", "NONE", dtype="ACCPAY")], _COA, _TYPES)
    assert [h.issue_type for h in hits] == ["purchase_tax_missing"]


def test_purchase_fixed_asset_no_vat_flagged():
    # Computer Equipment with No VAT → input VAT probably reclaimable → flag.
    hits = _find_purchase_tax_missing([_tx("1", "710", "NONE", dtype="ACCPAY")], _COA, _TYPES)
    assert len(hits) == 1


def test_purchase_wages_ignored_by_nature():
    # Wages legitimately carry no VAT → suppressed by the by-nature name ignore.
    assert _find_purchase_tax_missing([_tx("1", "477", "NONE", dtype="ACCPAY")], _COA, _TYPES) == []


def test_purchase_money_out_flagged():
    hits = _find_purchase_tax_missing([_tx("1", "400", "NONE", dtype="SPEND")], _COA, _TYPES)
    assert len(hits) == 1


# --- tax-code semantics ----------------------------------------------------

def test_zero_rated_and_exempt_not_flagged():
    # Deliberate 0% treatments with a real code — NOT "missing".
    assert _find_purchase_tax_missing([_tx("1", "400", "ZERORATED", dtype="ACCPAY")], _COA, _TYPES) == []
    assert _find_purchase_tax_missing([_tx("1", "400", "EXEMPTEXPENSES", dtype="ACCPAY")], _COA, _TYPES) == []


def test_outside_scope_flagged():
    hits = _find_purchase_tax_missing([_tx("1", "400", "Outside The Scope Of VAT", dtype="ACCPAY")], _COA, _TYPES)
    assert len(hits) == 1
