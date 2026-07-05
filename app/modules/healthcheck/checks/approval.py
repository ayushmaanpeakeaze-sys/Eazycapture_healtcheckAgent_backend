"""Approval & Status check group.

Unapproved (DRAFT/SUBMITTED) invoices + bills. Detection runs inside
``deterministic._inspect_transaction`` / ``_check_unapproved``; settings +
registry entries live here.
"""
from __future__ import annotations

from app.modules.healthcheck.checks.base import SettingField

SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("unapproved_grace_days", "Approval & Status", "unapproved_invoice",
                 "Invoice is at least … days old", "int",
                 "Only show an unapproved (DRAFT or SUBMITTED) invoice/bill once "
                 "it is at least this many days old, measured from the invoice "
                 "date. Default 0 = surface every unapproved document "
                 "immediately. Applies to both unapproved invoices and bills.",
                 unit="days", min=0, max=365, step=1),
)

META: tuple[tuple[str, str, bool], ...] = (
    ("unapproved_invoice", "Unapproved invoices", True),
    ("unapproved_bill", "Unapproved bills", True),
)
