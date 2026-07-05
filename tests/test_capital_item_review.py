"""Capital Item Review (pure deterministic) — mirror image of low_cost_fixed_asset.

A line posted to a MONITORED EXPENSE account for an amount ABOVE the threshold
(capital_item_threshold, default £5k) → may really be a capital item (fixed
asset) mis-coded to an expense. Account + AMOUNT only — no contact, no LLM.
Monitored = explicit codes when configured, else expense accounts whose NAME
looks capital-suspicious (repairs / maintenance / printing / stationery).
"""
from datetime import date
from decimal import Decimal

from app.shared.transaction import BatchLineItem, BatchTransaction
from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.checks.fixed_assets import _find_capital_items

# 473 Repairs & 461 Printing are EXPENSE; 710 is FIXED; 200 is REVENUE.
_TYPES = {"473": "EXPENSE", "461": "OVERHEADS", "710": "FIXED", "200": "REVENUE",
          "400": "EXPENSE"}
_NAMES = {"473": "Repairs & Maintenance", "461": "Printing & Stationery",
          "710": "Computer Equipment", "200": "Sales", "400": "Office Expenses"}


def _tx(tid, code, amt):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description="x",
        amount=Decimal(str(amt)), vendor_name="Acme", type="ACCPAY",
        contact_id="C1", current_account_code=code,
    )


def test_big_expense_on_repairs_flagged():
    # £90k to Repairs & Maintenance (expense) → way over £5k → maybe a fixed asset.
    hits = _find_capital_items([_tx("1", "473", "90000")], _NAMES, _TYPES)
    assert len(hits) == 1
    h = hits[0]
    assert h.issue_type == "capital_item_review"
    assert h.current_code == "473"
    assert h.match_reasons["line_amount"] == "90000.00"
    assert h.match_reasons["account_name"] == "Repairs & Maintenance"
    # Enrichment behind the "?": directional fix is the mirror of low-cost.
    assert h.suggested_code is None
    assert h.match_reasons["recommended_action"] == "capitalise"
    assert h.match_reasons["recode_to_account_type"] == "FIXED"
    assert h.reasoning and "FIXED asset" in h.reasoning


def test_small_expense_not_flagged():
    # £400 to Repairs → under £5k → a normal expense.
    assert _find_capital_items([_tx("1", "473", "400")], _NAMES, _TYPES) == []


def test_at_threshold_not_flagged():
    # Exactly £5k is NOT "above" the threshold (strict >).
    assert _find_capital_items([_tx("1", "473", "5000")], _NAMES, _TYPES) == []


def test_big_expense_on_non_suspicious_account_not_flagged():
    # £90k to a plain "Office Expenses" account (no capital keyword) → ignored in
    # name-keyword mode.
    assert _find_capital_items([_tx("1", "400", "90000")], _NAMES, _TYPES) == []


def test_fixed_asset_account_not_flagged():
    # A big amount on a FIXED-asset account is correctly coded already → ignored.
    assert _find_capital_items([_tx("1", "710", "90000")], _NAMES, _TYPES) == []


def test_revenue_account_not_flagged():
    assert _find_capital_items([_tx("1", "200", "90000")], _NAMES, _TYPES) == []


def test_explicit_monitored_accounts_override_keywords():
    # With explicit monitored codes, ONLY those codes are watched — regardless of
    # name. 400 (Office Expenses) is now monitored; 473 (Repairs) is not.
    s = AuditSettings.from_config({"capital_monitored_accounts": ["400"]})
    assert len(_find_capital_items([_tx("1", "400", "90000")], _NAMES, _TYPES, s)) == 1
    assert _find_capital_items([_tx("1", "473", "90000")], _NAMES, _TYPES, s) == []


def test_threshold_setting_respected():
    s = AuditSettings.from_config({"capital_item_threshold": "1000"})
    assert len(_find_capital_items([_tx("1", "473", "1500")], _NAMES, _TYPES, s)) == 1
    assert _find_capital_items([_tx("1", "473", "800")], _NAMES, _TYPES, s) == []


def test_per_line_item_checked():
    # One document, two lines: a big repairs line (flag) + a small one (ignore).
    tx = BatchTransaction(
        transaction_id="1", date=date(2026, 1, 1), description="x",
        amount=Decimal("90400"), vendor_name="Acme", type="ACCPAY", contact_id="C1",
        line_items=[
            BatchLineItem(account_code="473", amount=Decimal("90000")),  # capital-suspect
            BatchLineItem(account_code="473", amount=Decimal("400")),    # normal repair
        ],
    )
    hits = _find_capital_items([tx], _NAMES, _TYPES)
    assert len(hits) == 1
    assert hits[0].match_reasons["line_amount"] == "90000.00"
    assert hits[0].match_reasons["line_no"] == 1


def test_no_coa_types_silent_in_keyword_mode():
    # Without account-type info we can't confirm it's an expense → keyword mode
    # flags nothing (we never want to flag a non-expense line).
    assert _find_capital_items([_tx("1", "473", "90000")], _NAMES, {}) == []
