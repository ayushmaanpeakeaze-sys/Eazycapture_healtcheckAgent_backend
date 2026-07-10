"""Trapped-invoices feed service.

Orchestrates one paginated DB query (deterministic flags) + one Redis
``MGET`` (AI enrichments) into the flat ``TrappedInvoicesResponse``
shape the React UI polls.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.healthcheck.models import Company, HealthCheckResult
from app.modules.healthcheck.repository import HealthCheckResultRepository
from app.modules.healthcheck.schemas import (
    TrappedInvoiceAI,
    TrappedInvoiceItem,
    TrappedInvoicesResponse,
)
from app.modules.healthcheck.xero_links import xero_deep_link

logger = logging.getLogger("eazycapture.trapped")

_AI_KEY_PREFIX = "health_check_ai"

_ISSUE_TITLES: dict[str, str] = {
    "duplicate_invoice":             "{vendor} has duplicate invoices",
    "duplicate_bill":                "{vendor} has duplicate bills",
    "duplicate_credit_note":         "{vendor} has duplicate credit notes",
    "old_unpaid_invoice":            "{vendor} has old unpaid invoices",
    "old_unpaid_bill":               "{vendor} has old unpaid bills",
    "old_unsettled_sales_credit":    "{vendor} has unsettled sales credits",
    "old_unsettled_purchase_credit": "{vendor} has unsettled purchase credits",
    "opening_balance_difference":    "{vendor} has opening balance differences",
    "invoice_or_direct_booking":     "{vendor} may have direct bookings instead of invoices",
    "bill_or_direct_booking":        "{vendor} may have direct bookings instead of bills",
    "low_cost_fixed_asset":          "{vendor} has low-cost fixed asset expenses",
    "capital_item_review":           "{vendor} has potential capital items in expenses",
    "wrong_category":                "{vendor} has miscategorised transactions",
    "multi_account_supplier":        "{vendor} uses multiple accounts inconsistently",
    "multi_tax_code_supplier":       "{vendor} uses multiple tax codes",
    "unexpected_account":            "{vendor} has an unexpected account used",
    "unexpected_tax_code":           "{vendor} has an unexpected tax code",
    "amount_outlier":                "{vendor} has an unusually large amount",
    "anomaly":                       "{vendor} has an anomalous transaction",
    "purchase_tax_missing":          "{vendor} is missing purchase tax",
    "unapproved_invoice":            "{vendor} has unapproved invoices",
    "unapproved_bill":               "{vendor} has unapproved bills",
    "wrong_direction_account":       "{vendor} has a transaction on the wrong side of the ledger",
    "missing_tax":                   "{vendor} has missing tax codes",
    "missing_invoice_number":        "{vendor} has invoices with missing reference numbers",
    "duplicate_vendor":              "{vendor} may be a duplicate contact",
    "future_dated":                  "{vendor} has future-dated transactions",
    "currency_mismatch":             "{vendor} has a currency mismatch",
    "invalid_tax_code":              "{vendor} has an invalid tax code",
    "invalid_status_combo":          "{vendor} has an invalid status combination",
    # New tax direction checks
    "sales_tax_on_bills":            "{vendor} has sales tax applied to a purchase bill",
    "purchase_tax_on_invoices":      "{vendor} has purchase tax applied to a sales invoice",
    "sales_tax_missing":             "{vendor} has invoices with missing sales tax",
    # New contact checks
    "duplicate_contact":             "{vendor} may be a duplicate contact in Xero",
    "contact_defaults":              "{vendor} is missing default account or tax settings",
    "inactive_contact":              "{vendor} has had no transactions in the last 180 days",
}


def _build_title(vendor: str, result: dict) -> Optional[str]:
    """Generate a human-readable title like 'Hamilton Smith has duplicate invoices'."""
    flagged = result.get("flagged") or []
    if not flagged:
        return None
    issue_type = str(flagged[0].get("issue_type") or "")
    if not issue_type:
        return None
    vendor = vendor.strip() or "Unknown vendor"
    template = _ISSUE_TITLES.get(
        issue_type,
        "{vendor} has a " + issue_type.replace("_", " ") + " issue",
    )
    return template.format(vendor=vendor)


class TrappedInvoiceService:
    """Reads ``health_check_result`` rows for one company + splices in
    any ``health_check_ai:{tx}`` records that exist in Redis."""

    def __init__(self, db: AsyncSession, redis_client: Redis) -> None:
        self._db = db
        self._redis = redis_client
        self._repo = HealthCheckResultRepository(db)

    async def list_trapped(
        self,
        *,
        company_id: UUID,
        limit: int,
        offset: int,
        search_document_id: Optional[str] = None,
        include_dismissed: bool = False,
        include_marked_ok: bool = False,
        issue_type: Optional[str] = None,
        exclude_bank_items: bool = False,
    ) -> TrappedInvoicesResponse:
        rows, total, total_value = await self._repo.list_post_ledger_trapped_paginated(
            company_id,
            limit=limit,
            offset=offset,
            search_document_id=search_document_id,
            include_dismissed=include_dismissed,
            include_marked_ok=include_marked_ok,
            issue_type=issue_type,
            exclude_bank_items=exclude_bank_items,
        )

        # Per-page lookups: one Company fetch (for the shortcode) + one
        # Redis MGET (for AI annotations). Both keep the page render to
        # a constant number of round-trips.
        shortcode: Optional[str] = None
        company = await self._db.get(Company, company_id)
        if company is not None:
            shortcode = (company.xero_shortcode or "").strip() or None

        ai_lookup: dict[str, Optional[TrappedInvoiceAI]] = {}
        if rows:
            keys = [f"{_AI_KEY_PREFIX}:{row.document_id}" for row in rows]
            try:
                values = await self._redis.mget(keys)
            except Exception:
                logger.exception(
                    "[SuHe][Trapped] redis MGET failed for %d keys — "
                    "feed will render without AI annotations",
                    len(keys),
                )
                values = [None] * len(keys)
            for row, raw in zip(rows, values):
                ai_lookup[str(row.document_id)] = _coerce_ai(raw)

        results = [
            _build_item(
                row,
                ai_lookup.get(str(row.document_id)),
                shortcode=shortcode,
            )
            for row in rows
        ]
        return TrappedInvoicesResponse(
            results=results,
            total=total,
            total_value=total_value,
            limit=limit,
            offset=offset,
        )


# ----------------------- helpers ------------------------------------

def _coerce_ai(raw: Any) -> Optional[TrappedInvoiceAI]:
    """Tolerate str/bytes/dict/None — Redis client behaviour varies
    across versions and decode_responses settings."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw, dict):
        parsed = raw
    else:
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return TrappedInvoiceAI(**parsed)
    except Exception:
        logger.exception("[SuHe][Trapped] failed to parse AI record")
        return None


