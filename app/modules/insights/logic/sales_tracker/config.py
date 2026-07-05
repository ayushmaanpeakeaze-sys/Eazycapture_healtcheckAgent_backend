"""Sales-target configuration — how a client's monthly target is derived.

One of eight strategies (the Settings page picks it), optionally nudged by a
+/- percentage. Parsed from a plain dict so it can live in JSON config (stored
on ``company.audit_config['sales_target']``) without a schema dependency here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# The eight target strategies — match the Settings page options.
BASIS_NONE = "none"                       # No Target Set
BASIS_PREVIOUS_MONTH = "previous_month"   # last month's actual
BASIS_AVERAGE_3 = "average_3"             # avg of previous 3 months
BASIS_AVERAGE_6 = "average_6"             # avg of previous 6 months
BASIS_AVERAGE_12 = "average_12"           # avg of previous 12 months
BASIS_SAME_MONTH_LAST_YEAR = "same_month_last_year"  # same month, prior year
BASIS_XERO_BUDGET = "xero_budget"         # pulled from Xero budgets
BASIS_MANUAL = "manual"                   # one fixed value every month

_VALID_BASES = {
    BASIS_NONE, BASIS_PREVIOUS_MONTH, BASIS_AVERAGE_3, BASIS_AVERAGE_6,
    BASIS_AVERAGE_12, BASIS_SAME_MONTH_LAST_YEAR, BASIS_XERO_BUDGET, BASIS_MANUAL,
}

_DEFAULT_BASIS = BASIS_AVERAGE_3  # sensible default before a client configures one


@dataclass(frozen=True)
class SalesTargetConfig:
    basis: str = _DEFAULT_BASIS
    adjustment_pct: float = 0.0          # e.g. +10 → target × 1.10
    manual_value: Optional[float] = None  # only used when basis == manual

    @property
    def has_target(self) -> bool:
        return self.basis != BASIS_NONE


def parse_config(raw: Optional[dict[str, Any]]) -> SalesTargetConfig:
    """Build a config from a stored dict, defaulting any missing/invalid field."""
    raw = raw or {}
    basis = str(raw.get("basis") or _DEFAULT_BASIS).strip().lower()
    if basis not in _VALID_BASES:
        basis = _DEFAULT_BASIS
    try:
        adjustment = float(raw.get("adjustment_pct") or 0.0)
    except (TypeError, ValueError):
        adjustment = 0.0
    raw_manual = raw.get("manual_value")
    try:
        manual = float(raw_manual) if raw_manual is not None else None
    except (TypeError, ValueError):
        manual = None
    return SalesTargetConfig(
        basis=basis, adjustment_pct=adjustment, manual_value=manual,
    )
