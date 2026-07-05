"""Unapproved Invoices / Bills: a customer invoice or supplier
bill left in DRAFT or SUBMITTED status — it is not reflected in the accounts.

Spec: Status ∈ {DRAFT, SUBMITTED}. Setting "Date of invoice is at least x days
old" (by invoice date), default 0 → flag every unapproved document.
ACCREC → unapproved_invoice, ACCPAY → unapproved_bill.
"""
from datetime import date, timedelta
from decimal import Decimal

from app.schemas.transaction import BatchTransaction
from app.modules.healthcheck.engine.audit_settings import AuditSettings, DEFAULT_SETTINGS
from app.modules.healthcheck.engine.deterministic import _check_unapproved

_TODAY = date(2026, 6, 24)


def _tx(status, doc_type="ACCREC", age_days=10):
    return BatchTransaction(
        transaction_id="1", date=_TODAY - timedelta(days=age_days),
        description="x", amount=Decimal("1000"), vendor_name="Acme",
        type=doc_type, contact_id="C1", status=status,
    )


def test_draft_invoice_flagged():
    h = _check_unapproved(_tx("DRAFT"), _TODAY)
    assert h is not None
    assert h.issue_type == "unapproved_invoice"
    assert h.current_code == "DRAFT"


def test_submitted_invoice_flagged():
    h = _check_unapproved(_tx("SUBMITTED"), _TODAY)
    assert h is not None and h.issue_type == "unapproved_invoice"


def test_submitted_bill_flagged_as_bill():
    h = _check_unapproved(_tx("DRAFT", doc_type="ACCPAY"), _TODAY)
    assert h is not None and h.issue_type == "unapproved_bill"


def test_authorised_not_unapproved():
    # AUTHORISED = approved/Awaiting Payment → NOT this check (old-unpaid's job).
    assert _check_unapproved(_tx("AUTHORISED"), _TODAY) is None


def test_paid_not_unapproved():
    assert _check_unapproved(_tx("PAID"), _TODAY) is None


def test_default_zero_flags_same_day_draft():
    # Default 0 → even a DRAFT raised today shows up immediately.
    assert _check_unapproved(_tx("DRAFT", age_days=0), _TODAY, DEFAULT_SETTINGS) is not None


def test_minimum_age_setting_hides_recent():
    # "At least 5 days old": a 3-day-old DRAFT is hidden, a 5-day-old one shows.
    s = AuditSettings.from_config({"unapproved_grace_days": "5"})
    assert _check_unapproved(_tx("DRAFT", age_days=3), _TODAY, s) is None
    assert _check_unapproved(_tx("DRAFT", age_days=5), _TODAY, s) is not None
