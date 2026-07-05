"""The "Enough cash to pay?" checklist — group the categorised liabilities
into the eight outgoing categories and walk the cash down them in priority
order (short-term first) to mark each as payable or not (once the cash runs
out, the rest are unpayable).

Per-category the firm can override the auto Xero figure or exclude a category
entirely (the Settings page) — both honoured here.
"""
from __future__ import annotations

from typing import Any

from app.modules.insights.logic.cash_health.config import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    CORPORATION_TAX,
    CashHealthConfig,
)


def build_outgoings(
    liabilities: list[dict[str, Any]],
    current_cash: float,
    config: CashHealthConfig,
    corp_tax_estimate: float = 0.0,
) -> dict[str, Any]:
    """Returns the checklist + totals::

        {
          "categories": [{category, label, amount, auto_amount, included,
                          overridden, can_pay}],   # in priority order
          "total_expected_outgoings": float,       # sum of INCLUDED amounts
          "all_covered": bool,                      # cash >= total
        }

    ``can_pay`` is cumulative: a category is payable only if the cash covers it
    AND everything ahead of it in the list (so the first uncovered category and
    everything after it reads as unpayable).
    """
    # sum owed per category (floor at 0 — a debit-balance liability isn't an
    # amount you owe out)
    auto: dict[str, float] = {c: 0.0 for c in CATEGORY_ORDER}
    for liab in liabilities:
        auto[liab["category"]] = auto.get(liab["category"], 0.0) + liab["owed"]
    auto = {c: round(max(0.0, v), 2) for c, v in auto.items()}

    # Corporation Tax folds in the estimated (often unrecorded) future bill —
    # take the larger of the booked provision and the estimate.
    auto[CORPORATION_TAX] = round(
        max(auto.get(CORPORATION_TAX, 0.0), max(0.0, corp_tax_estimate)), 2,
    )

    categories: list[dict[str, Any]] = []
    cumulative = 0.0
    total = 0.0
    for cat in CATEGORY_ORDER:
        auto_amount = auto.get(cat, 0.0)
        override = config.override_for(cat)
        overridden = override is not None
        # floor overrides at 0, mirroring the auto figures — a negative
        # "amount you owe out" is nonsensical and would make the cumulative
        # walk go DOWN and deflate the total.
        amount = round(max(0.0, override), 2) if overridden else auto_amount
        included = config.is_included(cat)

        can_pay = None
        if included:
            cumulative += amount
            total += amount
            can_pay = current_cash >= round(cumulative, 2)

        categories.append({
            "category": cat,
            "label": CATEGORY_LABELS[cat],
            "amount": amount,
            "auto_amount": auto_amount,
            "included": included,
            "overridden": overridden,
            "can_pay": can_pay,
        })

    total = round(total, 2)
    return {
        "categories": categories,
        "total_expected_outgoings": total,
        "all_covered": current_cash >= total,
    }
