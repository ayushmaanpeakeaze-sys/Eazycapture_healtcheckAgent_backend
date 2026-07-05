"""Misallocated Items — material posting parked in a vague catch-all account.

Deterministic complement to the AI Wrong-Category check (check #29).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.schemas.transaction import BatchLineItem, BatchTransaction
from app.services.healthcheck.audit_settings import AuditSettings
from app.modules.healthcheck.checks.coding import _find_misallocated_items

# 429 is a vague account by NAME; 420 is specific.
_COA = {
    "429": "General Expenses",
    "999": "Suspense Account",
    "420": "Telephone & Internet",
    "500": "Cost of Goods Sold",
}


def _tx(tid, code, amount, lines=None):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description="x",
        amount=Decimal(amount), vendor_name="Acme", type="ACCPAY",
        current_account_code=code, line_items=lines or [],
    )


def test_material_posting_in_vague_account_flagged():
    hits = _find_misallocated_items([_tx("1", "429", "500.00")], _COA)
    assert [f.issue_type for f in hits] == ["misallocated_item"]
    assert hits[0].current_code == "429"


def test_below_materiality_not_flagged():
    # £50 < default £100 materiality → noise, skip.
    assert _find_misallocated_items([_tx("1", "429", "50.00")], _COA) == []


def test_specific_account_not_flagged():
    assert _find_misallocated_items([_tx("1", "420", "5000.00")], _COA) == []


def test_suspense_account_flagged_by_name():
    hits = _find_misallocated_items([_tx("1", "999", "300.00")], _COA)
    assert len(hits) == 1


def test_money_out_flagged():
    # The check covers Money In / Money Out too: a SPEND to a vague account, over
    # the materiality threshold, is just as much a misallocation as a bill line.
    spend = BatchTransaction(
        transaction_id="bt1", date=date(2026, 1, 1), description="Bike supplies",
        amount=Decimal("250.00"), vendor_name="Dave's Bikes", type="SPEND",
        current_account_code="429",
    )
    hits = _find_misallocated_items([spend], _COA)
    assert len(hits) == 1 and hits[0].current_code == "429"


def test_materiality_override_raises_bar():
    txns = [_tx("1", "429", "300.00")]
    assert _find_misallocated_items(txns, _COA) != []          # default £100
    quiet = _find_misallocated_items(
        txns, _COA, AuditSettings.from_config({"misallocated_materiality": "1000"}),
    )
    assert quiet == []


def test_per_client_vague_code_watchlist():
    # 500 (Cost of Goods Sold) isn't vague by name, but a client can watch it.
    txns = [_tx("1", "500", "800.00")]
    assert _find_misallocated_items(txns, _COA) == []          # not vague by default
    hits = _find_misallocated_items(
        txns, _COA, AuditSettings.from_config({"misallocated_vague_codes": ["500"]}),
    )
    assert len(hits) == 1


def test_per_line_vague_account_flagged():
    # Flat field is specific, but a LINE is coded to the vague 429.
    tx = _tx("1", "420", "1000.00", lines=[
        BatchLineItem(account_code="420", amount=Decimal("400.00")),
        BatchLineItem(account_code="429", amount=Decimal("600.00")),
    ])
    hits = _find_misallocated_items([tx], _COA)
    assert len(hits) == 1
    assert hits[0].current_code == "429"


def test_split_across_two_lines_same_vague_account_flags_once():
    tx = _tx("1", "429", "1000.00", lines=[
        BatchLineItem(account_code="429", amount=Decimal("300.00")),
        BatchLineItem(account_code="429", amount=Decimal("400.00")),
    ])
    assert len(_find_misallocated_items([tx], _COA)) == 1
