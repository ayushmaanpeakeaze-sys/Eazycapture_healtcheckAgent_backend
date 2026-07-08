"""Revenue vs Capital review (``revenue_vs_capital``) — the SOP content-based check.

A P&L EXPENSE line above the threshold whose DESCRIPTION / reference / supplier
name reads like a capital purchase (a capital keyword such as laptop / machinery
/ furniture, or a known capital supplier such as Dell / IKEA) — a possible fixed
asset that should be reviewed for capitalisation instead of being expensed.
Obvious revenue spend (repairs / servicing / fuel / insurance / consumables) is
excluded first.

Content-driven: it does NOT care which account the line sits on — that is the
sibling ``capital_item_review`` check's job. The two never double-flag the same
line (``capital_item_review`` steps aside whenever a content signal is present).
Always a review flag, never auto-capitalised.

Detection logic + settings + registry metadata live here; the keyword / exclusion
/ supplier master lists live in ``capital_keywords.py``.
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


def _settings(settings):
    if settings is None:
        from app.modules.healthcheck.engine.audit_settings import DEFAULT_SETTINGS
        return DEFAULT_SETTINGS
    return settings


def _find_revenue_vs_capital(
    transactions: list[BatchTransaction],
    coa_lookup: dict[str, str],
    coa_type_lookup: dict[str, str],
    settings=None,
) -> list[FlaggedIssue]:
    """Flag a P&L EXPENSE line above the threshold whose description / reference /
    supplier looks like a capital purchase (a capital keyword or a known capital
    supplier), unless the text is obvious revenue spend. Review-only."""
    settings = _settings(settings)
    threshold = settings.revenue_vs_capital_threshold
    flagged: list[FlaggedIssue] = []
    for tx in transactions:
        currency = (tx.currency_code or "GBP").strip().upper()
        symbol = "£" if currency == "GBP" else f"{currency} "
        supplier = (tx.vendor_name or "").strip()
        doc_text = " ".join(p for p in (tx.description, tx.reference, supplier) if p)
        if has_revenue_exclusion(doc_text):
            continue  # SOP exclusions: repairs / servicing / fuel / insurance
        keyword = matched_capital_keyword(doc_text)
        supplier_hit = matched_capital_supplier(supplier)
        if not (keyword or supplier_hit):
            continue  # no capital signal in the description / supplier
        for line_no, code, amount in _account_lines(tx):
            code = (code or "").strip()
            if not code or amount is None:
                continue
            if (coa_type_lookup.get(code) or "").strip().upper() not in _EXPENSE_ACCOUNT_TYPES:
                continue  # SOP: P&L expense accounts only
            amt = abs(amount)
            if amt <= threshold:
                continue  # SOP: amount threshold — low-value items ignored
            name = coa_lookup.get(code) or code
            signals: list[str] = []
            if keyword:
                signals.append(f"description mentions '{keyword}'")
            if supplier_hit:
                signals.append(f"supplier '{supplier}' commonly sells assets")
            why = "; ".join(signals)
            flagged.append(FlaggedIssue(
                transaction_id=tx.transaction_id,
                issue_type="revenue_vs_capital",
                severity="medium",
                message=(
                    f"{supplier or 'This'}: {symbol}{amt:.2f} on expense account "
                    f"{code} ({name}) — {why}. Review whether to capitalise as a "
                    f"fixed asset instead of expensing."
                )[:200],
                current_code=code,
                reasoning=(
                    f"{symbol}{amt:.2f} is above the {symbol}{threshold:.0f} capital "
                    f"threshold and {why}, so this may be a capital item that should "
                    f"be a FIXED asset (capitalised + depreciated), not expensed in "
                    f"one go. Recommended: review and, if it is an asset, re-code it "
                    f"to a fixed-asset account."
                ),
                match_reasons={
                    "line_no": line_no,
                    "account_code": code,
                    "account_name": name,
                    "current_account_type": "EXPENSE",
                    "line_amount": f"{amt:.2f}",
                    "threshold": f"{threshold:.2f}",
                    "currency": currency,
                    "matched_keyword": keyword,
                    "matched_supplier": supplier_hit,
                    "recommended_action": "capitalise",
                    "recode_to_account_type": "FIXED",
                },
            ))
    return flagged


# --- settings (gear) ---------------------------------------------------------
SETTING_FIELDS: tuple[SettingField, ...] = (
    SettingField("revenue_vs_capital_threshold", "Fixed Assets", "revenue_vs_capital",
                 "Flag expense over …", "amount",
                 "Review any P&L expense above this amount whose description, "
                 "reference or supplier looks like a capital purchase (laptop, "
                 "machinery, furniture, vehicle …) — it may belong in fixed assets. "
                 "Obvious revenue spend (repairs, fuel, insurance) is excluded. "
                 "Default 500.",
                 unit="currency", min=0, step=100),
)

# --- registry metadata (key, label, built) -----------------------------------
META: tuple[tuple[str, str, bool], ...] = (
    ("revenue_vs_capital", "Revenue vs Capital review", True),
)
