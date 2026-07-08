"""Fixed Assets check group.

Two account-based checks here, each = account TYPE + line AMOUNT, pure
deterministic (no LLM):
  * ``low_cost_fixed_asset``  — a line in a FIXED-asset account BELOW the
    capitalisation threshold (probably should be expensed).
  * ``capital_item_review``   — a P&L EXPENSE line above the threshold posted to a
    monitored / capital-suspicious account (repairs / printing …), obvious revenue
    spend excluded — a possible capital item hiding in an expense account.

The content-based sibling ``revenue_vs_capital`` (the SOP keyword / supplier
engine) lives in its own module; this check steps aside when a content signal is
present so the two never double-flag a line.

Detection logic + tunable settings + registry metadata all live here.
"""
from __future__ import annotations

from app.modules.healthcheck.checks.base import SettingField
from app.shared.transaction import BatchTransaction, FlaggedIssue
from app.modules.healthcheck.engine.shared import _account_lines, _EXPENSE_ACCOUNT_TYPES
from app.modules.healthcheck.checks.capital_keywords import (
    has_revenue_exclusion,
    matched_capital_keyword,
    matched_capital_supplier,
)

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
    """Capital item review (account-based): a P&L EXPENSE line above the threshold
    posted to a MONITORED / capital-suspicious expense account (repairs / printing
    / stationery … or an explicit ``capital_monitored_accounts`` code) — a possible
    capital item hiding in an expense account.

    Account-driven — it flags on WHERE the line sits. Content signals (a capital
    keyword or known supplier in the description) belong to the sibling
    ``revenue_vs_capital`` check, so this one STEPS ASIDE whenever such a signal is
    present: a line is never flagged by both. Obvious revenue spend (repairs /
    servicing / fuel / insurance / consumables) is excluded. Review-only.
    """
    settings = _settings(settings)
    threshold = settings.capital_item_threshold
    monitored = {c.strip().upper() for c in settings.capital_monitored_accounts if c.strip()}
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        currency = (tx.currency_code or "GBP").strip().upper()
        symbol = "£" if currency == "GBP" else f"{currency} "
        supplier = (tx.vendor_name or "").strip()
        doc_text = " ".join(p for p in (tx.description, tx.reference, supplier) if p)
        if has_revenue_exclusion(doc_text):
            continue  # SOP exclusions: repairs / fuel / insurance / consumables
        # A content signal (keyword/supplier) is revenue_vs_capital's territory —
        # step aside so the same line is never double-flagged by both checks.
        if matched_capital_keyword(doc_text) or matched_capital_supplier(supplier):
            continue
        for line_no, code, amount in _account_lines(tx):
            code = (code or "").strip()
            if not code or amount is None:
                continue
            if (coa_type_lookup.get(code) or "").strip().upper() not in _EXPENSE_ACCOUNT_TYPES:
                continue  # P&L expense accounts only
            amt = abs(amount)
            if amt <= threshold:
                continue  # amount threshold — low-value items ignored
            name = coa_lookup.get(code) or code
            if monitored:                       # explicit codes configured → only those
                if code.upper() not in monitored:
                    continue
            elif not any(k in name.lower() for k in _CAPITAL_REVIEW_KEYWORDS):
                continue                         # else auto-watch capital-suspicious names
            flagged.append(FlaggedIssue(
                transaction_id=tx.transaction_id,
                issue_type="capital_item_review",
                severity="medium",
                message=(
                    f"{supplier or 'This'}: {symbol}{amt:.2f} on monitored expense "
                    f"account {code} ({name}) — above the {symbol}{threshold:.0f} "
                    f"threshold. Review whether it is a capital item to capitalise."
                )[:200],
                current_code=code,
                reasoning=(
                    f"{symbol}{amt:.2f} sits on {code} ({name}) — an expense account "
                    f"watched for capital items — and is above the "
                    f"{symbol}{threshold:.0f} threshold. A purchase this size on this "
                    f"account may be a FIXED asset mis-coded to an expense. "
                    f"Recommended: review and, if it is an asset, re-code it to a "
                    f"fixed-asset account."
                ),
                match_reasons={
                    "line_no": line_no,
                    "account_code": code,
                    "account_name": name,
                    "current_account_type": "EXPENSE",
                    "line_amount": f"{amt:.2f}",
                    "threshold": f"{threshold:.2f}",
                    "currency": currency,
                    "monitored_account": True,
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
                 "Review any P&L expense above this amount posted to a monitored / "
                 "capital-suspicious account (repairs, printing …) — it may be a "
                 "capital item mis-coded to an expense. Obvious revenue spend "
                 "(repairs, fuel, insurance) is excluded. Default 500.",
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
