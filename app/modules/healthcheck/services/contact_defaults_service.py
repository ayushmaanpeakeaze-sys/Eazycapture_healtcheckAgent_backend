"""Contact Defaults — list each contact's default account/tax settings, and
write chosen defaults back to Xero (the "Confirm" button).

Powers the Contact Defaults screen and, in turn, the
Unexpected-Account / Unexpected-Tax checks. The four defaults are standard Xero
Contact fields (read + write): SalesDefaultAccountCode, PurchasesDefaultAccountCode,
AccountsReceivableTaxType, AccountsPayableTaxType.

All live calls fail open: with no Nango connection the list comes back empty
with ``connected: false`` rather than erroring, so demos work without creds.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.healthcheck.models import Company, HealthCheckResult
from app.modules.integrations.nango.client import NangoAuthError
from app.modules.integrations.service import IntegrationService
from app.modules.healthcheck.engine.contact_checks import (
    _DEFAULT_FIELD_TO_XERO,
    extract_contact_defaults,
    missing_contact_defaults,
)

logger = logging.getLogger("eazycapture.contact_defaults")

_CONTACT_DEFAULTS_RULE = "contact_defaults"


def to_xero_default_fields(defaults: dict[str, Any]) -> dict[str, str]:
    """{sales_account: '200', sales_tax: 'OUTPUT2', ...} → Xero field names,
    dropping blanks so a partial update only touches the fields provided."""
    out: dict[str, str] = {}
    for key, xero in _DEFAULT_FIELD_TO_XERO.items():
        raw = defaults.get(key)
        val = (str(raw).strip() if raw is not None else "")
        if val:
            out[xero] = val
    return out


class ContactDefaultsService:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._integration = IntegrationService()

    async def _connection(self, company_id: UUID) -> tuple[str, str]:
        company = await self._db.get(Company, company_id)
        if company is None:
            return "", ""
        return (
            (company.nango_connection_id or "").strip(),
            (company.xero_tenant_id or "").strip(),
        )

    async def _contact_trapped_map(
        self, company_id: UUID,
    ) -> dict[str, dict[str, Any]]:
        """{contact_id: {"trapped_row_id", "dismissed"}} for the stored
        contact_defaults trapped rows — lets the live list expose the row id
        (for the existing dismiss endpoint) AND honour persisted dismissals."""
        result = await self._db.execute(
            select(HealthCheckResult).where(
                HealthCheckResult.company_id == company_id,
                HealthCheckResult.document_type == "CONTACT",
                HealthCheckResult.kind == "post_ledger",
                HealthCheckResult.status == "blocked",
                HealthCheckResult.result.contains({"rule_ids": [_CONTACT_DEFAULTS_RULE]}),
            )
        )
        out: dict[str, dict[str, Any]] = {}
        for row in result.scalars().all():
            out[str(row.document_id)] = {
                "trapped_row_id": str(row.id),
                "dismissed": bool((row.result or {}).get("dismissed")),
            }
        return out

    async def list_defaults(
        self,
        company_id: UUID,
        *,
        missing_only: bool = True,
        search: Optional[str] = None,
        include_dismissed: bool = False,
    ) -> dict[str, Any]:
        """List contacts with their current four defaults + which are missing,
        plus the account/tax-rate options for the dropdowns.

        ``missing_only=False`` is the "Show all Xero contacts" toggle.
        ``include_dismissed=False`` hides contacts the user dismissed (their
        contact_defaults trapped row is marked dismissed); pass True for the
        "show dismissed" view. Each row carries ``trapped_row_id`` + ``dismissed``."""
        conn, tenant = await self._connection(company_id)
        empty = {"connected": False, "contacts": [], "accounts": [],
                 "tax_rates": [], "total": 0, "missing_count": 0}
        if not self._integration.is_connected(conn, tenant):
            return empty
        try:
            contacts, accounts, tax_rates = await asyncio.gather(
                self._integration.fetch_contacts(conn, tenant),
                self._integration.fetch_chart_of_accounts(conn, tenant),
                self._integration.fetch_tax_rates(conn, tenant),
            )
        except NangoAuthError:
            # Dead/expired token: report not-connected instead of a false "0 clean".
            return empty
        trapped = await self._contact_trapped_map(company_id)

        needle = (search or "").strip().lower()
        rows: list[dict[str, Any]] = []
        missing_count = 0
        for c in (contacts or []):
            if c.get("IsArchived"):
                continue
            name = (c.get("Name") or "").strip()
            cid = (c.get("ContactID") or "").strip()
            if not name or not cid:
                continue
            if needle and needle not in name.lower():
                continue
            t = trapped.get(cid) or {}
            dismissed = bool(t.get("dismissed"))
            # A dismissed contact is hidden from the actionable list unless the
            # caller asks for it ("show dismissed").
            if dismissed and not include_dismissed:
                continue
            missing = missing_contact_defaults(c)
            if missing:
                missing_count += 1
            # missing_only keeps only contacts missing a default; otherwise
            # every active contact is returned so defaults can be set on any.
            if missing_only and not missing:
                continue
            rows.append({
                "contact_id": cid,
                "name": name,
                "is_customer": bool(c.get("IsCustomer")),
                "is_supplier": bool(c.get("IsSupplier")),
                "current_defaults": extract_contact_defaults(c),
                "missing": missing,
                "trapped_row_id": t.get("trapped_row_id"),  # for dismiss; null if no flag
                "dismissed": dismissed,
            })

        return {
            "connected": True,
            "contacts": rows,
            # dropdown options
            "accounts": [
                {"code": (a.get("Code") or "").strip(),
                 "name": (a.get("Name") or "").strip(),
                 "type": (a.get("Type") or "").strip()}
                for a in (accounts or []) if (a.get("Code") or "").strip()
            ],
            "tax_rates": [
                {"code": (t.get("TaxType") or "").strip(),
                 "name": (t.get("Name") or "").strip()}
                for t in (tax_rates or []) if (t.get("TaxType") or "").strip()
            ],
            "total": len(rows),
            "missing_count": missing_count,
        }

    async def coding_options(self, company_id: UUID) -> dict[str, Any]:
        """Just the account + tax-rate dropdown options (for the 'Change To'
        pickers on Unexpected Account / Unexpected Tax). No contact fetch — a
        light call the picker can hit on its own."""
        conn, tenant = await self._connection(company_id)
        if not self._integration.is_connected(conn, tenant):
            return {"connected": False, "accounts": [], "tax_rates": []}
        accounts, tax_rates = await asyncio.gather(
            self._integration.fetch_chart_of_accounts(conn, tenant),
            self._integration.fetch_tax_rates(conn, tenant),
        )
        return {
            "connected": True,
            "accounts": [
                {"code": (a.get("Code") or "").strip(),
                 "name": (a.get("Name") or "").strip(),
                 "type": (a.get("Type") or "").strip()}
                for a in (accounts or []) if (a.get("Code") or "").strip()
            ],
            "tax_rates": [
                {"code": (t.get("TaxType") or "").strip(),
                 "name": (t.get("Name") or "").strip()}
                for t in (tax_rates or []) if (t.get("TaxType") or "").strip()
            ],
        }

    async def _find_contact_defaults_row(
        self, company_id: UUID, doc_uuid: UUID,
    ) -> Optional[HealthCheckResult]:
        return (await self._db.execute(
            select(HealthCheckResult).where(
                HealthCheckResult.company_id == company_id,
                HealthCheckResult.document_id == doc_uuid,
                HealthCheckResult.document_type == "CONTACT",
                HealthCheckResult.kind == "post_ledger",
                HealthCheckResult.status == "blocked",
                HealthCheckResult.result.contains({"rule_ids": [_CONTACT_DEFAULTS_RULE]}),
            ).limit(1)
        )).scalar_one_or_none()

    async def dismiss(
        self, company_id: UUID, contact_id: str, reason: Optional[str] = None,
    ) -> dict[str, Any]:
        """Persistently dismiss a contact from the Contact-Defaults list. Marks
        its contact_defaults trapped row dismissed (creating a minimal one if the
        last audit never flagged it — e.g. a tax-only gap), so the live list
        honours it on reload."""
        try:
            doc_uuid = UUID(str(contact_id))
        except (TypeError, ValueError):
            return {"contact_id": contact_id, "dismissed": False, "error": "invalid contact id"}
        row = await self._find_contact_defaults_row(company_id, doc_uuid)
        if row is None:
            res: dict[str, Any] = {
                "flagged": [{"issue_type": _CONTACT_DEFAULTS_RULE,
                             "transaction_id": str(doc_uuid)}],
                "rule_ids": [_CONTACT_DEFAULTS_RULE],
                "messages": "",
                "target_ledger": "xero",
                "dismissed": True,
            }
            if reason:
                res["dismissal_reason"] = reason
            row = HealthCheckResult(
                company_id=company_id, document_id=doc_uuid,
                document_type="CONTACT", kind="post_ledger", status="blocked",
                result=res,
            )
            self._db.add(row)
            await self._db.flush()
        else:
            r = dict(row.result or {})
            r["dismissed"] = True
            if reason:
                r["dismissal_reason"] = reason
            row.result = r
        rid = row.id
        await self._db.commit()
        return {"contact_id": str(doc_uuid), "dismissed": True, "trapped_row_id": str(rid)}

    async def reinstate(self, company_id: UUID, contact_id: str) -> dict[str, Any]:
        """Un-dismiss a contact (the "Reinstate" / show-dismissed action)."""
        try:
            doc_uuid = UUID(str(contact_id))
        except (TypeError, ValueError):
            return {"contact_id": contact_id, "dismissed": True, "error": "invalid contact id"}
        row = await self._find_contact_defaults_row(company_id, doc_uuid)
        if row is not None:
            r = dict(row.result or {})
            r["dismissed"] = False
            row.result = r
            await self._db.commit()
        return {"contact_id": str(doc_uuid), "dismissed": False}

    async def confirm(
        self,
        company_id: UUID,
        contact_id: str,
        defaults: dict[str, Any],
        *,
        _conn: Optional[tuple[str, str]] = None,
    ) -> dict[str, Any]:
        """Write the chosen defaults to the Xero contact. ``_conn`` lets the
        bulk path reuse one connection lookup."""
        conn, tenant = _conn if _conn is not None else await self._connection(company_id)
        if not self._integration.is_connected(conn, tenant):
            return {"contact_id": contact_id, "ok": False, "error": "not connected"}
        fields = to_xero_default_fields(defaults)
        if not fields:
            return {"contact_id": contact_id, "ok": False,
                    "error": "no valid default fields supplied"}
        try:
            result = await self._integration.update_contact_defaults(
                conn, tenant, contact_id, fields,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ContactDefaults] confirm failed contact=%s", contact_id)
            return {"contact_id": contact_id, "ok": False, "error": str(exc)}
        if result is None:
            return {"contact_id": contact_id, "ok": False, "error": "Xero update failed"}
        return {"contact_id": contact_id, "ok": True, "applied": fields, "error": None}

    async def bulk_confirm(
        self,
        company_id: UUID,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Write defaults for many contacts. Each item: {contact_id, defaults}."""
        conn = await self._connection(company_id)
        results: list[dict[str, Any]] = []
        for it in items:
            cid = str(it.get("contact_id") or "").strip()
            if not cid:
                results.append({"contact_id": None, "ok": False, "error": "missing contact_id"})
                continue
            results.append(await self.confirm(
                company_id, cid, it.get("defaults") or {}, _conn=conn,
            ))
        succeeded = sum(1 for r in results if r.get("ok"))
        return {
            "requested": len(items),
            "succeeded": succeeded,
            "failed": len(items) - succeeded,
            "results": results,
        }
