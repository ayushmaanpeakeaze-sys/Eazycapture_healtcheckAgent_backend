"""Opening Balance Differences — runtime orchestration.

Ties the pure cores (``app.modules.healthcheck.engine.opening_balance``) +
Companies House integration + manual entries (stored on ``audit_config``) into
the data the frontend renders:

  * ``list_differences`` — one row per filed period end: Net Assets (filed) vs
    Net Assets (Xero BalanceSheet at that date) → Difference.
  * ``late_transactions`` — drill-down: transactions dated in the closed period
    but posted most recently.
  * ``set_filed_net_assets`` / ``set_registration_number`` — manual config used
    when Companies House isn't connected (no API key).
  * ``dismiss`` / ``restore`` — hide/unhide a period.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import UUID

from app.modules.healthcheck.services.company_config import CompanyConfigStore
from app.modules.healthcheck.xero_links import xero_deep_link
from app.modules.healthcheck.tasks import _xero_date
from app.modules.integrations.companies_house.service import (
    CompaniesHouseService,
    FiledNetAssets,
)
from app.modules.integrations.nango.client import NangoAuthError
from app.modules.integrations.service import IntegrationService
from app.modules.healthcheck.engine.audit_settings import AuditSettings
from app.modules.healthcheck.engine.opening_balance import (
    LateTransaction,
    compute_opening_balance_diffs,
    extract_net_assets_from_balance_sheet,
    find_late_transactions,
)

logger = logging.getLogger("eazycapture.opening_balance_service")


def _dec(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


@dataclass
class _LateTx:
    """Lightweight view fed to find_late_transactions (raw Xero invoice)."""
    transaction_id: str
    date: Optional[str]
    posted_date: Optional[str]
    amount: Decimal
    type: Optional[str]


class OpeningBalanceService:
    def __init__(
        self,
        db,
        integration: Optional[IntegrationService] = None,
        companies_house: Optional[CompaniesHouseService] = None,
    ) -> None:
        self._db = db
        self._store = CompanyConfigStore(db)
        self._integration = integration or IntegrationService()
        self._ch = companies_house or CompaniesHouseService()

    # --- read -------------------------------------------------------------
    async def list_differences(
        self, company_id: UUID, *, include_dismissed: bool = False,
    ) -> dict[str, Any]:
        company, cfg = await self._store.load(company_id)
        if company is None:
            return {"total_value": 0.0, "items": [], "ch_connected": self._ch.is_enabled()}

        conn = getattr(company, "nango_connection_id", None)
        tenant = getattr(company, "xero_tenant_id", None)
        ob = self._store.opening_balance(cfg)
        reg_no = self._store.registration_number(cfg)
        settings = AuditSettings.from_config(cfg.get("settings"))

        filed = await self._collect_filed(reg_no, ob)
        if not filed:
            return {
                "total_value": 0.0, "items": [],
                "ch_connected": self._ch.is_enabled(),
                "registration_number": reg_no,
            }

        # Net Assets per Xero at each filed period end. A dead token surfaces as
        # NangoAuthError; report not-connected rather than a false "no differences".
        xero_na: dict[str, Optional[Decimal]] = {}
        try:
            for f in filed:
                report = await self._integration.fetch_balance_sheet(conn, tenant, f.period_end)
                xero_na[f.period_end] = extract_net_assets_from_balance_sheet(report)
        except NangoAuthError:
            return {"total_value": 0.0, "items": [], "ch_connected": self._ch.is_enabled(),
                    "registration_number": reg_no, "connected": False}

        diffs = compute_opening_balance_diffs(
            filed, xero_na, min_difference=settings.opening_balance_min_difference)

        dismissed = set(ob.get("dismissed") or [])
        items, total = [], Decimal("0")
        for d in diffs:
            is_dismissed = d.period_end in dismissed
            if is_dismissed and not include_dismissed:
                continue
            if not is_dismissed:
                total += d.abs_difference
            items.append({
                "id": d.period_end,                       # period end is the stable key
                "period_end": d.period_end,
                "net_assets_filed": float(d.net_assets_filed),
                "net_assets_xero": float(d.net_assets_xero),
                "difference": float(d.difference),
                "filed_source": d.filed_source,
                "filed_document_url": next(
                    (f.document_url for f in filed if f.period_end == d.period_end), None),
                "dismissed": is_dismissed,
            })
        return {
            "total_value": float(total),
            "items": items,
            "ch_connected": self._ch.is_enabled(),
            "registration_number": reg_no,
            "connected": True,
        }

    async def _collect_filed(
        self, reg_no: Optional[str], ob: dict[str, Any],
    ) -> list[FiledNetAssets]:
        """Companies House figures (if connected) overlaid with manual entries
        (manual wins — the user explicitly entered/corrected them)."""
        by_period: dict[str, FiledNetAssets] = {}
        if self._ch.is_enabled() and reg_no:
            for f in await self._ch.fetch_filed_net_assets(reg_no):
                by_period[f.period_end] = f
        for period, raw in (ob.get("filed") or {}).items():
            val = _dec(raw)
            if val is not None:
                by_period[period] = FiledNetAssets(
                    period_end=period, net_assets=val, source="manual")
        return sorted(by_period.values(), key=lambda f: f.period_end, reverse=True)

    async def late_transactions(
        self, company_id: UUID, period_end: str, *, limit: int = 5, offset: int = 0,
    ) -> dict[str, Any]:
        company, _ = await self._store.load(company_id)
        if company is None:
            return {"period_end": period_end, "total": 0, "items": []}
        conn = getattr(company, "nango_connection_id", None)
        tenant = getattr(company, "xero_tenant_id", None)
        shortcode = getattr(company, "xero_shortcode", None)

        raw_docs = await self._integration.fetch_all_invoices(conn, tenant) if \
            self._integration.is_connected(conn, tenant) else []
        rows = [
            _LateTx(
                transaction_id=str(doc.get("InvoiceID") or ""),
                date=_xero_date(doc.get("Date")),
                posted_date=_xero_date(doc.get("UpdatedDateUTC")),
                amount=_dec(doc.get("Total")) or Decimal("0"),
                type=doc.get("Type"),
            )
            for doc in raw_docs if isinstance(doc, dict)
        ]
        page, total = find_late_transactions(rows, period_end, limit=limit, offset=offset)
        return {
            "period_end": period_end,
            "total": total,
            "items": [self._late_row(t, shortcode) for t in page],
        }

    @staticmethod
    def _late_row(t: LateTransaction, shortcode: Optional[str]) -> dict[str, Any]:
        type_to_xero = {"Invoice": "ACCREC", "Bill": "ACCPAY", "Credit Note": "ACCRECCREDIT"}
        return {
            "transaction_id": t.transaction_id,
            "type_label": t.type_label,
            "amount": float(t.amount),
            "accounting_date": t.accounting_date,
            "posted_date": t.posted_date,
            "xero_url": xero_deep_link(
                type_to_xero.get(t.type_label), t.transaction_id, shortcode),
        }

    # --- write ------------------------------------------------------------
    async def set_registration_number(self, company_id: UUID, number: str) -> None:
        company, cfg = await self._store.load(company_id)
        if company is None:
            return
        cfg["registration_number"] = (number or "").strip()
        await self._store.save(company, cfg)

    async def set_filed_net_assets(
        self, company_id: UUID, period_end: str, net_assets: Decimal,
    ) -> None:
        company, cfg = await self._store.load(company_id)
        if company is None:
            return
        ob = dict(self._store.opening_balance(cfg))
        filed = dict(ob.get("filed") or {})
        filed[period_end] = str(net_assets)
        ob["filed"] = filed
        cfg["opening_balance"] = ob
        await self._store.save(company, cfg)

    async def dismiss(self, company_id: UUID, period_end: str) -> None:
        await self._toggle_dismissed(company_id, period_end, add=True)

    async def restore(self, company_id: UUID, period_end: str) -> None:
        await self._toggle_dismissed(company_id, period_end, add=False)

    async def _toggle_dismissed(self, company_id: UUID, period_end: str, *, add: bool) -> None:
        company, cfg = await self._store.load(company_id)
        if company is None:
            return
        ob = dict(self._store.opening_balance(cfg))
        dismissed = set(ob.get("dismissed") or [])
        dismissed.add(period_end) if add else dismissed.discard(period_end)
        ob["dismissed"] = sorted(dismissed)
        cfg["opening_balance"] = ob
        await self._store.save(company, cfg)
