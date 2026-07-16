"""Assemble the prepayment schedule for one company: synced Prepayments lines +
the real account balance from the Xero Trial Balance action. Review-only."""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.integrations.service import IntegrationService
from app.modules.integrations.sync.models import XeroDocument
from app.modules.insights.logic.bank import _parse_trial_balance_balances
from app.modules.healthcheck.checks.prepayment_schedule import (
    build_prepayment_schedule,
    collect_prepayment_items,
    is_prepayment_account,
    prepayment_balance_from_trial_balance,
)

logger = logging.getLogger("eazycapture.prepayment_schedule")


class PrepaymentScheduleService:
    def __init__(self, db: AsyncSession, integration: Optional[IntegrationService] = None) -> None:
        self._db = db
        self._integration = integration or IntegrationService()

    async def build(self, company: Any, year_end: date, months: int = 12) -> dict:
        """Build the reconciled prepayment schedule for ``company`` at ``year_end``."""
        accounts = await self._read(company.id, "account")
        # Bills + Spend Money — a prepayment can be paid either way.
        documents = (await self._read(company.id, "invoice")
                     + await self._read(company.id, "bank_transaction"))
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

        items = collect_prepayment_items(documents, coa_name, coa_type)

        ledger_balance = await self._ledger_balance(company, year_end, prepay_codes)

        schedule = build_prepayment_schedule(
            items, year_end, months=months, ledger_balance=ledger_balance,
        )
        schedule["prepayment_accounts"] = sorted(prepay_codes)
        schedule["item_count"] = len(items)
        return schedule

    async def _read(self, company_id, entity: str) -> list[dict]:
        """Raw synced Xero records for one company + entity."""
        rows = (await self._db.scalars(
            select(XeroDocument.raw_json).where(
                XeroDocument.company_id == company_id,
                XeroDocument.entity == entity,
            )
        )).all()
        return [r for r in rows if isinstance(r, dict)]

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
