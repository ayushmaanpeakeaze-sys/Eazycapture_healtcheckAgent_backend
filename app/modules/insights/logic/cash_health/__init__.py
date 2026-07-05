"""Cash Health Check — an early-warning view of whether the cash in the bank
alone can cover the upcoming outgoings (ignoring money owed by customers).

Split by concern (the PATTERN for new Insights features — each piece stays
small and independently testable/debuggable):

  config.py      — settings (include/override/account-map/disregard banks) + the
                   eight outgoing categories + the indicator weights
  accounts.py    — parse COA + Trial Balance → current cash + categorised liabilities
  outgoings.py   — the "Enough cash to pay?" checklist + totals
  indicator.py   — the weighted health-% score (short-term > long-term)
  movements.py   — recent cash movements (month-over-month bank-balance change)
  cash_health.py — orchestrator that assembles the full payload

Callers import only the package:  ``from ...cash_health import compute_cash_health``
"""
from app.modules.insights.logic.cash_health.cash_health import compute_cash_health

__all__ = ["compute_cash_health"]
