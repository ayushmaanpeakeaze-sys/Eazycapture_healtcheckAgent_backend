"""Corporation Tax estimate (UK) — pure logic.

Applies the FY2023+ UK corporation-tax rules to a net-profit figure:
  * profit <= £50,000        → 19% (small profits rate)
  * profit >= £250,000       → 25% (main rate)
  * in between               → 25% minus marginal relief (3/200 fraction)

This is an ESTIMATE before tax adjustments (add-backs like depreciation /
client entertainment, capital allowances, losses brought forward). Those need
accountant input — flagged in the response. Assumes a full 12-month period and
no associated companies (which would lower the £50k/£250k thresholds).
"""
from __future__ import annotations

from typing import Any

_LOWER_LIMIT = 50_000.0
_UPPER_LIMIT = 250_000.0
_MARGINAL_FRACTION = 3 / 200
_SMALL_RATE = 0.19
_MAIN_RATE = 0.25


def estimate_corporation_tax(net_profit: float) -> dict[str, Any]:
    p = float(net_profit or 0.0)
    if p <= 0:
        return {
            "taxable_profit": round(p, 2),
            "tax_estimate": 0.0,
            "band": "loss / no tax",
            "effective_rate": 0.0,
        }
    if p <= _LOWER_LIMIT:
        tax = p * _SMALL_RATE
        band = "small profits rate (19%)"
    elif p >= _UPPER_LIMIT:
        tax = p * _MAIN_RATE
        band = "main rate (25%)"
    else:
        marginal_relief = (_UPPER_LIMIT - p) * _MARGINAL_FRACTION
        tax = p * _MAIN_RATE - marginal_relief
        band = "marginal relief (19–25%)"
    return {
        "taxable_profit": round(p, 2),
        "tax_estimate": round(tax, 2),
        "band": band,
        "effective_rate": round(tax / p * 100, 2),
    }
