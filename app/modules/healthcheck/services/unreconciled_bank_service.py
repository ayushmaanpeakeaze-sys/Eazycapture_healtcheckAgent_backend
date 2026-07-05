"""Unreconciled Bank Items — runtime orchestration.

Fetches Xero bank transactions, counts the unreconciled ones per account
(``app.modules.healthcheck.engine.unreconciled_bank``), attaches a Xero "Process"
deep-link, and honours the per-company exclude list (stored on audit_config).
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

from app.modules.healthcheck.services.company_config import CompanyConfigStore
from app.modules.healthcheck.xero_links import xero_deep_link
from app.modules.integrations.service import IntegrationService
from app.modules.healthcheck.engine.unreconciled_bank import compute_unreconciled_accounts

logger = logging.getLogger("eazycapture.unreconciled_bank_service")


def _excluded(cfg: dict[str, Any]) -> set[str]:
    node = cfg.get("unreconciled")
    codes = node.get("excluded") if isinstance(node, dict) else None
    return {str(c).strip().upper() for c in (codes or [])}


class UnreconciledBankService:
    def __init__(self, db, integration: Optional[IntegrationService] = None) -> None:
        self._db = db
        self._store = CompanyConfigStore(db)
        self._integration = integration or IntegrationService()

    async def list_accounts(self, company_id: UUID) -> dict[str, Any]:
        company, cfg = await self._store.load(company_id)
        if company is None:
            return {"total_to_reconcile": 0, "items": []}
        conn = getattr(company, "nango_connection_id", None)
        tenant = getattr(company, "xero_tenant_id", None)
        shortcode = getattr(company, "xero_shortcode", None)

        txns = await self._load_bank_txns(company_id, conn, tenant)
        rows = compute_unreconciled_accounts(txns, exclude_codes=_excluded(cfg))
        total = 0
        for r in rows:
            total += r["total_to_reconcile"]
            r["process_url"] = xero_deep_link("BANK", r["account_id"], shortcode)
        return {
            "total_to_reconcile": total,
            # Honest banner so the UI never implies the feed-side count is zero.
            "unexplained_available": False,
            "items": rows,
        }

    async def _load_bank_txns(
        self, company_id: UUID, conn: Optional[str], tenant: Optional[str],
    ) -> list[dict[str, Any]]:
        """Bank transactions for the unreconciled count.

        Under ``AUDIT_SOURCE=db`` read the SYNCED rows (same source the main
        audit uses) so the count is reliable even when the live Xero token has
        died — the live fetch silently returns 0 on a dead connection, which is
        exactly what made this check flip to "OK" with stale data. Falls back to
        a live fetch only when nothing has been synced yet.
        """
        from app.core.config import settings as _settings

        if _settings.AUDIT_SOURCE == "db":
            from sqlalchemy import select as _select

            from app.modules.integrations.sync.models import XeroDocument

            rows = (
                await self._db.execute(
                    _select(XeroDocument.raw_json).where(
                        XeroDocument.company_id == company_id,
                        XeroDocument.entity == "bank_transaction",
                    )
                )
            ).scalars().all()
            txns = [r for r in rows if isinstance(r, dict)]
            if txns:
                return txns
            # nothing synced yet → fall through to live

        if self._integration.is_connected(conn, tenant):
            return await self._integration.fetch_all_bank_transactions(conn, tenant) or []
        return []

    async def exclude_account(self, company_id: UUID, account_code: str, *, excluded: bool) -> None:
        company, cfg = await self._store.load(company_id)
        if company is None:
            return
        node = dict(cfg.get("unreconciled") or {})
        current = set(node.get("excluded") or [])
        current.add(account_code) if excluded else current.discard(account_code)
        node["excluded"] = sorted(current)
        cfg["unreconciled"] = node
        await self._store.save(company, cfg)
