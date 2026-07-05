"""Default-based Unexpected Account (check #25).

When per-contact default accounts are supplied, a posting that differs from the
contact's saved default is flagged (and the default is the suggested fix). With
no defaults configured, the check is SILENT (per the spec) — there is no baseline
to compare against, and frequency-based detection is the separate Multi-Account
Suppliers check.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.shared.transaction import (
    BatchContext,
    BatchHealthCheckRequest,
    BatchTransaction,
    ContactDefault,
)
from app.modules.healthcheck.engine import run_batch_health_check
from app.modules.healthcheck.checks.coding import _find_unexpected_accounts
from app.modules.healthcheck.checks.tax import _find_unexpected_tax_codes

_COA = {"400": "Rent", "401": "Travel", "200": "Sales"}


def _tx(tid, cid, acct, doc="ACCPAY", vendor="Acme"):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description="x",
        amount=Decimal("100"), vendor_name=vendor, type=doc,
        contact_id=cid, current_account_code=acct,
    )


# defaults map: contact_id -> {"sales": code, "purchase": code}
_DEFAULTS = {"C1": {"sales": "200", "purchase": "400"}}


def test_purchase_differs_from_default_flagged():
    hits = _find_unexpected_accounts([_tx("1", "C1", "401")], _COA, _DEFAULTS)
    assert len(hits) == 1
    assert hits[0].issue_type == "unexpected_account"
    assert hits[0].current_code == "401"
    assert hits[0].suggested_code == "400"   # the contact's default


def test_posting_matching_default_not_flagged():
    assert _find_unexpected_accounts([_tx("1", "C1", "400")], _COA, _DEFAULTS) == []


def test_contact_without_default_is_silent():
    # C2 has no entry → no flag even though the account "looks" odd.
    assert _find_unexpected_accounts([_tx("1", "C2", "401")], _COA, _DEFAULTS) == []


def test_sales_uses_sales_default():
    hits = _find_unexpected_accounts(
        [_tx("1", "C1", "999", doc="ACCREC")], _COA, _DEFAULTS,
    )
    assert hits and hits[0].suggested_code == "200"


def test_blank_direction_default_silent():
    # contact has a purchase default but no sales default → sales txn silent.
    defaults = {"C1": {"sales": None, "purchase": "400"}}
    assert _find_unexpected_accounts(
        [_tx("1", "C1", "999", doc="ACCREC")], _COA, defaults,
    ) == []


def test_silent_without_defaults():
    # Per the spec: no defaults configured → nothing is "unexpected", even on a
    # large, heavily-dominated batch (that's the Multi-Account Suppliers check).
    txns = [_tx(str(i), "C1", "400") for i in range(120)]
    txns.append(_tx("odd", "C1", "401"))   # used once
    assert _find_unexpected_accounts(txns, _COA) == []          # no defaults arg
    assert _find_unexpected_accounts(txns, _COA, {}) == []      # empty map


async def test_default_based_end_to_end():
    ctx = BatchContext(
        chart_of_accounts=[],
        contact_defaults=[ContactDefault(contact_id="C1", purchase_account="400")],
    )
    req = BatchHealthCheckRequest(
        transactions=[_tx("1", "C1", "401")], context=ctx,
        disabled_rules=["wrong_category", "capital_item_review",
                        "low_cost_fixed_asset", "anomaly", "amount_outlier"],
    )
    res = await run_batch_health_check(req)
    flags = [f for f in res.flagged if f.issue_type == "unexpected_account"]
    assert flags and flags[0].suggested_code == "400"


# ============================ Bank transactions (Money In / Out) ============

def test_money_in_receive_uses_sales_default():
    # Bank RECEIVE (money in) → compared against the SALES default.
    hits = _find_unexpected_accounts([_tx("1", "C1", "999", doc="RECEIVE")], _COA, _DEFAULTS)
    assert hits and hits[0].suggested_code == "200"   # sales default


def test_money_out_spend_uses_purchase_default():
    # Bank SPEND (money out) → compared against the PURCHASE default.
    hits = _find_unexpected_accounts([_tx("1", "C1", "999", doc="SPEND")], _COA, _DEFAULTS)
    assert hits and hits[0].suggested_code == "400"   # purchase default


def test_money_in_matching_sales_default_not_flagged():
    assert _find_unexpected_accounts([_tx("1", "C1", "200", doc="RECEIVE")], _COA, _DEFAULTS) == []


def test_money_out_tax_uses_purchase_tax_default():
    hits = _find_unexpected_tax_codes([_txt("1", "C1", "NONE", doc="SPEND")], _TAX_DEFAULTS)
    assert hits and hits[0].suggested_code == "INPUT2"


def test_money_in_tax_uses_sales_tax_default():
    hits = _find_unexpected_tax_codes([_txt("1", "C1", "NONE", doc="RECEIVE")], _TAX_DEFAULTS)
    assert hits and hits[0].suggested_code == "OUTPUT2"


async def test_bank_txns_reach_unexpected_but_not_duplicates():
    # Two RECEIVE bank txns sharing an invoice number WOULD be a 1.0 duplicate
    # if they reached the duplicate engine — but bank txns are scoped OUT of
    # duplicate detection, so only unexpected_account fires.
    def _bank(tid, acct):
        return BatchTransaction(
            transaction_id=tid, date=date(2026, 1, 1), description="x",
            amount=Decimal("100"), vendor_name="Acme", type="RECEIVE",
            contact_id="C1", current_account_code=acct, invoice_number="INV-9",
        )
    ctx = BatchContext(
        chart_of_accounts=[],
        contact_defaults=[ContactDefault(contact_id="C1", sales_account="200")],
    )
    req = BatchHealthCheckRequest(
        transactions=[_bank("b1", "260"), _bank("b2", "260")], context=ctx,
        disabled_rules=["wrong_category", "capital_item_review",
                        "low_cost_fixed_asset", "anomaly", "amount_outlier"],
    )
    res = await run_batch_health_check(req)
    types = {f.issue_type for f in res.flagged}
    assert "unexpected_account" in types          # bank txns DID reach the check
    assert "duplicate_invoice" not in types        # but NOT duplicate detection
    assert "duplicate_bill" not in types


# ============================ Unexpected TAX (default-based) ================

def _txt(tid, cid, tax, doc="ACCPAY", vendor="Acme"):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description="x",
        amount=Decimal("100"), vendor_name=vendor, type=doc,
        contact_id=cid, tax_code=tax,
    )


# defaults map carries tax too: contact_id -> {sales, purchase, sales_tax, purchase_tax}
_TAX_DEFAULTS = {"C1": {"sales": None, "purchase": None,
                        "sales_tax": "OUTPUT2", "purchase_tax": "INPUT2"}}


def test_purchase_tax_differs_from_default_flagged():
    hits = _find_unexpected_tax_codes([_txt("1", "C1", "NONE")], _TAX_DEFAULTS)
    assert len(hits) == 1
    assert hits[0].issue_type == "unexpected_tax_code"
    assert hits[0].current_code == "NONE"
    assert hits[0].suggested_code == "INPUT2"   # contact's default purchase tax


def test_matching_tax_default_not_flagged():
    assert _find_unexpected_tax_codes([_txt("1", "C1", "INPUT2")], _TAX_DEFAULTS) == []


def test_sales_uses_sales_tax_default():
    hits = _find_unexpected_tax_codes(
        [_txt("1", "C1", "ZERORATED", doc="ACCREC")], _TAX_DEFAULTS,
    )
    assert hits and hits[0].suggested_code == "OUTPUT2"


def test_contact_without_tax_default_silent():
    assert _find_unexpected_tax_codes([_txt("1", "C2", "NONE")], _TAX_DEFAULTS) == []


def test_tax_silent_without_defaults():
    # Same rule for tax codes: no default tax configured → silent.
    txns = [_txt(str(i), "C1", "INPUT2") for i in range(120)]
    txns.append(_txt("odd", "C1", "NONE"))   # used once
    assert _find_unexpected_tax_codes(txns) == []
    assert _find_unexpected_tax_codes(txns, {}) == []


async def test_unexpected_tax_default_based_end_to_end():
    ctx = BatchContext(
        chart_of_accounts=[],
        contact_defaults=[ContactDefault(contact_id="C1", purchase_tax="INPUT2")],
    )
    req = BatchHealthCheckRequest(
        transactions=[_txt("1", "C1", "NONE")], context=ctx,
        disabled_rules=["wrong_category", "capital_item_review",
                        "low_cost_fixed_asset", "anomaly", "amount_outlier",
                        "purchase_tax_missing", "sales_tax_missing"],
    )
    res = await run_batch_health_check(req)
    flags = [f for f in res.flagged if f.issue_type == "unexpected_tax_code"]
    assert flags and flags[0].suggested_code == "INPUT2"
