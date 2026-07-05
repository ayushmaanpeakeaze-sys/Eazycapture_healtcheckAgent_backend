"""Undocumented Bills: a supplier bill (or, with the toggle, a Money Out)
with NO attachment in Xero. Filters: minimum amount, tax-only, ignored contacts.
Flag only when HasAttachments is explicitly False — never on missing data.
"""
from datetime import date
from decimal import Decimal

from app.schemas.transaction import BatchTransaction
from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.checks.documents import _find_undocumented_bills


def _bill(tid, has_attach=False, amount="100", dtype="ACCPAY",
          tax_total=None, cid="C1", vendor="Acme"):
    return BatchTransaction(
        transaction_id=tid, date=date(2026, 1, 1), description="x",
        amount=Decimal(amount), vendor_name=vendor, type=dtype, contact_id=cid,
        has_attachments=has_attach,
        tax_total=Decimal(tax_total) if tax_total is not None else None,
    )


def test_bill_no_attachment_flagged():
    hits = _find_undocumented_bills([_bill("1", has_attach=False)])
    assert [h.issue_type for h in hits] == ["undocumented_bill"]
    assert hits[0].match_reasons["net_amount"] == "100.00"


def test_bill_with_attachment_not_flagged():
    assert _find_undocumented_bills([_bill("1", has_attach=True)]) == []


def test_unknown_attachment_not_flagged():
    # HasAttachments not fetched (None) → never flag on missing data.
    assert _find_undocumented_bills([_bill("1", has_attach=None)]) == []


def test_below_min_amount_not_flagged():
    s = AuditSettings.from_config({"undocumented_min_amount": "50"})
    assert _find_undocumented_bills([_bill("1", amount="30")], s) == []
    assert _find_undocumented_bills([_bill("1", amount="500")], s) != []


def test_ignored_contact_not_flagged():
    s = AuditSettings.from_config({"undocumented_ignore_contacts": ["C1"]})
    assert _find_undocumented_bills([_bill("1", cid="C1")], s) == []


def test_tax_only_setting():
    s = AuditSettings.from_config({"undocumented_tax_only": True})
    assert _find_undocumented_bills([_bill("1", tax_total="0")], s) == []      # zero tax → skip
    assert _find_undocumented_bills([_bill("1", tax_total="20")], s) != []     # has tax → flag


def test_money_out_flagged():
    hits = _find_undocumented_bills([_bill("1", dtype="SPEND")])
    assert len(hits) == 1


def test_customer_invoice_not_a_bill():
    assert _find_undocumented_bills([_bill("1", dtype="ACCREC")]) == []
