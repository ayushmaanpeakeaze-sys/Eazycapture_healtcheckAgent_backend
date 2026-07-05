"""Document Integrity check group.

  * ``undocumented_bill`` — a supplier BILL (or Money Out) with NO attachment in
    Xero (``HasAttachments`` False). Detection lives here.
  * ``missing_invoice_number`` — emitted inside ``deterministic._inspect_transaction``
    (a per-document inspection), so only its registry entry is listed here.

Detection logic + tunable settings + registry metadata for this group.
"""
from __future__ import annotations

from app.modules.healthcheck.checks.base import SettingField
from app.shared.transaction import BatchTransaction, FlaggedIssue
from app.modules.healthcheck.engine.shared import _MONEY_OUT_TYPES


def _settings(settings):
    if settings is None:
        from app.modules.healthcheck.engine.audit_settings import DEFAULT_SETTINGS
        return DEFAULT_SETTINGS
    return settings


def _find_undocumented_bills(
    transactions: list[BatchTransaction],
    settings=None,
) -> list[FlaggedIssue]:
    """Undocumented Bills: a supplier BILL (or, via the 'Show direct
    payments' toggle, a Money Out) with NO attachment in Xero (HasAttachments
    False). Filters: minimum amount, tax-only, and ignored contacts. Money Out
    is always flagged here; the frontend hides it by default (exclude_bank_items)."""
    settings = _settings(settings)
    ignore_contacts = frozenset(
        c.strip().upper() for c in (settings.undocumented_ignore_contacts or ()) if c
    )
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        doc_type = (tx.type or "").strip().upper()
        is_bill = doc_type == "ACCPAY"
        if not (is_bill or doc_type in _MONEY_OUT_TYPES):
            continue
        # Only flag when we KNOW there is no attachment. None = not fetched → skip
        # (never flag on missing data).
        if tx.has_attachments is not False:
            continue
        contact = (tx.contact_id or "").strip().upper()
        name = (tx.vendor_name or "").strip().upper()
        if (contact and contact in ignore_contacts) or (name and name in ignore_contacts):
            continue
        if abs(tx.amount) < settings.undocumented_min_amount:
            continue
        if settings.undocumented_tax_only and not (tx.tax_total and abs(tx.tax_total) > 0):
            continue
        reasons: dict = {
            "net_amount": f"{abs(tx.amount):.2f}",
            "currency": (tx.currency_code or "GBP").strip().upper(),
        }
        if tx.tax_total is not None:
            reasons["tax_amount"] = f"{abs(tx.tax_total):.2f}"
        flagged.append(FlaggedIssue(
            transaction_id=tx.transaction_id,
            issue_type="undocumented_bill",
            severity="medium",
            message=(f"{tx.vendor_name}: {'bill' if is_bill else 'payment'} "
                     f"£{abs(tx.amount):.2f} has no attachment in Xero.")[:140],
            match_reasons=reasons,
        ))
    return flagged


# --- settings (gear) ---------------------------------------------------------
SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("undocumented_min_amount", "Document Integrity", "undocumented_bill",
                 "Ignore bills under …", "amount",
                 "Don't flag an unattached bill below this value. Default 0 = flag "
                 "any amount.",
                 unit="currency", min=0, step=10),
    SettingField("undocumented_tax_only", "Document Integrity", "undocumented_bill",
                 "Only bills that include tax", "bool",
                 "When on, only flag unattached bills that have a VAT/tax amount > 0 "
                 "(skip zero-tax bills like wages/bank charges)."),
    SettingField("undocumented_ignore_contacts", "Document Integrity", "undocumented_bill",
                 "Ignore these contacts", "list",
                 "Contact ids OR names whose bills never need an attachment (e.g. a "
                 "director). 'Ignore this contact' appends here."),
)

# --- registry metadata (key, label, built) -----------------------------------
META: tuple[tuple[str, str, bool], ...] = (
    ("missing_invoice_number", "Missing invoice number", True),
    ("undocumented_bill", "Undocumented bills (no attachment)", True),
)
