"""Canonical registry of every audit check.

The frontend's Audit Configuration screen toggles these on/off per
company. Each rule key matches the ``issue_type`` the engine emits, so
disabling a key simply drops that issue_type from the results (and skips
the relevant LLM pass when possible).

``built = False`` keys are surfaced for completeness (they appear in the
UI) but the engine doesn't emit them yet — toggling them is a no-op.
"""
from __future__ import annotations

from typing import Any

from app.modules.healthcheck.checks.approval import META as _APPROVAL_META
from app.modules.healthcheck.checks.bank import META as _BANK_META
from app.modules.healthcheck.checks.coding import META as _CODING_META
from app.modules.healthcheck.checks.contacts import META as _CONTACTS_META
from app.modules.healthcheck.checks.dates import META as _DATES_META
from app.modules.healthcheck.checks.documents import META as _DOCUMENTS_META
from app.modules.healthcheck.checks.duplicates import META as _DUPLICATES_META
from app.modules.healthcheck.checks.fixed_assets import META as _FIXED_ASSETS_META
from app.modules.healthcheck.checks.prepayments import META as _PREPAYMENTS_META
from app.modules.healthcheck.checks.tax import META as _TAX_META

# group → list of (key, label, built)
_GROUPS: dict[str, list[tuple[str, str, bool]]] = {
    "Bank & Reconciliation": list(_BANK_META),
    "Duplicates": list(_DUPLICATES_META),
    "Tax & VAT": list(_TAX_META),
    "Categorisation & Coding": list(_CODING_META),
    "Date & Ageing": list(_DATES_META) + list(_PREPAYMENTS_META),
    "Approval & Status": list(_APPROVAL_META),
    "Contacts": list(_CONTACTS_META),
    "Document Integrity": list(_DOCUMENTS_META),
    "Fixed Assets": list(_FIXED_ASSETS_META),
    "Currency": [
        ("currency_mismatch", "Currency mismatch", False),
    ],
}

# Flat set of all valid rule keys.
ALL_RULE_KEYS: set[str] = {
    key for rules in _GROUPS.values() for (key, _, _) in rules
}


def rule_catalog(disabled_rules: set[str]) -> list[dict[str, Any]]:
    """Return the full grouped rule list with each rule's enabled state,
    ready for the frontend to render the Audit Configuration screen."""
    out: list[dict[str, Any]] = []
    for group, rules in _GROUPS.items():
        out.append({
            "group": group,
            "rules": [
                {
                    "key": key,
                    "label": label,
                    "built": built,
                    "enabled": key not in disabled_rules,
                }
                for (key, label, built) in rules
            ],
        })
    return out


def total_checks() -> int:
    return len(ALL_RULE_KEYS)
