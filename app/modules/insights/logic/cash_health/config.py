"""Cash Health Check configuration — the per-client settings + the fixed
category model behind the "Enough cash to pay?" checklist.

Eight outgoing categories, ordered SHORT-TERM first (these get more weight in
the health indicator — being unable to pay a supplier this week matters more
than a multi-year loan). Each liability account from Xero is sorted into one of
these; anything unrecognised falls to "Loans & Other".

Settings live in JSON on ``company.audit_config['cash_health']`` so they can be
parsed without a schema dependency here (mirrors the Sales Tracker config).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# --- the eight categories (short-term first) -------------------------------
SUPPLIERS = "suppliers"
NET_WAGES = "net_wages"
PAYE_NIC = "paye_nic"
PENSION = "pension"
CREDIT_CARDS = "credit_cards"
VAT = "vat"
CORPORATION_TAX = "corporation_tax"
LOANS_OTHER = "loans_other"

# Render/priority order — cash is applied down this list in the "can we pay?"
# walk, so the most pressing outgoings are covered first.
CATEGORY_ORDER = [
    SUPPLIERS, NET_WAGES, PAYE_NIC, PENSION, CREDIT_CARDS, VAT,
    CORPORATION_TAX, LOANS_OTHER,
]

CATEGORY_LABELS = {
    SUPPLIERS: "Suppliers",
    NET_WAGES: "Net Wages",
    PAYE_NIC: "PAYE & NIC",
    PENSION: "Pension Contributions",
    CREDIT_CARDS: "Credit Cards",
    VAT: "VAT Bill",
    CORPORATION_TAX: "Corporation Tax",
    LOANS_OTHER: "Loans & Other",
}

# Short-term outgoings carry more weight in the health indicator than the
# long-term ones (the premise of the check — short-term liquidity matters most).
_SHORT_TERM = {SUPPLIERS, NET_WAGES, PAYE_NIC, PENSION, CREDIT_CARDS, VAT}
_WEIGHT_SHORT = 3.0
_WEIGHT_LONG = 1.0


def category_weight(category: str) -> float:
    """Health-indicator weight for a category (short-term > long-term)."""
    return _WEIGHT_SHORT if category in _SHORT_TERM else _WEIGHT_LONG


# --- default categorisation (UK default chart of accounts + Xero demo) ------
# Matched on (lowercased name keyword) OR (exact account code) OR Xero metadata.
# Order matters: more specific rules first (credit-card before supplier, etc.).
_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    (CREDIT_CARDS, ("credit card",)),
    (VAT, ("vat", "sales tax", "gst")),
    (PAYE_NIC, ("paye", "p.a.y.e", "nic", "national insurance", "employee tax", "payg")),
    (NET_WAGES, ("net wage", "wages payable", "net pay", "salary payable", "payroll payable")),
    (PENSION, ("pension", "superannuation", "nest", "auto enrol")),
    (CORPORATION_TAX, ("corporation tax", "corp tax", "income tax payable", "company tax")),
    (SUPPLIERS, ("accounts payable", "trade payable", "trade creditor", "creditor",
                 "unpaid expense", "supplier")),
]
# UK/Xero default chart-of-accounts codes → category, used only as a fallback
# after the account name (codes are reused for different purposes across orgs).
_CODE_MAP = {
    "800": SUPPLIERS, "801": SUPPLIERS,
    "814": NET_WAGES,
    "820": VAT,
    "825": PAYE_NIC,
    "830": CORPORATION_TAX,
}

# Short, ambiguous tokens are matched on a word boundary so they don't false-hit
# inside longer words ("vat" in innovation, "nic" in technician).
_WORD_BOUNDARY = {"vat", "gst", "nic", "paye", "payg", "p.a.y.e", "corp tax"}


def _name_matches(word: str, low: str) -> bool:
    if word in _WORD_BOUNDARY:
        return re.search(r"\b" + re.escape(word) + r"\b", low) is not None
    return word in low


def default_category(
    name: str,
    code: Optional[str],
    bank_account_type: Optional[str] = None,
    system_account: Optional[str] = None,
) -> str:
    """Sort one liability account into a category. Unrecognised → Loans & Other.

    Precedence: Xero metadata (credit-card bank type / AP system account) →
    name keywords → code map (fallback) → Loans & Other. The NAME is trusted
    before the code because account codes are reused for different purposes
    across orgs.
    """
    bat = (bank_account_type or "").strip().upper()
    if bat == "CREDITCARD":
        return CREDIT_CARDS
    if (system_account or "").strip().upper() == "ACCPAY":
        return SUPPLIERS

    low = (name or "").lower()
    for category, words in _KEYWORDS:
        if any(_name_matches(w, low) for w in words):
            return category

    code = (code or "").strip()
    if code in _CODE_MAP:
        return _CODE_MAP[code]
    return LOANS_OTHER


# --- per-client settings ----------------------------------------------------

@dataclass(frozen=True)
class CashHealthConfig:
    # category -> include in the outgoings total (default: all included)
    included: dict[str, bool] = field(default_factory=dict)
    # category -> manual override value (replaces the auto Xero figure)
    overrides: dict[str, float] = field(default_factory=dict)
    # account code -> category (re-assign an account to a different category)
    account_overrides: dict[str, str] = field(default_factory=dict)
    # bank account codes to leave OUT of the current-cash figure (e.g. personal)
    disregarded_banks: frozenset[str] = field(default_factory=frozenset)

    def is_included(self, category: str) -> bool:
        return self.included.get(category, True)

    def override_for(self, category: str) -> Optional[float]:
        return self.overrides.get(category)


def _coerce_float(v: Any) -> Optional[float]:
    if v is None or v == "" or isinstance(v, bool):
        return None   # reject bools: float(True) would silently become 1.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # reject nan/inf — they poison totals and every >= comparison downstream
    if not math.isfinite(f):
        return None
    return f


def parse_config(raw: Optional[dict[str, Any]]) -> CashHealthConfig:
    """Build a config from a stored dict, defaulting/ignoring malformed fields."""
    raw = raw or {}

    included = {
        c: bool(raw.get("included", {}).get(c, True)) for c in CATEGORY_ORDER
    } if isinstance(raw.get("included"), dict) else {}

    overrides: dict[str, float] = {}
    if isinstance(raw.get("overrides"), dict):
        for c, v in raw["overrides"].items():
            fv = _coerce_float(v)
            if c in CATEGORY_LABELS and fv is not None:
                overrides[c] = fv

    account_overrides: dict[str, str] = {}
    if isinstance(raw.get("account_overrides"), dict):
        for code, cat in raw["account_overrides"].items():
            if cat in CATEGORY_LABELS:
                account_overrides[str(code).strip()] = cat

    disregarded = raw.get("disregarded_banks") or []
    disregarded_banks = frozenset(
        str(c).strip() for c in disregarded if str(c).strip()
    ) if isinstance(disregarded, (list, tuple, set)) else frozenset()

    return CashHealthConfig(
        included=included,
        overrides=overrides,
        account_overrides=account_overrides,
        disregarded_banks=disregarded_banks,
    )
