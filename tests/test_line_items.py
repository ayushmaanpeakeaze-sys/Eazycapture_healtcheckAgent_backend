"""Per-line tax checks — verify the audit examines EVERY line item, not just
line 1, and uses Xero's authoritative CanApplyToExpenses/Revenue flags for
the wrong-direction check (with keyword fallback for legacy/seeded data)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.shared.transaction import (
    BatchContext,
    BatchLineItem,
    BatchTransaction,
    TaxRate,
)
from app.modules.healthcheck.checks.tax import (
    _find_purchase_tax_on_invoices,
    _find_sales_tax_on_bills,
)
from app.modules.healthcheck.engine.deterministic import (
    _inspect_transaction,
)
from app.modules.healthcheck.engine.shared import _allowed_tax_codes, _tax_direction_map

_CTX = BatchContext(tax_rates=[
    TaxRate(code="INPUT2", name="GST on Expenses",
            can_apply_to_expenses=True, can_apply_to_revenue=False),
    TaxRate(code="OUTPUT2", name="GST on Income",
            can_apply_to_expenses=False, can_apply_to_revenue=True),
])


def _bill(line_items, tax_code="INPUT2"):
    return BatchTransaction(
        transaction_id="BILL-1", date=date(2026, 1, 1), description="bill",
        amount=Decimal("1000"), vendor_name="Office Supplies Ltd", type="ACCPAY",
        tax_code=tax_code, current_account_code="420", line_items=line_items,
    )


def _invoice(line_items, tax_code="OUTPUT2"):
    return BatchTransaction(
        transaction_id="INV-1", date=date(2026, 1, 1), description="invoice",
        amount=Decimal("1000"), vendor_name="Acme", type="ACCREC",
        tax_code=tax_code, current_account_code="200", line_items=line_items,
    )


# --- wrong tax direction is caught on a NON-first line ---------------------

def test_sales_tax_on_bills_catches_line_2():
    tx = _bill([
        BatchLineItem(account_code="420", tax_code="INPUT2", amount=Decimal("600")),
        BatchLineItem(account_code="720", tax_code="OUTPUT2", amount=Decimal("400")),
    ])
    hits = _find_sales_tax_on_bills([tx], _tax_direction_map(_CTX))
    assert len(hits) == 1
    assert hits[0].issue_type == "sales_tax_on_bills"
    assert "line 2" in hits[0].message


def test_purchase_tax_on_invoices_catches_line_2():
    tx = _invoice([
        BatchLineItem(account_code="200", tax_code="OUTPUT2", amount=Decimal("600")),
        BatchLineItem(account_code="201", tax_code="INPUT2", amount=Decimal("400")),
    ])
    hits = _find_purchase_tax_on_invoices([tx], _tax_direction_map(_CTX))
    assert len(hits) == 1
    assert hits[0].issue_type == "purchase_tax_on_invoices"
    assert "line 2" in hits[0].message


def test_clean_multiline_bill_not_flagged():
    tx = _bill([
        BatchLineItem(account_code="420", tax_code="INPUT2", amount=Decimal("600")),
        BatchLineItem(account_code="421", tax_code="INPUT2", amount=Decimal("400")),
    ])
    assert _find_sales_tax_on_bills([tx], _tax_direction_map(_CTX)) == []


def test_sales_tax_on_bills_includes_money_out_with_amounts():
    # Money Out (SPEND) with a SALES code → caught (for "Show Bank payments too"),
    # and the Net + Tax amounts land in match_reasons for the UI columns.
    spend = BatchTransaction(
        transaction_id="SP-1", date=date(2026, 1, 1), description="x",
        amount=Decimal("100"), vendor_name="Doggy Treats Ltd", type="SPEND",
        line_items=[BatchLineItem(
            account_code="200", tax_code="OUTPUT2",
            amount=Decimal("100"), tax_amount=Decimal("20"))],
    )
    hits = _find_sales_tax_on_bills([spend], _tax_direction_map(_CTX))
    assert len(hits) == 1
    assert hits[0].match_reasons["tax_code"] == "OUTPUT2"
    assert hits[0].match_reasons["net_amount"] == "100.00"
    assert hits[0].match_reasons["tax_amount"] == "20.00"


# --- missing tax is caught on a NON-first line -----------------------------

def test_missing_tax_caught_on_line_2():
    tx = _bill([
        BatchLineItem(account_code="420", tax_code="INPUT2", amount=Decimal("600")),
        BatchLineItem(account_code="720", tax_code=None, amount=Decimal("400")),
    ])
    issues = _inspect_transaction(tx, _allowed_tax_codes(_CTX), None, date(2026, 6, 1))
    missing = [i for i in issues if i.issue_type == "missing_tax"]
    assert len(missing) == 1
    assert "line 2" in missing[0].message


# --- backward compatibility: no line_items → falls back to flat tax_code ----

def test_fallback_to_flat_tax_code_when_no_line_items():
    # Legacy/seeded shape: no line_items, flat tax_code is an OUTPUT code on a bill.
    tx = _bill([], tax_code="OUTPUT2")
    hits = _find_sales_tax_on_bills([tx], _tax_direction_map(_CTX))
    assert len(hits) == 1
    # No line number when running off the flat field.
    assert "line" not in hits[0].message


def test_keyword_fallback_when_no_tax_context():
    # No TaxRates context (empty map) → direction check falls back to keywords.
    tx = _bill([
        BatchLineItem(account_code="420", tax_code="OUTPUT", amount=Decimal("400")),
    ])
    hits = _find_sales_tax_on_bills([tx], {})   # empty map → keyword path
    assert len(hits) == 1
    assert hits[0].issue_type == "sales_tax_on_bills"


# --- tax direction judged by ACCOUNT, not doc type (refunds/reversals) ------

_COA_TYPE = {"200": "REVENUE", "412": "OVERHEADS", "420": "OVERHEADS"}


def _money_in(acct, tax_code="INPUT2"):
    return BatchTransaction(
        transaction_id="RCV-1", date=date(2026, 1, 1), description="refund",
        amount=Decimal("500"), vendor_name="Kafea terra UK", type="RECEIVE",
        current_account_code=acct,
        line_items=[BatchLineItem(account_code=acct, tax_code=tax_code, amount=Decimal("500"))],
    )


def test_money_in_refund_on_expense_not_flagged():
    # Money-In on an EXPENSE account with input tax = supplier refund/reversal —
    # input tax is correct there → must NOT be flagged (was a false positive).
    tx = _money_in("412", "INPUT2")
    assert _find_purchase_tax_on_invoices([tx], _tax_direction_map(_CTX), _COA_TYPE) == []


def test_money_in_input_tax_on_revenue_still_flagged():
    # A genuine income line (revenue account) with input tax IS wrong → flagged.
    tx = _money_in("200", "INPUT2")
    hits = _find_purchase_tax_on_invoices([tx], _tax_direction_map(_CTX), _COA_TYPE)
    assert len(hits) == 1
    assert hits[0].issue_type == "purchase_tax_on_invoices"


def test_money_out_refund_on_revenue_not_flagged():
    # Money-Out on a REVENUE account with output tax = customer refund — output
    # tax is correct → must NOT be flagged as sales-tax-on-a-bill.
    tx = BatchTransaction(
        transaction_id="SPD-1", date=date(2026, 1, 1), description="refund",
        amount=Decimal("500"), vendor_name="Acme", type="SPEND",
        current_account_code="200",
        line_items=[BatchLineItem(account_code="200", tax_code="OUTPUT2", amount=Decimal("500"))],
    )
    assert _find_sales_tax_on_bills([tx], _tax_direction_map(_CTX), _COA_TYPE) == []
