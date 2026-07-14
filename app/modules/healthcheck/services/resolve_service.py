"""Resolve + dismiss orchestration for trapped rows.

This is the single seam where Xero writes happen: ``_call_xero`` pushes the
update through Nango when the org is connected, otherwise it logs the intent
and returns a stub response so the row still resolves end-to-end.

Allow-lists for ``field_updates``:

* Header fields go in one PUT body.
* Line-item fields trigger a GET-modify-PUT cycle when connected; otherwise
  the intent is recorded.

Anything outside both allow-lists is dropped into ``skipped_fields``
so the frontend can show "we ignored these" without a 400.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException, status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.healthcheck.models import Company
from app.modules.healthcheck.repository import HealthCheckResultRepository
from app.modules.healthcheck.schemas import (
    BulkActionItemResult,
    BulkActionResponse,
    DismissResponse,
    MarkOkResponse,
    RestoreResponse,
    ResolveResponse,
    SnoozeResponse,
)
from app.modules.healthcheck.xero_links import xero_deep_link
from app.modules.integrations.service import IntegrationService

logger = logging.getLogger("eazycapture.resolve")

ALLOWED_UPDATE_FIELDS: frozenset[str] = frozenset({
    "InvoiceNumber", "Reference", "Date", "DueDate",
    "Status", "LineAmountTypes",
})
ALLOWED_LINE_ITEM_FIELDS: frozenset[str] = frozenset({
    "TaxType", "AccountCode", "Description", "Quantity", "UnitAmount",
})


class ResolveService:
    """Marks rows resolved / dismissed and writes the change back to Xero."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = HealthCheckResultRepository(db)

    async def _shortcode_for(self, company_id: UUID) -> Optional[str]:
        """Fetch the org's Xero shortcode (cached per session via SA's
        identity map). Returned to ``xero_deep_link`` so error responses
        carry tenant-scoped URLs that resolve cleanly in any active Xero
        session."""
        company = await self._db.get(Company, company_id)
        if company is None:
            return None
        return (company.xero_shortcode or "").strip() or None

    # ------------------------------------------------------------------
    # resolve
    # ------------------------------------------------------------------

    async def resolve(
        self,
        *,
        row_id: UUID,
        company_id: UUID,
        field_updates: dict[str, str],
        resolution_notes: Optional[str] = None,
        resolved_by_user_id: Optional[UUID] = None,
        ai_applied: bool = False,
        ai_fix_strategy: Optional[str] = None,
    ) -> ResolveResponse:
        """Apply field updates (pushed to Xero when connected) + mark row resolved.

        Always returns a ``ResolveResponse``; sets ``error_code`` when
        the request is rejected so the route can map it to a 4xx.
        """
        row = await self._repo.find_by_id(row_id, company_id)
        if row is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Trapped row not found for this company.",
            )

        shortcode = await self._shortcode_for(company_id)
        xero_url = xero_deep_link(row.document_type, row.document_id, shortcode)

        if (row.result or {}).get("resolved"):
            return ResolveResponse(
                row_id=row.id,
                document_id=row.document_id,
                resolved=False,
                applied_updates={},
                xero_url=xero_url,
                ai_applied=ai_applied,
                ai_fix_strategy=ai_fix_strategy,
                error_code="ALREADY_RESOLVED",
                error_detail="This trapped row has already been resolved.",
            )

        clean_header, clean_lines, skipped = _split_updates(field_updates)
        if not clean_header and not clean_lines:
            return ResolveResponse(
                row_id=row.id,
                document_id=row.document_id,
                resolved=False,
                applied_updates={},
                skipped_fields=skipped,
                xero_url=xero_url,
                ai_applied=ai_applied,
                ai_fix_strategy=ai_fix_strategy,
                error_code="NO_SUPPORTED_FIELDS",
                error_detail=(
                    "None of the requested field_updates are in the "
                    "header or line-item allow-lists."
                ),
            )

        # Recode only the flagged line(s): the lines currently on a flagged
        # account (current_code) — not every line on the document.
        target_codes = frozenset(
            str(f.get("current_code")).strip()
            for f in (row.result or {}).get("flagged", [])
            if isinstance(f, dict) and f.get("current_code")
        )
        xero_response = await self._call_xero(
            company_id=company_id,
            document_id=row.document_id,
            document_type=row.document_type,
            header_updates=clean_header,
            line_item_updates=clean_lines,
            target_codes=target_codes,
        )
        # A genuine Xero write failure (or an unsupported bank-txn recode) must NOT
        # mark the row resolved — that would be a false success while Xero is
        # unchanged. A demo stub (no live connection) has xero_unreachable=False and
        # still resolves, so demos keep working.
        if xero_response.get("xero_unreachable"):
            return ResolveResponse(
                row_id=row.id,
                document_id=row.document_id,
                resolved=False,
                applied_updates={},
                skipped_fields=skipped,
                xero_url=xero_url,
                xero_response=xero_response,
                ai_applied=ai_applied,
                ai_fix_strategy=ai_fix_strategy,
                error_code="XERO_UPDATE_FAILED",
                error_detail=(
                    xero_response.get("reason")
                    or "Xero couldn't be updated — try again, or edit it in Xero."
                ),
            )

        notes = resolution_notes or _default_notes(
            ai_applied=ai_applied,
            ai_fix_strategy=ai_fix_strategy,
        )
        updated = await self._repo.mark_resolved(
            row.id,
            company_id,
            resolution_notes=notes,
            resolved_by_user_id=resolved_by_user_id,
            xero_response={
                **xero_response,
                "ai_applied": ai_applied,
                "ai_fix_strategy": ai_fix_strategy,
            },
        )
        if updated is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Row vanished mid-resolve.",
            )
        await self._db.commit()

        return ResolveResponse(
            row_id=row.id,
            document_id=row.document_id,
            resolved=True,
            applied_updates={**clean_header, **clean_lines},
            skipped_fields=skipped,
            xero_url=xero_url,
            xero_response=xero_response,
            ai_applied=ai_applied,
            ai_fix_strategy=ai_fix_strategy,
        )

    # ------------------------------------------------------------------
    # void (duplicate cleanup — Status → VOIDED, with precondition)
    # ------------------------------------------------------------------

    async def void(
        self,
        *,
        row_id: UUID,
        company_id: UUID,
        resolution_notes: Optional[str] = None,
    ) -> ResolveResponse:
        """Void an invoice/bill (Status → VOIDED). Xero rejects voiding an
        invoice that has a payment or credit note allocated, so we block that
        up-front with a clear message instead of failing at the
        API. Otherwise delegates to ``resolve`` (the real Xero write)."""
        row = await self._repo.find_by_id(row_id, company_id)
        if row is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Trapped row not found for this company.",
            )
        res = row.result or {}
        status_str = (res.get("invoice_status") or "").strip().upper()
        amount_paid = res.get("amount_paid")
        try:
            paid_val = Decimal(str(amount_paid)) if amount_paid not in (None, "") else Decimal("0")
        except (TypeError, ValueError, InvalidOperation):
            paid_val = Decimal("0")
        if status_str == "PAID" or paid_val > 0:
            shortcode = await self._shortcode_for(company_id)
            return ResolveResponse(
                row_id=row.id,
                document_id=row.document_id,
                resolved=False,
                applied_updates={},
                xero_url=xero_deep_link(row.document_type, row.document_id, shortcode),
                error_code="HAS_PAYMENT_OR_CREDIT",
                error_detail=(
                    "This invoice has a payment or credit note allocated and "
                    "can't be voided. Unallocate it in Xero first, then void."
                ),
            )
        return await self.resolve(
            row_id=row_id,
            company_id=company_id,
            field_updates={"Status": "VOIDED"},
            resolution_notes=resolution_notes or "Voided — duplicate removed.",
        )

    # ------------------------------------------------------------------
    # create credit note (write-off / discount an old unpaid invoice)
    # ------------------------------------------------------------------

    async def create_credit_note(
        self,
        *,
        row_id: UUID,
        company_id: UUID,
        reason: Optional[str] = None,
        created_by_user_id: Optional[UUID] = None,
    ) -> ResolveResponse:
        """The 'Credit Note' button on an old unpaid invoice. Creates a credit
        note in Xero that fully credits the invoice (write-off / discount) and
        marks the row resolved. Writes to real Xero when connected, else returns
        a stub so the demo flow works end-to-end without credentials."""
        row = await self._repo.find_by_id(row_id, company_id)
        if row is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Trapped row not found for this company.",
            )
        shortcode = await self._shortcode_for(company_id)
        xero_url = xero_deep_link(row.document_type, row.document_id, shortcode)

        if (row.result or {}).get("resolved"):
            return ResolveResponse(
                row_id=row.id,
                document_id=row.document_id,
                resolved=False,
                applied_updates={},
                xero_url=xero_url,
                error_code="ALREADY_RESOLVED",
                error_detail="This trapped row has already been resolved.",
            )

        xero_response = await self._call_xero_credit_note(
            company_id=company_id,
            document_id=row.document_id,
            document_type=row.document_type,
        )
        # Only clear the bill when the credit note was ALLOCATED (AmountDue → 0);
        # a created-but-unallocated note leaves the bill unpaid. Stub responses skip this gate.
        if not xero_response.get("stub"):
            created_cn = xero_response.get("xero_response") or {}
            if not created_cn.get("allocation"):
                return ResolveResponse(
                    row_id=row.id,
                    document_id=row.document_id,
                    resolved=False,
                    applied_updates={},
                    xero_url=xero_url,
                    xero_response=xero_response,
                    error_code="CREDIT_NOTE_NOT_ALLOCATED",
                    error_detail=(
                        "Credit note was created but could not be allocated to the "
                        "bill, so the bill is still unpaid. Allocate the credit note "
                        "in Xero (or retry) — it will clear once AmountDue reaches 0."
                    ),
                )
        notes = reason or "Credit note created to write off / discount the invoice."
        updated = await self._repo.mark_resolved(
            row.id,
            company_id,
            resolution_notes=notes,
            resolved_by_user_id=created_by_user_id,
            xero_response=xero_response,
        )
        if updated is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Row vanished mid-credit-note.",
            )
        await self._db.commit()
        return ResolveResponse(
            row_id=row.id,
            document_id=row.document_id,
            resolved=True,
            applied_updates={},
            xero_url=xero_url,
            xero_response=xero_response,
        )

    # ------------------------------------------------------------------
    # dismiss
    # ------------------------------------------------------------------

    async def dismiss(
        self,
        *,
        row_id: UUID,
        company_id: UUID,
        dismissal_reason: Optional[str] = None,
        dismissed_by_user_id: Optional[UUID] = None,
    ) -> DismissResponse:
        row = await self._repo.find_by_id(row_id, company_id)
        if row is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Trapped row not found for this company.",
            )
        updated = await self._repo.mark_dismissed(
            row.id,
            company_id,
            dismissal_reason=dismissal_reason,
            dismissed_by_user_id=dismissed_by_user_id,
        )
        if updated is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Row vanished mid-dismiss.",
            )
        await self._db.commit()
        return DismissResponse(row_id=row.id, dismissed=True)

    # ------------------------------------------------------------------
    # snooze ("ignore for N days")
    # ------------------------------------------------------------------

    async def snooze(
        self,
        *,
        row_id: UUID,
        company_id: UUID,
        days: int = 30,
        reason: Optional[str] = None,
        snoozed_by_user_id: Optional[UUID] = None,
    ) -> SnoozeResponse:
        row = await self._repo.find_by_id(row_id, company_id)
        if row is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Trapped row not found for this company.",
            )
        until = datetime.now(timezone.utc) + timedelta(days=days)
        until_iso = until.replace(microsecond=0).isoformat()
        updated = await self._repo.mark_snoozed(
            row.id, company_id,
            snoozed_until_ts=int(until.timestamp()),
            snoozed_until_iso=until_iso,
            snooze_reason=reason,
            snoozed_by_user_id=snoozed_by_user_id,
        )
        if updated is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Row vanished mid-snooze.",
            )
        await self._db.commit()
        return SnoozeResponse(row_id=row.id, snoozed=True, snoozed_until=until_iso)

    # ------------------------------------------------------------------
    # mark OK (accept a legit difference)
    # ------------------------------------------------------------------

    async def mark_ok(
        self,
        *,
        row_id: UUID,
        company_id: UUID,
        reason: Optional[str] = None,
        marked_ok_by_user_id: Optional[UUID] = None,
    ) -> MarkOkResponse:
        row = await self._repo.find_by_id(row_id, company_id)
        if row is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Trapped row not found for this company.",
            )
        updated = await self._repo.mark_ok(
            row.id, company_id,
            reason=reason,
            marked_ok_by_user_id=marked_ok_by_user_id,
        )
        if updated is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Row vanished mid-mark-ok.",
            )
        await self._db.commit()
        return MarkOkResponse(row_id=row.id, marked_ok=True)

    # ------------------------------------------------------------------
    # restore ("Mark as Not OK" / add back to the issue list)
    # ------------------------------------------------------------------

    async def restore(
        self,
        *,
        row_id: UUID,
        company_id: UUID,
    ) -> RestoreResponse:
        row = await self._repo.find_by_id(row_id, company_id)
        if row is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Trapped row not found for this company.",
            )
        updated = await self._repo.restore(row.id, company_id)
        if updated is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="Row vanished mid-restore.",
            )
        await self._db.commit()
        return RestoreResponse(row_id=row.id, restored=True)

    # ------------------------------------------------------------------
    # bulk (apply one local-state action to many rows)
    # ------------------------------------------------------------------

    async def bulk(
        self,
        *,
        row_ids: list[UUID],
        company_id: UUID,
        action: str,
        days: int = 30,
        reason: Optional[str] = None,
        acting_user_id: Optional[UUID] = None,
    ) -> BulkActionResponse:
        """Apply ``dismiss`` / ``snooze`` / ``mark_ok`` to many rows. Each row
        is applied independently; one failure (e.g. a cross-tenant / missing id)
        is recorded and does not abort the rest. Commits once at the end."""
        until = datetime.now(timezone.utc) + timedelta(days=days)
        until_iso = until.replace(microsecond=0).isoformat()
        until_ts = int(until.timestamp())

        results: list[BulkActionItemResult] = []
        succeeded = 0
        for rid in row_ids:
            try:
                if action == "dismiss":
                    updated = await self._repo.mark_dismissed(
                        rid, company_id, dismissal_reason=reason,
                        dismissed_by_user_id=acting_user_id,
                    )
                elif action == "snooze":
                    updated = await self._repo.mark_snoozed(
                        rid, company_id,
                        snoozed_until_ts=until_ts,
                        snoozed_until_iso=until_iso,
                        snooze_reason=reason,
                        snoozed_by_user_id=acting_user_id,
                    )
                elif action == "mark_ok":
                    updated = await self._repo.mark_ok(
                        rid, company_id, reason=reason,
                        marked_ok_by_user_id=acting_user_id,
                    )
                elif action == "restore":
                    updated = await self._repo.restore(rid, company_id)
                else:  # defensive — schema Literal should prevent this
                    results.append(BulkActionItemResult(
                        row_id=rid, ok=False, error=f"unknown action '{action}'"))
                    continue
            except Exception as exc:  # noqa: BLE001 — isolate per-row failures
                logger.exception("[SuHe][Bulk] %s failed for row %s", action, rid)
                results.append(BulkActionItemResult(row_id=rid, ok=False, error=str(exc)))
                continue

            if updated is None:
                results.append(BulkActionItemResult(
                    row_id=rid, ok=False, error="not found for this company"))
            else:
                succeeded += 1
                results.append(BulkActionItemResult(row_id=rid, ok=True))

        await self._db.commit()
        return BulkActionResponse(
            action=action,
            requested=len(row_ids),
            succeeded=succeeded,
            failed=len(row_ids) - succeeded,
            results=results,
        )

    # ------------------------------------------------------------------
    # Xero write — pushes via Nango when connected, else a stub response
    # ------------------------------------------------------------------

    async def _call_xero(
        self,
        *,
        company_id: UUID,
        document_id: UUID,
        document_type: str,
        header_updates: dict[str, str],
        line_item_updates: dict[str, str],
        target_codes: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """Push updates to Xero via Nango, or log a stub if disabled.

        Real path triggers only when *all three* are true:
            * IntegrationService.is_available() (secret key present)
            * The company has a ``nango_connection_id``
            * The company has a ``xero_tenant_id``

        Any missing piece keeps the row resolvable end-to-end via the
        stub, so demos work without live credentials.
        """
        integration = IntegrationService()
        company = await self._db.get(Company, company_id)
        connection_id = (company.nango_connection_id or "").strip() if company else ""
        tenant_id = (company.xero_tenant_id or "").strip() if company else ""

        if not integration.is_connected(connection_id, tenant_id):
            return self._stub_response(
                document_id=document_id,
                document_type=document_type,
                header_updates=header_updates,
                line_item_updates=line_item_updates,
                reason="Nango disabled or company missing connection.",
            )

        # Bank transactions (SPEND/RECEIVE) live at /BankTransactions, not
        # /Invoices — a line recode through the invoice endpoint silently no-ops,
        # so surface it as a failed write instead of a false "resolved".
        if line_item_updates and document_type.strip().upper() in {"SPEND", "RECEIVE"}:
            return self._stub_response(
                document_id=document_id,
                document_type=document_type,
                header_updates=header_updates,
                line_item_updates=line_item_updates,
                reason="Line recode not supported for a bank transaction — edit in Xero.",
                xero_unreachable=True,
            )

        body = await self._build_xero_body(
            integration=integration,
            connection_id=connection_id,
            tenant_id=tenant_id,
            document_id=document_id,
            header_updates=header_updates,
            line_item_updates=line_item_updates,
            target_codes=target_codes,
        )
        if body is None:
            logger.warning(
                "[SuHe][Resolve] could not build Xero body for "
                "document=%s (read-modify-write fetch failed). Falling "
                "back to header-only update.",
                document_id,
            )
            body = {
                "Invoices": [{
                    "InvoiceID": str(document_id),
                    **header_updates,
                }],
            }

        result = await integration.update_invoice(
            connection_id=connection_id,
            tenant_id=tenant_id,
            invoice_id=str(document_id),
            body=body,
            field_updates=header_updates or None,
            line_item_updates=line_item_updates or None,
        )
        if result is None:
            logger.warning(
                "[SuHe][Resolve] Nango update_xero_invoice returned None "
                "for document=%s — surfacing stub response so the row "
                "still resolves locally.",
                document_id,
            )
            return self._stub_response(
                document_id=document_id,
                document_type=document_type,
                header_updates=header_updates,
                line_item_updates=line_item_updates,
                reason="Nango call returned None (proxy error / 4xx).",
                xero_unreachable=True,
            )

        logger.info(
            "[SuHe][Resolve] Nango PUT /Invoices/%s OK "
            "header=%s line=%s",
            document_id, header_updates, line_item_updates,
        )
        return {
            "stub": False,
            "applied": {
                **header_updates,
                **(
                    {"LineItems": [dict(line_item_updates)]}
                    if line_item_updates else {}
                ),
            },
            "document_id": str(document_id),
            "document_type": document_type,
            "xero_response": result,
        }

    async def _build_xero_body(
        self,
        *,
        integration: IntegrationService,
        connection_id: str,
        tenant_id: str,
        document_id: UUID,
        header_updates: dict[str, str],
        line_item_updates: dict[str, str],
        target_codes: frozenset[str] = frozenset(),
    ) -> Optional[dict[str, Any]]:
        """For line-item updates we need a read-modify-write: fetch the
        invoice, preserve each line's required fields, overlay our changes ONLY
        on the flagged line(s) — those currently on a ``target_codes`` account —
        so a multi-line bill's other lines are left untouched. Empty target_codes
        falls back to every line (safe for single-line docs). Header-only updates
        skip the fetch."""
        if not line_item_updates:
            return {
                "Invoices": [{
                    "InvoiceID": str(document_id),
                    **header_updates,
                }],
            }

        existing = await integration.fetch_invoice(
            connection_id, tenant_id, str(document_id),
        )
        if not isinstance(existing, dict):
            return None

        invoices = existing.get("Invoices") or []
        if not (isinstance(invoices, list) and invoices):
            return None
        current_lines = invoices[0].get("LineItems") or []

        merged_lines: list[dict[str, Any]] = []
        for line in current_lines:
            if not isinstance(line, dict):
                continue
            line_item_id = line.get("LineItemID")
            if not line_item_id:
                continue
            new_line: dict[str, Any] = {"LineItemID": line_item_id}
            for preserved in (
                "Description", "Quantity", "UnitAmount",
                "AccountCode", "TaxType",
            ):
                if line.get(preserved) is not None:
                    new_line[preserved] = line[preserved]
            line_code = str(line.get("AccountCode") or "").strip()
            if not target_codes or line_code in target_codes:
                new_line.update(line_item_updates)
            merged_lines.append(new_line)

        if not merged_lines:
            return None

        return {
            "Invoices": [{
                "InvoiceID": str(document_id),
                "LineItems": merged_lines,
                **header_updates,
            }],
        }

    def _stub_response(
        self,
        *,
        document_id: UUID,
        document_type: str,
        header_updates: dict[str, str],
        line_item_updates: dict[str, str],
        reason: str,
        xero_unreachable: bool = False,
    ) -> dict[str, Any]:
        logger.info(
            "[SuHe][Resolve] STUB Xero call (%s) — "
            "document_type=%s document_id=%s "
            "header_updates=%s line_item_updates=%s",
            reason, document_type, document_id,
            header_updates, line_item_updates,
        )
        would_apply: dict[str, Any] = dict(header_updates)
        if line_item_updates:
            would_apply["LineItems"] = [dict(line_item_updates)]
        return {
            "stub": True,
            "would_apply": would_apply,
            "document_id": str(document_id),
            "document_type": document_type,
            "reason": reason,
            "xero_unreachable": xero_unreachable,
        }

    # ------------------------------------------------------------------
    # Xero credit-note write (create + allocate, with stub fallback)
    # ------------------------------------------------------------------

    async def _call_xero_credit_note(
        self,
        *,
        company_id: UUID,
        document_id: UUID,
        document_type: str,
    ) -> dict[str, Any]:
        """Create + allocate a credit note via Nango, or a stub when the org
        isn't connected (so demos work without live credentials)."""
        integration = IntegrationService()
        company = await self._db.get(Company, company_id)
        connection_id = (company.nango_connection_id or "").strip() if company else ""
        tenant_id = (company.xero_tenant_id or "").strip() if company else ""

        if not integration.is_connected(connection_id, tenant_id):
            return self._stub_credit_note_response(
                document_id=document_id,
                document_type=document_type,
                reason="Nango disabled or company missing connection.",
            )
        result = await integration.create_credit_note(
            connection_id=connection_id,
            tenant_id=tenant_id,
            invoice_id=str(document_id),
        )
        if result is None:
            return self._stub_credit_note_response(
                document_id=document_id,
                document_type=document_type,
                reason="Xero create-credit-note call failed; stubbed.",
                xero_unreachable=True,
            )
        return {
            "stub": False,
            "action": "credit_note_created",
            "document_id": str(document_id),
            "document_type": document_type,
            "xero_response": result,
        }

    def _stub_credit_note_response(
        self,
        *,
        document_id: UUID,
        document_type: str,
        reason: str,
        xero_unreachable: bool = False,
    ) -> dict[str, Any]:
        logger.info(
            "[SuHe][Resolve] STUB credit note (%s) — document_type=%s document_id=%s",
            reason, document_type, document_id,
        )
        return {
            "stub": True,
            "action": "credit_note_created",
            "document_id": str(document_id),
            "document_type": document_type,
            "reason": reason,
            "xero_unreachable": xero_unreachable,
        }


# ----------------------- module-level helpers -----------------------

def _split_updates(
    field_updates: dict[str, str],
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    header: dict[str, str] = {}
    lines: dict[str, str] = {}
    skipped: list[str] = []
    for key, value in (field_updates or {}).items():
        if key in ALLOWED_UPDATE_FIELDS:
            header[key] = value
        elif key in ALLOWED_LINE_ITEM_FIELDS:
            lines[key] = value
        else:
            skipped.append(key)
    return header, lines, skipped


def _default_notes(*, ai_applied: bool, ai_fix_strategy: Optional[str]) -> str:
    if ai_applied:
        return f"Auto-applied AI fix: {ai_fix_strategy or 'manual_review'}"
    return "Manually resolved via API"
