"""Fixed Assets check group.

Two checks, each = account TYPE + line AMOUNT, pure deterministic (no LLM):
  * ``low_cost_fixed_asset``  — a line in a FIXED-asset account BELOW the
    capitalisation threshold (probably should be expensed).
  * ``capital_item_review``   — a line in a monitored EXPENSE account ABOVE the
    threshold (may really be a capital item / fixed asset).

Detection logic + tunable settings + registry metadata all live here.
"""
from __future__ import annotations

from app.modules.healthcheck.checks.base import SettingField
from app.shared.transaction import BatchTransaction, FlaggedIssue
from app.modules.healthcheck.engine.shared import _account_lines, _EXPENSE_ACCOUNT_TYPES

_FIXED_ASSET_TYPES = frozenset({"FIXED", "FIXEDASSET"})
_CAPITAL_REVIEW_KEYWORDS = ("repair", "maintenance", "printing", "stationery")


def _settings(settings):
    """Resolve the default settings lazily — avoids importing audit_settings at
    module load (which would cycle through deterministic)."""
    if settings is None:
        from app.modules.healthcheck.engine.audit_settings import DEFAULT_SETTINGS
        return DEFAULT_SETTINGS
    return settings


def _find_low_cost_fixed_assets(
    transactions: list[BatchTransaction],
    coa_type_lookup: dict[str, str],
    coa_lookup: dict[str, str],
    settings=None,
) -> list[FlaggedIssue]:
    """Flag a transaction line posted to a FIXED-ASSET account for an amount BELOW
    the capitalisation threshold (``low_cost_asset_max``, default £10k). Such
    items should usually be expensed, not capitalised."""
    settings = _settings(settings)
    threshold = settings.low_cost_asset_max
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        currency = (tx.currency_code or "GBP").strip().upper()
        symbol = "£" if currency == "GBP" else f"{currency} "
        for line_no, code, amount in _account_lines(tx):
            code = (code or "").strip()
            if not code or amount is None:
                continue
            if (coa_type_lookup.get(code) or "").strip().upper() not in _FIXED_ASSET_TYPES:
                continue
            amt = abs(amount)
            if amt <= 0 or amt >= threshold:
                continue
            name = coa_lookup.get(code) or code
            flagged.append(FlaggedIssue(
                transaction_id=tx.transaction_id,
                issue_type="low_cost_fixed_asset",
                severity="medium",
                message=(
                    f"{tx.vendor_name}: {symbol}{amt:.2f} posted to fixed-asset "
                    f"account {code} ({name}) — below the {symbol}{threshold:.0f} "
                    f"capitalisation threshold; consider expensing instead."
                )[:200],
                current_code=code,
                reasoning=(
                    f"This line sits in {code} ({name}) — a fixed-asset account — "
                    f"but {symbol}{amt:.2f} is below your {symbol}{threshold:.0f} "
                    f"capitalisation threshold, so it is usually too small to "
                    f"capitalise. Recommended: re-code it to an EXPENSE account. "
                    f"There is no single correct expense account, so this is a "
                    f"suggestion to review (hence the '?'), not a one-click fix — "
                    f"pick the expense account that fits."
                ),
                match_reasons={
                    "line_no": line_no,
                    "account_code": code,
                    "account_name": name,
                    "current_account_type": "FIXED",
                    "line_amount": f"{amt:.2f}",
                    "threshold": f"{threshold:.2f}",
                    "currency": currency,
                    "recommended_action": "expense",
                    "recode_to_account_type": "EXPENSE",
                },
            ))
    return flagged


