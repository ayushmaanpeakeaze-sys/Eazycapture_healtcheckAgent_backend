"""Date & Ageing check group.

Overdue invoices/bills + old unsettled credit notes + future-dated documents.
Detection runs inside ``deterministic._inspect_transaction`` / ``_check_old_unpaid``
/ ``_check_old_unsettled_credit`` (a shared per-document loop), so only the
settings + registry entries live here.
"""
from __future__ import annotations

from app.modules.healthcheck.checks.base import SettingField

SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("old_unpaid_invoice_days", "Date & Ageing", "old_unpaid_invoice",
                 "Flag once … days overdue", "int",
                 "With the default 'due date' basis: how many days PAST the due "
                 "date before a customer invoice is flagged. 1 = flag as soon as "
                 "it is a day overdue. (If basis = invoice date, this is days "
                 "since the invoice was raised instead.)",
                 unit="days", min=1, max=365, step=1),
    SettingField("old_unpaid_age_basis", "Date & Ageing", "old_unpaid_invoice",
                 "Age measured from", "select",
                 "Measure overdue from the DUE date (default — the due date "
                 "already includes the 20/30-day terms, so even 1 day past it is "
                 "overdue) or from the invoice date (ageing — days "
                 "since it was raised). Applies to both old-unpaid checks.",
                 options=("due_date", "invoice_date")),
    SettingField("old_unpaid_bill_days", "Date & Ageing", "old_unpaid_bill",
                 "Flag once … days overdue", "int",
                 "With the default 'due date' basis: how many days PAST the due "
                 "date before a supplier bill is flagged. 1 = flag as soon as it "
                 "is a day overdue.",
                 unit="days", min=1, max=365, step=1),
    SettingField("credit_age_days", "Date & Ageing", "old_unsettled_sales_credit",
                 "Credit note is at least … days old", "int",
                 "Flag a sales or purchase credit note that still has unallocated "
                 "credit (RemainingCredit > 0) and is at least this many days old "
                 "(by credit-note date). Default 60. Applies to both old "
                 "sales and old purchase credit checks.",
                 unit="days", min=1, max=365, step=1),
)

META: tuple[tuple[str, str, bool], ...] = (
    ("future_dated", "Future-dated documents", True),
    ("old_unpaid_bill", "Overdue bills (we owe)", True),
    ("old_unpaid_invoice", "Overdue invoices (we're owed)", True),
    ("old_unsettled_sales_credit", "Old sales credit notes", True),
    ("old_unsettled_purchase_credit", "Old purchase credit notes", True),
)
