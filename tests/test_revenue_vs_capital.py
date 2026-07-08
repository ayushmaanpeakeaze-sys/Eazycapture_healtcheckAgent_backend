"""Revenue vs Capital review (content-based, pure deterministic) — the SOP engine.

A P&L EXPENSE line above the threshold whose description / reference / supplier
reads like a capital purchase (a capital keyword or a known capital supplier) is
flagged for capitalisation review, unless the text is obvious revenue spend
(repairs / servicing / fuel / insurance / consumables). Independent of the
account it sits on — that is the sibling capital_item_review check's job.
"""
from datetime import date
from decimal import Decimal

from app.shared.transaction import BatchTransaction
from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.checks.revenue_vs_capital import _find_revenue_vs_capital

# 400 = plain "Office Expenses" (EXPENSE but NOT monitored / capital-suspicious by
# name), so ONLY the description or supplier signal can flag these — isolating the
# content engine from the account-based check.
_TYPES = {"400": "EXPENSE", "710": "FIXED", "200": "REVENUE"}
_NAMES = {"400": "Office Expenses", "710": "Computer Equipment", "200": "Sales"}


def _tx(tid, amt, desc, *, code="400", vendor="Acme"):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description=desc,
        amount=Decimal(str(amt)), vendor_name=vendor, type="ACCPAY",
        contact_id="C1", current_account_code=code,
    )


def test_laptop_flagged():
    hits = _find_revenue_vs_capital([_tx("1", 1200, "Dell laptop purchase", vendor="Dell")], _NAMES, _TYPES)
    assert len(hits) == 1
    h = hits[0]
    assert h.issue_type == "revenue_vs_capital"
    assert h.match_reasons["matched_keyword"] == "laptop"
    assert h.match_reasons["recommended_action"] == "capitalise"
    assert h.match_reasons["recode_to_account_type"] == "FIXED"


def test_vehicle_servicing_excluded():
    # "vehicle" is capital, but "servicing" / "repair" is a revenue exclusion.
    assert _find_revenue_vs_capital([_tx("1", 900, "Vehicle servicing and repair")], _NAMES, _TYPES) == []


def test_office_furniture_flagged():
    assert len(_find_revenue_vs_capital([_tx("1", 750, "Office furniture - chairs", vendor="IKEA")], _NAMES, _TYPES)) == 1


def test_below_threshold_ignored():
    assert _find_revenue_vs_capital([_tx("1", 300, "Dell laptop", vendor="Dell")], _NAMES, _TYPES) == []


def test_no_capital_signal_ignored():
    assert _find_revenue_vs_capital([_tx("1", 600, "Monthly subscription fee", vendor="Netflix")], _NAMES, _TYPES) == []


def test_exclusion_beats_keyword():
    assert _find_revenue_vs_capital([_tx("1", 800, "Machinery repair")], _NAMES, _TYPES) == []


def test_supplier_signal_flags():
    # No obvious keyword, but a known capital supplier + above threshold.
    hits = _find_revenue_vs_capital([_tx("1", 1500, "Order 4471", vendor="Currys")], _NAMES, _TYPES)
    assert len(hits) == 1
    assert hits[0].match_reasons["matched_supplier"] == "currys"


def test_flags_regardless_of_account():
    # Content-driven: a laptop on a FIXED account still isn't its job (only P&L
    # expense accounts), but on any EXPENSE account the content signal flags it —
    # here 400 is a plain non-monitored expense account, proving it is account-agnostic.
    hits = _find_revenue_vs_capital([_tx("1", 1200, "New server hardware", vendor="Dell")], _NAMES, _TYPES)
    assert len(hits) == 1
    assert hits[0].current_code == "400"


def test_only_expense_accounts():
    # A capital-looking line already on a FIXED account is correctly coded → ignored.
    assert _find_revenue_vs_capital([_tx("1", 1200, "Dell laptop", code="710", vendor="Dell")], _NAMES, _TYPES) == []


def test_threshold_setting_respected():
    s = AuditSettings.from_config({"revenue_vs_capital_threshold": "1000"})
    assert len(_find_revenue_vs_capital([_tx("1", 1500, "Dell laptop", vendor="Dell")], _NAMES, _TYPES, s)) == 1
    assert _find_revenue_vs_capital([_tx("1", 800, "Dell laptop", vendor="Dell")], _NAMES, _TYPES, s) == []