def _build_item(
    row: HealthCheckResult,
    ai: Optional[TrappedInvoiceAI],
    *,
    shortcode: Optional[str] = None,
) -> TrappedInvoiceItem:
    """ORM row + Redis AI → flat response item.

    ``user_id`` / ``target_ledger`` live inside the ``result`` JSONB
    (the audit task stores them there); we project them up so the
    frontend sees a single flat object.
    """
    result = row.result or {}
    raw_user = result.get("recorded_by_user_id")
    user_id: Optional[UUID] = None
    if isinstance(raw_user, str) and raw_user.strip():
        try:
            user_id = UUID(raw_user)
        except ValueError:
            user_id = None

    vendor = str(result.get("vendor_name") or "")
    title = _build_title(vendor, result)
    invoice_status = str(result.get("invoice_status") or "").strip().upper() or None

    return TrappedInvoiceItem(
        id=row.id,
        document_id=row.document_id,
        document_type=row.document_type,
        company_id=row.company_id,
        user_id=user_id,
        kind=row.kind,
        target_ledger=str(result.get("target_ledger") or "xero"),
        status=row.status,
        title=title,
        error_msgs=row.error_msgs,
        result=result,
        ran_at=row.ran_at,
        xero_url=xero_deep_link(row.document_type, row.document_id, shortcode, invoice_status),
        ai=ai,
    )
