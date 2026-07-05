"""Review fixes: per-contact grouping (FIX 1) + name-based opening balance (FIX 2)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.schemas.transaction import BatchLineItem, BatchTransaction
from app.modules.healthcheck.checks.coding import _find_multi_account_suppliers
from app.modules.healthcheck.checks.bank import _find_opening_balance_differences
from app.modules.healthcheck.checks.tax import _find_multi_tax_code_suppliers
from app.services.healthcheck.deterministic import (
    _build_contact_alias,
    _contact_key,
)

_COA = {"420": "Telephone", "421": "Travel", "9999": "Opening Balance Conversion"}


def _tx(tid, cid, acct, vendor="Acme"):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description="x",
        amount=Decimal("100"), vendor_name=vendor, type="ACCPAY",
        contact_id=cid, current_account_code=acct,
    )


# --- FIX 1: _contact_key ----------------------------------------------------

def test_contact_key_uses_contact_id_not_name():
    a = _tx("1", "C1", "420", vendor="Acme")
    b = _tx("2", "C1", "420", vendor="Acme Ltd")   # different name, same contact
    assert _contact_key(a) == _contact_key(b)


def test_contact_key_falls_back_to_name_when_no_contact_id():
    a = BatchTransaction(transaction_id="1", date=date(2026, 1, 1), description="x",
                         amount=Decimal("1"), vendor_name="Acme", type="ACCPAY")
    assert _contact_key(a) == "name:acme"


def test_contact_key_merges_via_alias():
    alias = _build_contact_alias([["C1", "C2"]])
    a = _tx("1", "C1", "420")
    b = _tx("2", "C2", "420")
    assert _contact_key(a, alias) == _contact_key(b, alias)


# --- FIX 1: supplier check groups by ContactID ------------------------------

def test_multi_account_groups_by_contact_despite_name_drift():
    # 4 bills from ONE contact (names vary), 3 to 420, 1 outlier to 421.
    txns = [
        _tx("1", "C1", "420", "Acme"), _tx("2", "C1", "420", "Acme Ltd"),
        _tx("3", "C1", "420", "Acme"), _tx("4", "C1", "421", "Acme Ltd"),
    ]
    hits = _find_multi_account_suppliers(txns, _COA)
    assert len(hits) == 1 and hits[0].current_code == "421"


def test_multi_account_cross_contact_merge():
    alias = _build_contact_alias([["C1", "C2"]])
    # C1 only ever uses 420; C2 only ever uses 421.
    txns = [_tx("1", "C1", "420"), _tx("2", "C2", "421")]
    # merged → one contact spans 420 + 421 → flagged
    assert len(_find_multi_account_suppliers(txns, _COA, alias)) == 1
    # un-merged → each contact uses a single account → nothing
    assert _find_multi_account_suppliers(txns, _COA, None) == []


def test_multi_account_includes_money_out():
    # The check covers Money Out too: a bill on 420 + a SPEND to the same supplier
    # on 421 → two distinct accounts → flagged.
    spend = BatchTransaction(
        transaction_id="2", date=date(2026, 1, 1), description="x",
        amount=Decimal("50"), vendor_name="Acme", type="SPEND",
        contact_id="C1", current_account_code="421",
    )
    hits = _find_multi_account_suppliers([_tx("1", "C1", "420"), spend], _COA)
    assert len(hits) == 1


def test_multi_account_reads_all_line_items():
    # Account code is per-LINE: one bill whose two lines hit two accounts is
    # itself multi-account (the old first-line-only logic missed this).
    tx = BatchTransaction(
        transaction_id="1", date=date(2026, 1, 1), description="x",
        amount=Decimal("100"), vendor_name="Acme", type="ACCPAY", contact_id="C1",
        line_items=[
            BatchLineItem(account_code="420", amount=Decimal("60")),
            BatchLineItem(account_code="421", amount=Decimal("40")),
        ],
    )
    assert len(_find_multi_account_suppliers([tx], _COA)) == 1


# --- Multi-tax-code suppliers (2+ distinct tax codes) ------------------------

def _tx_tax(tid, cid, tax, dtype="ACCPAY"):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description="x",
        amount=Decimal("100"), vendor_name="Acme", type=dtype,
        contact_id=cid, current_account_code="400", tax_code=tax,
    )


def test_multi_tax_pure_two_distinct():
    # One supplier, two distinct tax codes → flagged (the differing one).
    hits = _find_multi_tax_code_suppliers([_tx_tax("1", "C1", "20I"), _tx_tax("2", "C1", "NONE")])
    assert len(hits) == 1


def test_multi_tax_single_code_not_flagged():
    assert _find_multi_tax_code_suppliers([_tx_tax("1", "C1", "20I"), _tx_tax("2", "C1", "20I")]) == []


def test_multi_tax_includes_money_out():
    # A bill on 20I + a SPEND (Money Out) on NONE for the same supplier → flagged.
    txns = [_tx_tax("1", "C1", "20I"), _tx_tax("2", "C1", "NONE", dtype="SPEND")]
    assert len(_find_multi_tax_code_suppliers(txns)) == 1


def test_multi_tax_reads_all_line_items():
    # Tax lives per-LINE: one bill with two lines on two tax codes → multi-tax.
    tx = BatchTransaction(
        transaction_id="1", date=date(2026, 1, 1), description="x",
        amount=Decimal("100"), vendor_name="Acme", type="ACCPAY", contact_id="C1",
        line_items=[
            BatchLineItem(tax_code="20I", amount=Decimal("60")),
            BatchLineItem(tax_code="NONE", amount=Decimal("40")),
        ],
    )
    assert len(_find_multi_tax_code_suppliers([tx])) == 1


# --- FIX 2: opening balance matched by NAME (not just code 840) -------------

def test_opening_balance_matched_by_name():
    txns = [_tx("1", "C1", "9999")]   # code 9999, name "Opening Balance Conversion"
    hits = _find_opening_balance_differences(txns, _COA)
    assert len(hits) == 1
    assert hits[0].issue_type == "opening_balance_difference"


def test_opening_balance_still_matches_configured_840():
    txns = [_tx("1", "C1", "840")]
    hits = _find_opening_balance_differences(txns, {"840": "Historical Adjustment"})
    assert len(hits) == 1
