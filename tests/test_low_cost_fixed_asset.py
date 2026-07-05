"""Low-Cost Fixed Asset (pure deterministic): a line posted to a FIXED-ASSET
account for an amount BELOW the capitalisation threshold (low_cost_asset_max,
default £10k) → should likely be expensed. Account TYPE + AMOUNT only — no
contact, no date, no LLM.
"""
from datetime import date
from decimal import Decimal

from app.schemas.transaction import BatchLineItem, BatchTransaction
from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.checks.fixed_assets import _find_low_cost_fixed_assets

_TYPES = {"710": "FIXED", "200": "REVENUE", "400": "EXPENSE"}
_NAMES = {"710": "Computer Equipment", "400": "Office Expenses"}


def _tx(tid, code, amt):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description="x",
        amount=Decimal(str(amt)), vendor_name="Acme", type="ACCPAY",
        contact_id="C1", current_account_code=code,
    )


def test_low_cost_fixed_asset_flagged():
    # Mouse £500 → Computer Equipment (FIXED), < £10k default → flag.
    hits = _find_low_cost_fixed_assets([_tx("1", "710", "500")], _TYPES, _NAMES)
    assert len(hits) == 1
    h = hits[0]
    assert h.issue_type == "low_cost_fixed_asset"
    assert h.current_code == "710"
    assert h.match_reasons["line_amount"] == "500.00"
    assert h.match_reasons["account_name"] == "Computer Equipment"
    # Enrichment behind the "?": no auto-suggestion, but a directional fix hint.
    assert h.suggested_code is None
    assert h.match_reasons["recommended_action"] == "expense"
    assert h.match_reasons["recode_to_account_type"] == "EXPENSE"
    assert h.reasoning and "EXPENSE account" in h.reasoning


def test_expensive_fixed_asset_not_flagged():
    # Laptop £80,000 → FIXED but >= £10k threshold → NOT low-cost.
    assert _find_low_cost_fixed_assets([_tx("1", "710", "80000")], _TYPES, _NAMES) == []


def test_non_fixed_account_not_flagged():
    # £500 to an EXPENSE account → not a fixed asset at all.
    assert _find_low_cost_fixed_assets([_tx("1", "400", "500")], _TYPES, _NAMES) == []


def test_revenue_account_not_flagged():
    assert _find_low_cost_fixed_assets([_tx("1", "200", "500")], _TYPES, _NAMES) == []


def test_threshold_setting_respected():
    s = AuditSettings.from_config({"low_cost_asset_max": "1000"})
    assert len(_find_low_cost_fixed_assets([_tx("1", "710", "500")], _TYPES, _NAMES, s)) == 1
    # 1500 >= 1000 → not low-cost under the tighter threshold
    assert _find_low_cost_fixed_assets([_tx("1", "710", "1500")], _TYPES, _NAMES, s) == []


def test_per_line_item_checked():
    # Document with a cheap fixed-asset line AND an expensive one → only the cheap
    # fixed-asset line is flagged.
    tx = BatchTransaction(
        transaction_id="1", date=date(2026, 1, 1), description="x",
        amount=Decimal("80500"), vendor_name="Acme", type="ACCPAY", contact_id="C1",
        line_items=[
            BatchLineItem(account_code="710", amount=Decimal("500")),    # cheap fixed asset
            BatchLineItem(account_code="710", amount=Decimal("80000")),  # expensive
            BatchLineItem(account_code="400", amount=Decimal("300")),    # expense, ignored
        ],
    )
    hits = _find_low_cost_fixed_assets([tx], _TYPES, _NAMES)
    assert len(hits) == 1
    assert hits[0].match_reasons["line_amount"] == "500.00"
    assert hits[0].match_reasons["line_no"] == 1


def test_no_coa_types_silent():
    # Without account-type info we can't know it's a fixed asset → nothing.
    assert _find_low_cost_fixed_assets([_tx("1", "710", "500")], {}, _NAMES) == []


def test_money_out_bank_item_flagged():
    # The check covers Money In / Money Out too. A SPEND (Money Out) of £300 coded to
    # a FIXED-asset account is just as much a low-cost asset as a bill line.
    spend = BatchTransaction(
        transaction_id="bt1", date=date(2026, 1, 1), description="Cheap printer",
        amount=Decimal("300"), vendor_name="PC World", type="SPEND",
        contact_id="C1", current_account_code="710",
    )
    hits = _find_low_cost_fixed_assets([spend], _TYPES, _NAMES)
    assert len(hits) == 1 and hits[0].current_code == "710"
    assert hits[0].match_reasons["line_amount"] == "300.00"
