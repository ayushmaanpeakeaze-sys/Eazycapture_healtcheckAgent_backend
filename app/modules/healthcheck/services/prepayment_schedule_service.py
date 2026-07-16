"""Prepayment schedule service — assemble the working paper for one company.

Ties together the synced Prepayments-account lines (the amortisation grid) and
the ACTUAL account balance read from Xero via the Trial Balance action, so the
schedule reconciles against the real ledger. Nango action-first (the proxy is
only the library's own fallback inside the integration layer); nothing posts.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional
from uuid import UUID

from app.modules.integrations.service import IntegrationService
from app.modules.integrations.sync import db_read
from app.modules.insights.logic.bank import _parse_trial_balance_balances
from app.modules.healthcheck.checks.prepayment_schedule import (
    build_prepayment_schedule,
    collect_prepayment_items,
    is_prepayment_account,
    prepayment_balance_from_trial_balance,
)

logger = logging.getLogger("eazycapture.prepayment_schedule")


class PrepaymentScheduleService:
    def __init__(self, db, integration: Optional[IntegrationService] = None) -> None:
        self._db = db
        self._integration = integration or IntegrationService()

    async def build(self, company: Any, year_end: date, months: int = 12) -> dict:
        """Build the reconciled prepayment schedule for ``company`` at ``year_end``."""
        accounts = db_read.read_raw(self._db, company.id, "account")
        coa_name: dict[str, str] = {}
        coa_type: dict[str, str] = {}
        prepay_codes: set[str] = set()
        for a in accounts:
            code = str(a.get("Code") or "").strip()
            if not code:
                continue
            coa_name[code], coa_type[code] = a.get("Name") or code, a.get("Type") or ""
            if is_prepayment_account(a.get("Name"), a.get("Type")):
                prepay_codes.add(code)

        documents = db_read.read_raw(self._db, company.id, "invoice")
        items = collect_prepayment_items(documents, coa_name, coa_type)

        ledger_balance = await self._ledger_balance(company, year_end, prepay_codes)

        schedule = build_prepayment_schedule(
            items, year_end, months=months, ledger_balance=ledger_balance,
        )
        schedule["prepayment_accounts"] = sorted(prepay_codes)
        schedule["item_count"] = len(items)
        return schedule

    async def _ledger_balance(
        self, company: Any, year_end: date, prepay_codes: set[str],
    ) -> Optional[str]:
        """Real Prepayments-account balance from the Xero Trial Balance action.
        ``None`` (schedule falls back to posted amounts) when the org isn't
        connected or the report scope isn't granted."""
        conn = (getattr(company, "nango_connection_id", "") or "").strip()
        tenant = (getattr(company, "xero_tenant_id", "") or "").strip()
        if not (prepay_codes and self._integration.is_connected(conn, tenant)):
            return None
        try:
            report = await self._integration.fetch_trial_balance(
                conn, tenant, year_end.isoformat(),
            )
        except Exception:  # noqa: BLE001 — best-effort; fall back to posted amounts
            logger.warning("[Prepayment] trial-balance fetch failed for company=%s", company.id)
            return None
        if not report:
            return None
        parsed = _parse_trial_balance_balances(report)
        return str(prepayment_balance_from_trial_balance(parsed, prepay_codes))
