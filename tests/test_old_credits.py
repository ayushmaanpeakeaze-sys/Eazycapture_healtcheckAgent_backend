"""Old Sales / Purchase Credits: a credit note that still has
unallocated credit (RemainingCredit > 0) and is at least `credit_age_days`
(default 60) old, by credit-note date. Sales (ACCRECCREDIT) and purchase
(ACCPAYCREDIT) are split into separate issue types.

The key correctness point: a Xero credit note carries RemainingCredit (not
AmountDue), so a fully-settled credit (RemainingCredit 0) must NOT be flagged.
"""
from datetime import date, timedelta
from decimal import Decimal

from app.modules.healthcheck.tasks import _reshape_xero_to_batch
from app.schemas.transaction import BatchTransaction
from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.engine.deterministic import _check_old_unsettled_credit

_TODAY = date(2026, 6, 25)


def _credit(tid, days_old, remaining, dtype="ACCRECCREDIT"):
    return BatchTransaction(
        transaction_id=tid, date=_TODAY - timedelta(days=days_old),
        description="x", amount=Decimal("1000"), vendor_name="Acme",
        type=dtype, amount_due=Decimal(str(remaining)),   # amount_due ← RemainingCredit
    )


def test_old_sales_credit_with_remaining_flagged():
    h = _check_old_unsettled_credit(_credit("1", 70, "400"), _TODAY)
    assert h is not None and h.issue_type == "old_unsettled_sales_credit"


def test_old_purchase_credit_flagged():
    h = _check_old_unsettled_credit(_credit("1", 70, "400", dtype="ACCPAYCREDIT"), _TODAY)
    assert h is not None and h.issue_type == "old_unsettled_purchase_credit"


def test_fully_settled_credit_not_flagged():
    # RemainingCredit 0 → fully allocated/refunded → NOT an issue (the bug fix).
    assert _check_old_unsettled_credit(_credit("1", 70, "0"), _TODAY) is None


def test_recent_credit_not_flagged():
    assert _check_old_unsettled_credit(_credit("1", 30, "400"), _TODAY) is None


def test_at_threshold_is_flagged():
    # "at least 60 days old" → exactly 60 flags, 59 does not.
    assert _check_old_unsettled_credit(_credit("1", 60, "400"), _TODAY) is not None
    assert _check_old_unsettled_credit(_credit("1", 59, "400"), _TODAY) is None


def test_non_credit_doc_ignored():
    assert _check_old_unsettled_credit(_credit("1", 70, "400", dtype="ACCREC"), _TODAY) is None


def test_threshold_setting_respected():
    s = AuditSettings.from_config({"credit_age_days": 90})
    assert _check_old_unsettled_credit(_credit("1", 70, "400"), _TODAY, s) is None
    assert _check_old_unsettled_credit(_credit("1", 95, "400"), _TODAY, s) is not None


# --- reshape: a Xero credit note's RemainingCredit must land in amount_due -----

def test_reshape_credit_note_maps_remaining_credit():
    # Fully-settled credit (RemainingCredit 0, AmountDue absent) → amount_due "0.0"
    raw = {
        "CreditNoteID": "cn1", "Type": "ACCRECCREDIT", "Total": 541.25,
        "RemainingCredit": 0.0, "Date": "/Date(1778371200000+0000)/",
        "Contact": {"Name": "Acme", "ContactID": "C1"},
    }
    out = _reshape_xero_to_batch(raw)
    assert out["amount_due"] == "0.0"        # RemainingCredit, NOT None/Total

    raw2 = {**raw, "CreditNoteID": "cn2", "RemainingCredit": 400.0}
    assert _reshape_xero_to_batch(raw2)["amount_due"] == "400.0"
