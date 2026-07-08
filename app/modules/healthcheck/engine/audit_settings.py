"""Per-client audit settings — the tunable thresholds for every check.

Each check reads its threshold from an ``AuditSettings`` instead of a hardcoded
constant. Defaults match the historical constants, so behaviour is **unchanged**
unless a client overrides a value. Built from the company's
``audit_config['settings']`` dict (any unknown keys are ignored; missing keys
keep the default).

This is the configurable-settings foundation — one object, threaded through the
orchestrator into the checks.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

# Fields that must be Decimal (used in Decimal arithmetic inside the checks).
_DECIMAL_FIELDS = {"outlier_multiple", "outlier_min_amount",
                   "low_cost_asset_max", "capital_pre_filter_min",
                   "capital_item_threshold",
                   "misallocated_materiality", "bank_balance_tolerance",
                   "undocumented_min_amount", "opening_balance_min_difference"}
# Fields coerced to int (everything else numeric falls through to float).
_INT_FIELDS = {"duplicate_days_window", "overdue_days", "credit_age_days",
               "old_unpaid_invoice_days", "old_unpaid_bill_days",
               "unapproved_grace_days", "inactive_days", "bill_direct_window_days",
               "invoice_direct_window_days",
               "supplier_min_txns", "outlier_min_txns"}
# Fields that are tuples of upper-cased string tokens (account codes / contact
# ids / names). Accept a list/tuple or a comma-separated string.
_TUPLE_FIELDS = {"misallocated_vague_codes", "tax_missing_ignore_accounts",
                 "tax_missing_ignore_contacts", "multi_account_whitelist_contacts",
                 "bank_exclude_accounts", "capital_monitored_accounts",
                 "undocumented_ignore_contacts"}
# Boolean fields.
_BOOL_FIELDS = {"ignore_generic_contact", "duplicate_require_same_amount",
                "duplicate_require_exact_reference", "duplicate_also_check_paid",
                "undocumented_tax_only"}
# Enum/string fields: value must be one of the allowed tokens (lower-cased);
# anything else is dropped so a bad value can never poison an audit.
_ENUM_FIELDS = {"old_unpaid_age_basis": {"invoice_date", "due_date"}}


def _coerce_field(key: str, value: Any) -> tuple[bool, Any]:
    """Coerce one ``audit_config['settings']`` value to its field type.

    Returns ``(True, coerced)`` on success or ``(False, None)`` when the value
    is unusable (so the caller drops it and keeps the default). Shared by
    ``from_config`` (build the settings object) and ``clean_overrides`` (persist
    only the valid overrides) so coercion lives in exactly one place.
    """
    try:
        if key in _DECIMAL_FIELDS:
            return True, Decimal(str(value))
        if key in _BOOL_FIELDS:
            return True, bool(value)
        if key in _ENUM_FIELDS:
            v = str(value).strip().lower()
            return (True, v) if v in _ENUM_FIELDS[key] else (False, None)
        if key in _TUPLE_FIELDS:
            if isinstance(value, str):
                items: Any = value.split(",")
            elif isinstance(value, (list, tuple)):
                items = value
            else:
                return False, None
            return True, tuple(
                str(v).strip().upper() for v in items if str(v).strip()
            )
        if key in _INT_FIELDS:
            return True, int(value)
        return True, float(value)
    except (TypeError, ValueError, InvalidOperation):
        return False, None


@dataclass(frozen=True)
class AuditSettings:
    # --- duplicates ---
    # 0 (default) = the two documents must share the same issue date.
    # Bump to N to also pair documents that many days apart.
    duplicate_days_window: int = 0
    # Duplicate-invoice toggles; defaults are stricter, flip to widen matching.
    duplicate_require_same_amount: bool = True       # off → values may differ
    # ON (default): drop pairs whose references conflict (both present, differ);
    # exact-ref and no-ref pairs still flag. OFF → different-ref also surfaces.
    duplicate_require_exact_reference: bool = True
    duplicate_also_check_paid: bool = False          # default: ≥1 invoice must be unpaid
    duplicate_min_confidence: float = 0.90
    # --- aging ---
    # Legacy shared threshold; seeds the split per-check values below when those
    # aren't explicitly set, so old configs keep working.
    overdue_days: int = 60
    # Old-unpaid checks: grace days past the due date before flagging (1 = flag
    # once a day overdue). Separate values for customer invoices vs supplier bills.
    old_unpaid_invoice_days: int = 1     # ACCREC — customer invoices
    old_unpaid_bill_days: int = 1        # ACCPAY — supplier bills
    # How an invoice/bill's age is measured for the old-unpaid checks:
    #   "due_date" (default) — days past the due date (true overdue).
    #   "invoice_date"       — days since it was raised.
    old_unpaid_age_basis: str = "due_date"
    credit_age_days: int = 60
    # Unapproved invoices/bills (DRAFT or SUBMITTED): minimum age in days (by
    # invoice date) to surface. Default 0 flags every unapproved document.
    unapproved_grace_days: int = 0
    inactive_days: int = 180
    # Bill-or-Direct-Payment: an unpaid bill is matched with a SPEND bank payment
    # to the same supplier dated within this many days of the bill.
    bill_direct_window_days: int = 30
    # Invoice-or-Direct-Deposit: an unpaid invoice matched with a RECEIVE deposit
    # to the same customer dated within this many days of the invoice.
    invoice_direct_window_days: int = 30
    # --- supplier patterns (history) ---
    supplier_min_txns: int = 3
    supplier_dominance: float = 0.70
    # --- amount outlier ---
    outlier_min_txns: int = 4
    outlier_multiple: Decimal = Decimal("4.0")
    outlier_min_amount: Decimal = Decimal("100")
    # --- duplicate contacts ---
    # Two contact names must be at least this similar (0..1) to flag a possible
    # duplicate. VAT/email/phone are enrichment, not part of the match.
    dup_contact_name_sim: float = 0.70   # 0..1 (default 70%)
    ignore_generic_contact: bool = True  # kept for config back-compat
    # --- capital / asset ---
    # Low-Cost Fixed Asset: a FIXED-asset line BELOW this is too cheap to capitalise.
    low_cost_asset_max: Decimal = Decimal("10000")
    capital_pre_filter_min: Decimal = Decimal("300")
    # Capital Item Review (mirror): a monitored EXPENSE line ABOVE this may really
    # be a capital item (fixed asset) mis-coded to an expense account.
    capital_item_threshold: Decimal = Decimal("500")
    # Account CODES to watch for capital items (e.g. Repairs, Printing). Empty →
    # fall back to a name-keyword match on expense accounts (repairs / maintenance
    # / printing / stationery).
    capital_monitored_accounts: tuple[str, ...] = ()
    # --- misallocated items (vague account + material amount) ---
    # --- undocumented bills (no attachment in Xero) -----------------------
    undocumented_min_amount: Decimal = Decimal("0")   # ignore bills under this
    undocumented_tax_only: bool = False               # only bills with tax > 0
    undocumented_ignore_contacts: tuple[str, ...] = ()
    # --- opening balance differences (filed accounts vs Xero) -------------
    # Minimum |Net Assets filed - Net Assets in Xero| that flags an issue.
    # The default is £1 (ignores negligible rounding).
    opening_balance_min_difference: Decimal = Decimal("1")
    # --- misallocated items (vague account + material amount) -------------
    misallocated_materiality: Decimal = Decimal("100")
    # Extra per-client account CODES to treat as "vague" (on top of the default
    # name-keyword match: uncategorised / unapplied / general expenses / …).
    misallocated_vague_codes: tuple[str, ...] = ()
    # --- tax-missing ignore-lists (suppress known-legit zero-rated cases) ---
    # Account codes whose postings never need VAT (e.g. wages, bank interest).
    tax_missing_ignore_accounts: tuple[str, ...] = ()
    # Contact ids OR names (upper-cased) that are legitimately zero-rated/exempt.
    tax_missing_ignore_contacts: tuple[str, ...] = ()
    # --- multi-account supplier whitelist ---
    # Contact ids OR names that legitimately post to many accounts (e.g. Amazon)
    # — suppresses the multi_account_supplier flag for them.
    multi_account_whitelist_contacts: tuple[str, ...] = ()
    # --- bank balance check (statement vs GL balance per account) ---
    bank_balance_tolerance: Decimal = Decimal("0.01")
    bank_exclude_accounts: tuple[str, ...] = ()   # personal/credit-card accounts
    # --- llm ---
    llm_min_confidence: float = 0.80

    def as_json_dict(self) -> dict[str, Any]:
        """All fields in JSON-storable form (Decimal → str, tuple → list).
        Used to expose the defaults to the Audit Configuration screen so it can
        render each threshold's placeholder/current value."""
        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            val = getattr(self, f.name)
            if isinstance(val, Decimal):
                out[f.name] = str(val)
            elif isinstance(val, tuple):
                out[f.name] = list(val)
            else:
                out[f.name] = val
        return out

    @classmethod
    def from_config(cls, cfg: Optional[dict[str, Any]]) -> "AuditSettings":
        """Build from ``audit_config['settings']`` — unknown keys ignored,
        missing keys keep defaults, numeric strings coerced safely."""
        if not isinstance(cfg, dict):
            return cls()
        valid = {f.name for f in dataclasses.fields(cls)}
        overrides: dict[str, Any] = {}
        for key, value in cfg.items():
            if key not in valid or value is None:
                continue
            ok, coerced = _coerce_field(key, value)
            if ok:
                overrides[key] = coerced
        # Back-compat: the legacy shared ``overdue_days`` seeds the per-check
        # thresholds when those split keys weren't explicitly provided, so old
        # stored configs (and callers) keep behaving the same.
        if "overdue_days" in overrides:
            overrides.setdefault("old_unpaid_invoice_days", overrides["overdue_days"])
            overrides.setdefault("old_unpaid_bill_days", overrides["overdue_days"])
        return dataclasses.replace(cls(), **overrides)

    @classmethod
    def clean_overrides(cls, cfg: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Return only the valid, client-set keys from ``cfg`` in a
        JSON-storable form (Decimal → str, tuple → list), for persisting to the
        ``audit_config['settings']`` JSONB blob.

        Keeps just the overrides (so a stored config stays minimal and future
        default changes still apply to keys the client never touched). Unknown
        keys and bad values are dropped — a bad value is NOT persisted as the
        default; it is simply omitted.
        """
        if not isinstance(cfg, dict):
            return {}
        valid = {f.name for f in dataclasses.fields(cls)}
        out: dict[str, Any] = {}
        for key, value in cfg.items():
            if key not in valid or value is None:
                continue
            ok, coerced = _coerce_field(key, value)
            if not ok:
                continue
            if isinstance(coerced, Decimal):
                out[key] = str(coerced)
            elif isinstance(coerced, tuple):
                out[key] = list(coerced)
            else:
                out[key] = coerced
        return out


# Module-level default so check functions can default their `settings` param
# (keeps existing call sites + tests working without passing settings).
DEFAULT_SETTINGS = AuditSettings()


# ---------------------------------------------------------------------------
# Per-check field metadata for the Audit Configuration screen
# ---------------------------------------------------------------------------
# Maps every tunable field to its check (``check`` keys match ``rules_registry``)
# and how to render it, so the settings UI is fully backend-driven.
#
# ``type`` tells the frontend which control to draw:
#   bool      → toggle
#   int       → integer input (see ``unit``)
#   amount    → money input (stored as a Decimal string)
#   multiple  → "Nx" numeric input
#   percent   → 0..1 value rendered as a percentage / slider
#   list      → comma-separated / tag list of codes, ids or names
#   select    → one of ``options`` (a dropdown)
# SettingField lives in app/checks/base.py (neutral module) to avoid a circular
# import; re-exported here for existing ``from audit_settings import SettingField``.
from app.modules.healthcheck.checks.base import (  # noqa: E402
    SettingField,
    collect_category_setting_fields,
)


# Field metadata per check. Add more checks' fields here (mapped to their rule
# key) to grow the config screen one section at a time; ``settings_schema()``
# and the /audit-config/ responses pick them up automatically.
_SETTINGS_META: tuple[SettingField, ...] = (
    # --- Duplicate invoices -----------------------------------------------
    # --- Duplicate contacts -----------------------------------------------
    # --- Old unpaid invoices (customer / ACCREC) --------------------------
    # --- Old unpaid bills (supplier / ACCPAY) -----------------------------
    # --- Old sales / purchase credit notes (unallocated) ------------------
    # --- Bill or Direct Payment -------------------------------------------
    # --- Invoice or Direct Deposit ----------------------------------------
    # --- Opening Balance Differences --------------------------------------
    # --- Bank Balance Check -----------------------------------------------
    # --- Inactive contacts ------------------------------------------------
    # --- Unapproved invoices / bills (DRAFT or SUBMITTED) ------------------
    # Fixed-asset checks' settings moved to app/checks/fixed_assets.py
    # (aggregated via collect_category_setting_fields()).
    # --- Misallocated items (vague account + material amount) -------------
    # Misallocated-items settings moved to app/checks/coding.py.
    # Undocumented-bill settings moved to app/checks/documents.py.
    # (capital_item_review settings also live in app/checks/fixed_assets.py)
)


def settings_schema() -> list[dict[str, Any]]:
    """Per-check field metadata for the Audit Configuration screen.

    Groups every tunable :class:`AuditSettings` field under the check it
    belongs to, so the settings UI renders entirely from the backend — one
    section per check, each field carrying its label / type / help / default /
    bounds. Each entry's ``check`` matches a ``rules_registry`` rule key so the
    frontend can pair the field group with that check's on/off toggle.

    Group order follows first appearance in ``_SETTINGS_META``.
    """
    defaults = DEFAULT_SETTINGS.as_json_dict()
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    # Central fields + each per-category module's SETTING_FIELDS (e.g. Fixed Assets).
    for f in _SETTINGS_META + collect_category_setting_fields():
        bucket = grouped.setdefault(
            (f.group, f.check),
            {"group": f.group, "check": f.check, "fields": []},
        )
        bucket["fields"].append({
            "key": f.key,
            "label": f.label,
            "type": f.type,
            "help": f.help,
            "unit": f.unit,
            "min": f.min,
            "max": f.max,
            "step": f.step,
            "options": list(f.options) if f.options else None,
            "default": defaults.get(f.key),
        })
    return list(grouped.values())
