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