def _find_capital_items(
    transactions: list[BatchTransaction],
    coa_lookup: dict[str, str],
    coa_type_lookup: dict[str, str],
    settings=None,
) -> list[FlaggedIssue]:
    """Mirror of low_cost_fixed_asset: a line posted to a MONITORED EXPENSE
    account for an amount ABOVE the threshold (``capital_item_threshold``) — it
    may really be a capital item (fixed asset) mis-coded to an expense.

    Monitored = the codes in ``capital_monitored_accounts`` when set, else any
    EXPENSE-type account whose name looks capital-suspicious (repairs / printing
    / maintenance / stationery)."""
    settings = _settings(settings)
    threshold = settings.capital_item_threshold
    monitored = {c.strip().upper() for c in settings.capital_monitored_accounts if c.strip()}
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        currency = (tx.currency_code or "GBP").strip().upper()
        symbol = "£" if currency == "GBP" else f"{currency} "
        for line_no, code, amount in _account_lines(tx):
            code = (code or "").strip()
            if not code or amount is None:
                continue
            name = coa_lookup.get(code) or code
            if monitored:
                if code.upper() not in monitored:
                    continue
            else:
                if (coa_type_lookup.get(code) or "").strip().upper() not in _EXPENSE_ACCOUNT_TYPES:
                    continue
                if not any(k in name.lower() for k in _CAPITAL_REVIEW_KEYWORDS):
                    continue
            amt = abs(amount)
            if amt <= threshold:
                continue
            flagged.append(FlaggedIssue(
                transaction_id=tx.transaction_id,
                issue_type="capital_item_review",
                severity="medium",
                message=(
                    f"{tx.vendor_name}: {symbol}{amt:.2f} posted to expense account "
                    f"{code} ({name}) — above the {symbol}{threshold:.0f} threshold; "
                    f"may be a capital item (fixed asset), not an expense."
                )[:200],
                current_code=code,
                reasoning=(
                    f"This line sits in {code} ({name}) — an expense account — "
                    f"but {symbol}{amt:.2f} is above your {symbol}{threshold:.0f} "
                    f"threshold, so it may really be a capital item that should be "
                    f"a FIXED asset (capitalised + depreciated), not expensed in "
                    f"one go. Recommended: review and, if it is an asset, re-code "
                    f"it to a fixed-asset account. There is no single correct "
                    f"target, so this is a suggestion to review (hence the '?')."
                ),
                match_reasons={
                    "line_no": line_no,
                    "account_code": code,
                    "account_name": name,
                    "current_account_type": "EXPENSE",
                    "line_amount": f"{amt:.2f}",
                    "threshold": f"{threshold:.2f}",
                    "currency": currency,
                    "recommended_action": "capitalise",
                    "recode_to_account_type": "FIXED",
                },
            ))
    return flagged


# --- settings (gear) ---------------------------------------------------------
SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("low_cost_asset_max", "Fixed Assets", "low_cost_fixed_asset",
                 "Capitalisation threshold", "amount",
                 "Flag any line (invoice, bill, Money In or Money Out) posted to "
                 "a FIXED-asset account for LESS than this amount — too cheap to "
                 "capitalise, probably should be expensed. Set it to your "
                 "organisation's capitalisation policy. Default 10,000.",
                 unit="currency", min=0, step=100),
    SettingField("capital_item_threshold", "Fixed Assets", "capital_item_review",
                 "Flag expense over …", "amount",
                 "Flag a line posted to a monitored EXPENSE account for more than "
                 "this amount — it may really be a capital item (fixed asset). "
                 "Mirror of the low-cost fixed-asset check. Default 5,000.",
                 unit="currency", min=0, step=100),
    SettingField("capital_monitored_accounts", "Fixed Assets", "capital_item_review",
                 "Monitored expense accounts", "list",
                 "Account CODES to watch for capital items (e.g. 461 Printing, "
                 "473 Repairs). Leave empty to auto-watch any expense account whose "
                 "name looks capital-suspicious (repairs / maintenance / printing / "
                 "stationery)."),
)

# --- registry metadata (key, label, built) -----------------------------------
META: tuple[tuple[str, str, bool], ...] = (
    ("low_cost_fixed_asset", "Low-cost fixed asset", True),
    ("capital_item_review", "Capital item review", True),
)
