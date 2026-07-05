"""Sales Tracker — monthly actual sales vs a configurable target.

Split by concern (the PATTERN for new Insights features — each piece stays
small and independently testable/debuggable):

  config.py    — target-strategy settings (the 8 options + % adjust)
  targets.py   — resolve a per-period target from history (the 8 strategies)
  chart.py     — the monthly actual-vs-target chart (last 5 months)
  analysis.py  — current-month analysis (status, % of target, remaining, days)
  tracker.py   — orchestrator that assembles the full payload

Callers import only the package:  ``from ...sales_tracker import compute_sales_tracker``
"""
from app.modules.insights.logic.sales_tracker.tracker import compute_sales_tracker

__all__ = ["compute_sales_tracker"]
