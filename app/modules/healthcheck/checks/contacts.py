"""Contacts check group.

Missing vendor, missing contact defaults, inactive contacts. Detection lives in
``app/services/healthcheck/contact_checks.py``; settings + registry entries here.
"""
from __future__ import annotations

from app.modules.healthcheck.checks.base import SettingField

SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("inactive_days", "Contacts", "inactive_contact",
                 "Inactive if no transaction for … ", "int",
                 "Flag a contact whose most recent transaction is at least this "
                 "many days old (or that has never transacted). Default 180.",
                 unit="days", min=1, max=1095, step=1),
)

META: tuple[tuple[str, str, bool], ...] = (
    ("missing_vendor", "Missing vendor", True),
    ("contact_defaults", "Contact defaults missing", True),
    ("inactive_contact", "Inactive contacts", True),
)
