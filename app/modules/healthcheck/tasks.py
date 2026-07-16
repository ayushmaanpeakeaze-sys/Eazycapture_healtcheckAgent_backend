"""Celery tasks for the healthcheck service.

``historical_audit_task`` does the same job regardless of where the
invoices come from:

1. Decide the data source — Nango (real Xero) when the company has a
   connection and the service is configured, otherwise seeded local
   data (current default).
2. Reshape every invoice into the 14-field shape the rules engine
   validates against.
3. Post the batch to ``/api/v1/health-check/batch`` over HTTP.
4. Persist trapped rows + fire-and-forget the AI enrichment call.

Everything in the task body is SYNC by design — Celery's prefork
worker doesn't share asyncio loops cleanly. Async Nango calls are
bridged with a small per-task ``asyncio.run`` helper.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID

import httpx
import redis
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.db import SyncSessionLocal
from app.modules.healthcheck._fixtures import (
    HARDCODED_CHART_OF_ACCOUNTS,
    HARDCODED_TAX_RATES,
)
from app.modules.auth.models import User, UserCompanyAccess
from app.modules.healthcheck.models import (
    AuditBatch,
    Company,
    HealthCheckResult,
    Invoice,
)
from app.modules.integrations.sync import db_read
from app.modules.healthcheck.services.audit_service import (
    META_FIELD,
    batch_key,
)
from app.modules.integrations.service import IntegrationService

logger = logging.getLogger("eazycapture.audit.task")

KIND_POST_LEDGER = "post_ledger"
STATUS_BLOCKED = "blocked"
DEFAULT_TARGET_LEDGER = "xero"
_AI_BATCH_CHUNK = 200  # split very large batches; Demo Co stays under this


# =====================================================================
# Redis meta helpers (sync)
# =====================================================================

def _redis_client() -> redis.Redis:
    return redis.from_url(settings.REDIS_URL, decode_responses=True)


def _read_meta(r: redis.Redis, batch_id: str) -> dict[str, Any]:
    raw = r.hget(batch_key(batch_id), META_FIELD)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _write_meta(r: redis.Redis, batch_id: str, meta: dict[str, Any]) -> None:
    key = batch_key(batch_id)
    r.hset(key, META_FIELD, json.dumps(meta, default=str))
    r.expire(key, settings.HEALTHCHECK_BATCH_HASH_TTL_SECONDS)


def _patch_meta(
    r: redis.Redis,
    batch_id: str,
    **changes: Any,
) -> dict[str, Any]:
    """Shallow-merge ``changes`` into the meta hash, log the stage move."""
    meta = _read_meta(r, batch_id)
    meta.update(changes)
    _write_meta(r, batch_id, meta)
    stage = changes.get("stage") or meta.get("stage")
    label = changes.get("stage_label") or meta.get("stage_label")
    logger.info(
        "[SuHe][Audit] batch=%s stage=%s :: %s", batch_id, stage, label,
    )
    return meta


# =====================================================================
# Invoice → AI-batch transaction reshape
# =====================================================================

# Xero status filter — DELETED and VOIDED documents are immutable in
# Xero, so feeding them to the rules engine just clutters the audit.
_XERO_SKIP_STATUSES: frozenset[str] = frozenset({"DELETED", "VOIDED"})
_XERO_DATE_RE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def _xero_date(value: Any) -> Optional[str]:
    """Normalise a Xero date (``/Date(ms+0000)/`` or ISO) → ``YYYY-MM-DD``."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        match = _XERO_DATE_RE.match(value.strip())
        if match:
            try:
                ts_ms = int(match.group(1))
                return (
                    datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                    .date()
                    .isoformat()
                )
            except (ValueError, OSError):
                return None
        try:
            return datetime.fromisoformat(value).date().isoformat()
        except ValueError:
            return value[:10] if len(value) >= 10 else None
    return None


def _str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    return text or None


def _reshape_xero_to_batch(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map a raw Xero Invoice or CreditNote JSON dict → the 14-field batch shape.

    Credit notes use CreditNoteID instead of InvoiceID — we try both so
    credit notes are included in the audit rather than silently dropped.

    Returns ``None`` when the document is DELETED / VOIDED or when key
    identifiers are missing — the audit will simply skip it.
    """
    if not isinstance(raw, dict):
        return None
    # Invoices use InvoiceID; credit notes use CreditNoteID.
    invoice_id = (
        raw.get("InvoiceID") or raw.get("CreditNoteID") or ""
    ).strip()
    if not invoice_id:
        return None
    status = (raw.get("Status") or "").strip().upper()
    if status in _XERO_SKIP_STATUSES:
        return None

    # Credit notes from /CreditNotes endpoint may omit Type — infer it.
    doc_type = (raw.get("Type") or "").strip().upper()
    if not doc_type and raw.get("CreditNoteID"):
        doc_type = "ACCRECCREDIT"   # default; Xero credit notes are sales credits
    doc_type = doc_type or None
    contact = raw.get("Contact") or {}
    vendor_name = (contact.get("Name") or "").strip() or "Unknown Vendor"
    line_items = raw.get("LineItems") or []
    first_line = line_items[0] if isinstance(line_items, list) and line_items else {}

    description = (
        raw.get("Reference")
        or first_line.get("Description")
        or f"{vendor_name} — {doc_type or 'Invoice'}"
    )
    description = str(description).strip() or "Invoice"

    # A bill's identifier is its Reference, stored in either Reference or
    # InvoiceNumber; for purchase docs fall back to InvoiceNumber when blank.
    reference_value = _str_or_none(raw.get("Reference"))
    if doc_type in ("ACCPAY", "ACCPAYCREDIT") and not reference_value:
        # Credit notes carry CreditNoteNumber, not InvoiceNumber — fall back to it
        # so a purchase credit's identifier isn't blank (duplicate matching keys on it).
        reference_value = _str_or_none(
            raw.get("InvoiceNumber") or raw.get("CreditNoteNumber")
        )

    return {
        "transaction_id": invoice_id,
        "date": _xero_date(raw.get("Date")) or datetime.now(timezone.utc).date().isoformat(),
        "description": description[:1000],
        "amount": _str_or_none(raw.get("Total")) or "0",
        "vendor_name": vendor_name,
        # Contact.ContactID — the foreign key per-contact checks group on.
        "contact_id": (contact.get("ContactID") or "").strip() or None,
        # Bill identifier (Xero Reference, or InvoiceNumber as fallback);
        # duplicate-matching keys on this.
        "reference": reference_value,
        "tax_code": _str_or_none(first_line.get("TaxType")),
        "current_account_code": _str_or_none(first_line.get("AccountCode")),
        # Credit notes use CreditNoteNumber (no InvoiceNumber) — map it here so the
        # duplicate check's primary "same identifying number" signal works for them.
        "invoice_number": _str_or_none(
            raw.get("InvoiceNumber") or raw.get("CreditNoteNumber")
        ),
        "due_date": _xero_date(raw.get("DueDate")),
        "status": status or None,
        "amount_paid": _str_or_none(raw.get("AmountPaid")),
        # Invoices/bills carry AmountDue; credit notes carry RemainingCredit
        # (the unallocated balance) instead.
        "amount_due": _str_or_none(raw.get("AmountDue")) or _str_or_none(raw.get("RemainingCredit")),
        # Bank-matched (reconciled) flag, injected by _pull_xero_documents from
        # the Payments feed. None when payments weren't fetched.
        "reconciled": raw.get("_IsReconciled") if "_IsReconciled" in raw else None,
        # Attachment presence (Undocumented-Bills check) + total tax (tax-only filter).
        "has_attachments": raw.get("HasAttachments"),
        "tax_total": _str_or_none(raw.get("TotalTax")),
        "currency_code": _str_or_none(raw.get("CurrencyCode")) or "GBP",
        "type": doc_type,
        "posted_date": _xero_date(raw.get("UpdatedDateUTC")),
        # Every line, so per-line tax/account checks cover the whole document.
        "line_items": [
            {
                "account_code": _str_or_none(li.get("AccountCode")),
                "tax_code": _str_or_none(li.get("TaxType")),
                "amount": _str_or_none(li.get("LineAmount")),
                "tax_amount": _str_or_none(li.get("TaxAmount")),
                "description": _str_or_none(li.get("Description")),
            }
            for li in line_items
            if isinstance(li, dict)
        ],
    }


def _reshape_bank_txn_to_batch(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map a raw Xero BankTransaction (Money In / Money Out) → the batch shape,
    tagged with type RECEIVE / SPEND.

    Only spend/receive money WITH a contact is kept — those are what the
    Unexpected-Account check compares against the contact's default account.
    Transfers / pre- & over-payments and contact-less lines are skipped (nothing
    to compare → we stay silent on them).
    """
    if not isinstance(raw, dict):
        return None
    bt_id = (raw.get("BankTransactionID") or "").strip()
    if not bt_id:
        return None
    bt_type = (raw.get("Type") or "").strip().upper()
    if bt_type not in {"RECEIVE", "SPEND"}:   # transfers / over- & pre-payments → skip
        return None
    status = (raw.get("Status") or "").strip().upper()
    if status in _XERO_SKIP_STATUSES:
        return None
    contact = raw.get("Contact") or {}
    contact_id = (contact.get("ContactID") or "").strip()
    if not contact_id:
        return None   # no contact → no default to compare → skip
    vendor_name = (contact.get("Name") or "").strip() or "Unknown"
    line_items = raw.get("LineItems") or []
    first_line = line_items[0] if isinstance(line_items, list) and line_items else {}
    description = (
        _str_or_none(raw.get("Reference"))
        or first_line.get("Description")
        or f"{vendor_name} — {bt_type}"
    )
    return {
        "transaction_id": bt_id,
        "date": _xero_date(raw.get("Date")) or datetime.now(timezone.utc).date().isoformat(),
        "description": str(description).strip()[:1000] or bt_type,
        "amount": _str_or_none(raw.get("Total")) or "0",
        "vendor_name": vendor_name,
        "contact_id": contact_id,
        "reference": _str_or_none(raw.get("Reference")),
        "tax_code": _str_or_none(first_line.get("TaxType")),
        "current_account_code": _str_or_none(first_line.get("AccountCode")),
        "invoice_number": None,
        "due_date": None,
        "status": status or None,
        "amount_paid": None,
        "amount_due": None,
        "reconciled": raw.get("IsReconciled") if "IsReconciled" in raw else None,
        "has_attachments": raw.get("HasAttachments"),
        "tax_total": _str_or_none(raw.get("TotalTax")),
        "currency_code": _str_or_none(raw.get("CurrencyCode")) or "GBP",
        "type": bt_type,   # RECEIVE (money in) / SPEND (money out)
        "posted_date": _xero_date(raw.get("UpdatedDateUTC")),
        "line_items": [
            {
                "account_code": _str_or_none(li.get("AccountCode")),
                "tax_code": _str_or_none(li.get("TaxType")),
                "amount": _str_or_none(li.get("LineAmount")),
                "tax_amount": _str_or_none(li.get("TaxAmount")),
                "description": _str_or_none(li.get("Description")),
            }
            for li in line_items
            if isinstance(li, dict)
        ],
    }


def _reshape_journal_to_batch(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Raw Xero Journal → batch shape, ONLY manual journals (SourceType
    MANJOURNAL) so invoices/bills/bank txns aren't double-counted."""
    if not isinstance(raw, dict):
        return None
    if (raw.get("SourceType") or "").strip().upper() != "MANJOURNAL":
        return None
    jid = (raw.get("JournalID") or "").strip()
    lines = raw.get("JournalLines") or []
    if not jid or not isinstance(lines, list) or not lines:
        return None
    batch_lines = [
        {
            "account_code": _str_or_none(li.get("AccountCode")),
            "tax_code": None,
            "amount": _str_or_none(li.get("NetAmount")),
            "tax_amount": _str_or_none(li.get("TaxAmount")),
            "description": _str_or_none(li.get("Description")),
        }
        for li in lines
        if isinstance(li, dict)
    ]
    # first non-empty line narration (keyword lives here); biggest line = amount
    desc = next(
        (str(li.get("Description")).strip() for li in lines
         if isinstance(li, dict) and (li.get("Description") or "").strip()),
        "Manual journal entry",
    )
    biggest = max(
        (abs(float(li.get("NetAmount") or 0)) for li in lines if isinstance(li, dict)),
        default=0.0,
    )
    return {
        "transaction_id": jid,
        "date": _xero_date(raw.get("JournalDate")) or datetime.now(timezone.utc).date().isoformat(),
        "description": desc[:1000] or "Manual journal entry",
        "amount": f"{biggest:.2f}",
        "vendor_name": "Manual journal",
        "contact_id": None,
        "reference": None,
        "current_account_code": batch_lines[0]["account_code"] if batch_lines else None,
        "currency_code": "GBP",
        "type": "MANJOURNAL",
        "line_items": batch_lines,
    }


def _payment_and_edit_state(transaction: dict[str, Any]) -> dict[str, Any]:
    """Compute paid/unpaid status + whether we can edit the line via the API.

    Returns:
        payment_status  — "paid" | "part_paid" | "unpaid" | "settled" (bank txn)
                          | "unknown"
        editable        — True when the account/tax can be updated via the API
                          (NOT reconciled, NO payment/credit note allocated)
        editable_reason — why NOT editable ("reconciled" / "payment_allocated" /
                          "payment_or_credit_allocated"); None when editable.

    Frontend: editable → "Change To" dropdown + Save Changes; not editable →
    "Edit in Xero" (and ``editable_reason`` explains why).
    """
    def _f(v: Any) -> Optional[float]:
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    doc_type = (transaction.get("type") or "").strip().upper()
    is_bank = doc_type in {"RECEIVE", "SPEND"}
    reconciled = transaction.get("reconciled") is True
    paid = _f(transaction.get("amount_paid"))
    due = _f(transaction.get("amount_due"))
    amt = _f(transaction.get("amount"))

    # --- paid / unpaid -------------------------------------------------
    if is_bank:
        payment_status = "settled"          # money already moved
    elif due is not None and due <= 0.005:
        payment_status = "paid"
    elif (paid is not None and paid > 0.005) or (
        amt is not None and due is not None and due < amt - 0.005
    ):
        payment_status = "part_paid"
    elif due is not None:
        payment_status = "unpaid"
    else:
        payment_status = "unknown"

    # --- editable + reason ---------------------------------------------
    if reconciled:
        editable, reason = False, "reconciled"
    elif paid is not None and paid > 0.005:
        editable, reason = False, "payment_allocated"
    elif amt is not None and due is not None and abs(due - amt) > 0.005:
        editable, reason = False, "payment_or_credit_allocated"
    else:
        editable, reason = True, None

    return {"payment_status": payment_status, "editable": editable,
            "editable_reason": reason}


def _reshape_invoice(invoice: Invoice) -> dict[str, Any]:
    """Map an :class:`Invoice` (+ first line) to the 14-field shape
    that ``/api/v1/health-check/batch`` validates against."""
    # description has min_length=1 on the AI service side — fall back to
    # vendor + type so we never send an empty string.
    description = (invoice.reference or f"{invoice.vendor_name} — {invoice.type}").strip()
    current_account_code = (
        invoice.line_items[0].account_code if invoice.line_items else None
    )
    return {
        "transaction_id": str(invoice.id),
        "date": invoice.issue_date.isoformat(),
        "description": description,
        "amount": str(invoice.amount),
        "vendor_name": invoice.vendor_name,
        "reference": invoice.reference,        # Xero Reference — what duplicate-matching keys on
        "tax_code": invoice.tax_code,
        "current_account_code": current_account_code or invoice.account_code,
        "invoice_number": invoice.invoice_number,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "status": invoice.status,
        "amount_paid": str(invoice.amount_paid) if invoice.amount_paid is not None else None,
        "amount_due": str(invoice.amount_due) if invoice.amount_due is not None else None,
        "currency_code": invoice.currency_code,
        "type": invoice.type,
        "posted_date": None,
    }


def _fetch_audit_transactions(
    db,
    company: Company,
) -> tuple[list[dict[str, Any]], str]:
    """Load + reshape this company's invoices, choosing Nango (real Xero)
    when configured and seeded local data otherwise.

    Returns ``(transactions, source)`` where ``source`` is ``"nango"`` or
    ``"seed"`` and is used purely for logs / observability.
    """
    integration = IntegrationService()
    use_nango = integration.is_connected(
        company.nango_connection_id, company.xero_tenant_id,
    )

    # DB-backed source (AUDIT_SOURCE=db): read mirrored Xero data from the synced
    # tables, gated on an initial sync; else fall through to the live path.
    if (
        settings.AUDIT_SOURCE == "db"
        and use_nango
        and db_read.has_synced_documents(db, company.id)
    ):
        docs, raw_bank_txns = db_read.read_documents(db, company.id)
        raw_journals = db_read.read_raw(db, company.id, "journal")
        shaped = [_reshape_xero_to_batch(raw) for raw in docs]
        shaped += [_reshape_bank_txn_to_batch(raw) for raw in raw_bank_txns]
        # Manual journals (MANJOURNAL only) — the SOP's capital-in-the-ledger gap.
        shaped += [_reshape_journal_to_batch(raw) for raw in raw_journals]
        transactions = [tx for tx in shaped if tx is not None]
        n_journals = sum(1 for tx in shaped if tx is not None and tx.get("type") == "MANJOURNAL")
        logger.info(
            "[SuHe][Audit] Loaded %d doc(s) + %d bank txn(s) + %d manual journal(s) "
            "from DB sync for company=%s",
            len(docs), len(raw_bank_txns), n_journals, company.id,
        )
        return transactions, "db"

    if use_nango:
        try:
            raw_invoices, raw_bank_txns = asyncio.run(
                _pull_xero_documents(
                    integration,
                    company.nango_connection_id,
                    company.xero_tenant_id,
                )
            )
        except Exception as exc:
            # Self-heal a stale connection-id: find the live Xero connection and,
            # if different, repoint the company and retry once.
            healed = None
            # Scope the heal to this company's firm; unscoped only for legacy
            # firm-less companies.
            heal_eu = None
            if getattr(company, "firm_id", None) is not None:
                heal_eu = db.execute(
                    select(User.id).where(
                        User.firm_id == company.firm_id,
                        User.nango_connection_id.isnot(None),
                    ).limit(1)
                ).scalar_one_or_none()
            try:
                live = asyncio.run(integration.find_live_xero_connection(
                    end_user_id=str(heal_eu) if heal_eu else None))
            except Exception:        # noqa: BLE001 — detection is best-effort
                live = None
            if live and (live[0] != company.nango_connection_id
                         or live[1] != company.xero_tenant_id):
                new_conn, new_tenant = live
                row = db.get(Company, company.id)
                if row is not None:
                    row.nango_connection_id = new_conn
                    row.xero_tenant_id = new_tenant
                    db.commit()
                company.nango_connection_id = new_conn
                company.xero_tenant_id = new_tenant
                logger.info(
                    "[SuHe][Audit] self-healed stale connection for company=%s → %s",
                    company.id, new_conn,
                )
                try:
                    raw_invoices, raw_bank_txns = asyncio.run(
                        _pull_xero_documents(integration, new_conn, new_tenant)
                    )
                    healed = True
                except Exception:    # noqa: BLE001 — fall through to the clear error
                    healed = False
            if not healed:
                row = db.get(Company, company.id)
                if row is not None and not row.needs_reconnect:
                    row.needs_reconnect = True
                    db.commit()
                # A connected company whose Xero pull fails must not fall back to
                # stale seed data; surface a clear "reconnect Xero" error instead.
                logger.exception(
                    "[SuHe][Audit] Xero pull FAILED for company=%s (self-heal "
                    "didn't recover) — surfacing as a connection error",
                    company.id,
                )
                raise RuntimeError(
                    "Xero connection failed — the link looks expired or revoked. "
                    "Reconnect Xero for this organisation, then run the audit again."
                ) from exc
        # Pull succeeded — trust it even if empty; a connected org with no
        # invoices is genuinely empty, not a reason to show seed data.
        row = db.get(Company, company.id)
        if row is not None and row.needs_reconnect:
            row.needs_reconnect = False
            db.commit()
        shaped = [_reshape_xero_to_batch(raw) for raw in raw_invoices]
        # Bank transactions (Money In/Out), tagged RECEIVE/SPEND — feed only the
        # Unexpected-Account/Tax checks (the orchestrator splits them back out).
        shaped += [_reshape_bank_txn_to_batch(raw) for raw in raw_bank_txns]
        transactions = [tx for tx in shaped if tx is not None]
        logger.info(
            "[SuHe][Audit] Fetched %d invoice(s) + %d bank txn(s) via Nango for company=%s",
            len(raw_invoices), len(raw_bank_txns), company.id,
        )
        return transactions, "nango"

    # No Nango connection at all → seed/demo data is the intended source.
    invoices = db.scalars(
        select(Invoice)
        .where(Invoice.company_id == company.id)
        .options(selectinload(Invoice.line_items))
    ).all()
    transactions = [_reshape_invoice(inv) for inv in invoices]
    logger.info(
        "[SuHe][Audit] Fetched %d invoice(s) via seed for company=%s",
        len(transactions), company.id,
    )
    return transactions, "seed"


def _reconciled_invoice_ids(payments: list[dict[str, Any]]) -> set[str]:
    """IDs of invoices/bills whose payment is BANK MATCHED (reconciled) in Xero.
    Built once from the Payments feed so each document can be marked without a
    per-invoice call."""
    out: set[str] = set()
    for p in payments or []:
        if not isinstance(p, dict) or not p.get("IsReconciled"):
            continue
        inv = p.get("Invoice") or {}
        inv_id = (inv.get("InvoiceID") or "").strip()
        if inv_id:
            out.add(inv_id)
    return out


async def _pull_xero_documents(
    integration: IntegrationService,
    connection_id: str,
    tenant_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch all invoices + credit notes via Nango, plus payments (one paged
    call) to mark which documents are bank-reconciled, plus bank transactions
    (Money In/Out) for the Unexpected-Account check. Payments + bank transactions
    are secondary — failing must never break the audit, so they degrade to [].

    Returns ``(invoice_docs, bank_txns)`` — both RAW Xero dicts for the caller to
    reshape.
    """
    invoices, credit_notes, payments, bank_txns = await asyncio.gather(
        integration.fetch_all_invoices(connection_id, tenant_id),
        integration.fetch_all_credit_notes(connection_id, tenant_id),
        integration.fetch_all_payments(connection_id, tenant_id),
        integration.fetch_all_bank_transactions(connection_id, tenant_id),
        return_exceptions=True,
    )
    # Invoices is the critical fetch: if it failed (e.g. expired token), surface
    # it rather than coercing to [], which the caller would read as "no invoices"
    # and serve stale seed data. Other fetches are secondary and may degrade.
    if isinstance(invoices, BaseException):
        raise invoices
    invoices = invoices if isinstance(invoices, list) else []
    credit_notes = credit_notes if isinstance(credit_notes, list) else []
    payments = payments if isinstance(payments, list) else []
    bank_txns = bank_txns if isinstance(bank_txns, list) else []
    reconciled_ids = _reconciled_invoice_ids(payments)
    docs = invoices + credit_notes
    for raw in docs:
        if isinstance(raw, dict):
            inv_id = (raw.get("InvoiceID") or raw.get("CreditNoteID") or "").strip()
            raw["_IsReconciled"] = inv_id in reconciled_ids
    return docs, bank_txns


# Xero account types that live on the Balance Sheet rather than P&L.
_BALANCE_SHEET_ACCOUNT_TYPES: frozenset[str] = frozenset({
    "ASSET", "CURRENT", "FIXED", "NONCURRENT", "PREPAYMENT",
    "CURRLIAB", "TERMLIAB", "LIABILITY", "OTHERL",
    "EQUITY", "CURRLIAB",
})


def _map_xero_accounts(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Xero Account objects → ChartOfAccount dicts.

    Only includes accounts with a Code so the rules engine can reference
    them by code. Accounts without a code (system accounts) are skipped.
    """
    out: list[dict[str, Any]] = []
    for acc in raw:
        if not isinstance(acc, dict):
            continue
        code = (acc.get("Code") or "").strip()
        name = (acc.get("Name") or "").strip()
        if not code or not name:
            continue
        acc_type = (acc.get("Type") or "").strip().upper()
        tax_type = (acc.get("TaxType") or "").strip() or None
        statement = (
            "Balance Sheet" if acc_type in _BALANCE_SHEET_ACCOUNT_TYPES else "P&L"
        )
        out.append({
            "code": code,
            "name": name,
            "type": acc_type,
            "vat_code": tax_type,
            "statement": statement,
        })
    return out


def _xero_bool(value: Any) -> Optional[bool]:
    """Parse Xero's "true"/"false" string booleans → bool (None if absent)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _map_xero_tax_rates(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Xero TaxRate objects → TaxRate dicts."""
    out: list[dict[str, Any]] = []
    for rate in raw:
        if not isinstance(rate, dict):
            continue
        code = (rate.get("TaxType") or "").strip()
        name = (rate.get("Name") or "").strip()
        if not code:
            continue
        effective = rate.get("EffectiveTaxRate") or rate.get("DisplayTaxRate") or 0
        try:
            rate_str = str(round(float(effective), 2))
        except (TypeError, ValueError):
            rate_str = "0"
        out.append({
            "code": code,
            "name": name,
            "rate": rate_str,
            # Xero's authoritative direction flags — used by the
            # wrong-tax-direction check instead of guessing from the name.
            "can_apply_to_expenses": _xero_bool(rate.get("CanApplyToExpenses")),
            "can_apply_to_revenue": _xero_bool(rate.get("CanApplyToRevenue")),
        })
    return out


async def _fetch_xero_org_context(
    integration: IntegrationService,
    connection_id: str,
    tenant_id: str,
) -> dict[str, Any]:
    """Fetch COA, tax rates, and base currency live from Xero.

    Returns a context dict ready to pass to ``_build_context()``.
    Falls back to empty lists (triggering hardcoded fallback in caller)
    on any Nango/Xero error.
    """
    try:
        accounts_raw, tax_rates_raw, org = await asyncio.gather(
            integration.fetch_chart_of_accounts(connection_id, tenant_id),
            integration.fetch_tax_rates(connection_id, tenant_id),
            integration.fetch_organisation(connection_id, tenant_id),
        )
        coa = _map_xero_accounts(accounts_raw)
        tax_rates = _map_xero_tax_rates(tax_rates_raw)
        base_currency = (
            (org.get("BaseCurrency") or "").strip() if isinstance(org, dict) else ""
        ) or "GBP"
        shortcode = (
            (org.get("ShortCode") or "").strip() if isinstance(org, dict) else ""
        ) or None
        logger.info(
            "[SuHe][Audit] fetched live COA (%d accounts), %d tax rates, "
            "base_currency=%s via Nango",
            len(coa), len(tax_rates), base_currency,
        )
        return {
            "coa": coa,
            "tax_rates": tax_rates,
            "base_currency": base_currency,
            "shortcode": shortcode,
            "financial_year_end": _org_year_end(org),
        }
    except Exception:
        logger.exception(
            "[SuHe][Audit] failed to fetch live COA/tax-rates from Nango — "
            "falling back to hardcoded fixtures"
        )
        return {}


def _org_year_end(org: Any) -> dict[str, int]:
    """Xero org's FinancialYearEndMonth/Day → prepayment_review settings ({} when
    the org doesn't carry them)."""
    if not isinstance(org, dict):
        return {}
    try:
        month, day = int(org.get("FinancialYearEndMonth")), int(org.get("FinancialYearEndDay"))
    except (TypeError, ValueError):
        return {}
    if 1 <= month <= 12 and 1 <= day <= 31:
        return {"financial_year_end_month": month, "financial_year_end_day": day}
    return {}


def _accrual_year_end(fy: dict[str, int], today: date) -> date:
    """Most recent COMPLETED financial year-end (the year being closed)."""
    from calendar import monthrange
    m = int(fy.get("financial_year_end_month") or 12)
    d = int(fy.get("financial_year_end_day") or 31)
    ye = date(today.year, m, min(d, monthrange(today.year, m)[1]))
    return ye if ye <= today else date(today.year - 1, m, min(d, monthrange(today.year - 1, m)[1]))


def _db_org_context(company_id: UUID) -> dict[str, Any]:
    """DB-backed twin of ``_fetch_xero_org_context``: read the synced COA, tax
    rates and organisation from our tables and map them with the SAME functions
    the live path uses, so the audit context is identical."""
    try:
        with SyncSessionLocal() as s:
            accounts_raw = db_read.read_raw(s, company_id, "account")
            tax_rates_raw = db_read.read_raw(s, company_id, "tax_rate")
            org = db_read.read_organisation(s, company_id)
            # fixed-asset register (SOP Step 7 dedup); empty until assets.read granted
            assets_raw = db_read.read_raw(s, company_id, "asset")
        coa = _map_xero_accounts(accounts_raw)
        tax_rates = _map_xero_tax_rates(tax_rates_raw)
        base_currency = (
            (org.get("BaseCurrency") or "").strip() if isinstance(org, dict) else ""
        ) or "GBP"
        shortcode = (
            (org.get("ShortCode") or "").strip() if isinstance(org, dict) else ""
        ) or None
        logger.info(
            "[SuHe][Audit] loaded COA (%d accounts), %d tax rates, %d asset(s), "
            "base_currency=%s from DB sync", len(coa), len(tax_rates), len(assets_raw),
            base_currency,
        )
        return {
            "coa": coa,
            "tax_rates": tax_rates,
            "base_currency": base_currency,
            "shortcode": shortcode,
            "assets": assets_raw,
            "financial_year_end": _org_year_end(org),
        }
    except Exception:
        logger.exception(
            "[SuHe][Audit] failed to load COA/tax-rates from DB sync — "
            "falling back to hardcoded fixtures"
        )
        return {}


def _build_context(
    coa: Optional[list[dict[str, Any]]] = None,
    tax_rates: Optional[list[dict[str, Any]]] = None,
    base_currency: Optional[str] = None,
    duplicate_contact_pairs: Optional[list[list[str]]] = None,
    contact_defaults: Optional[list[dict[str, Any]]] = None,
    contact_vat: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "chart_of_accounts": coa or HARDCODED_CHART_OF_ACCOUNTS,
        "tax_rates": tax_rates or HARDCODED_TAX_RATES,
        "base_currency": base_currency or "GBP",
        "duplicate_contact_pairs": duplicate_contact_pairs or [],
    }
    if contact_defaults:
        ctx["contact_defaults"] = contact_defaults
    if contact_vat:
        ctx["contact_vat"] = contact_vat
    return ctx


# =====================================================================
# AI-service HTTP calls
# =====================================================================

# Capital flags eligible for the SOP fixed-asset dedup (Step 7).
_CAPITAL_ISSUE_TYPES = {"capital_item_review"}


def _as_date(value: Any) -> Optional[date]:
    """Best-effort parse of a Xero/ISO date string → date (None if unparseable)."""
    if not value:
        return None
    iso = _xero_date(str(value).strip()) or str(value).strip()
    try:
        return date.fromisoformat(iso[:10])
    except (ValueError, TypeError):
        return None


def _drop_already_capitalised(
    flagged: list[dict[str, Any]],
    transactions: list[dict[str, Any]],
    assets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """SOP Step 7 — drop a capital flag when the item is already on the fixed-asset
    register (strong match only: value ±£1 AND purchase date within 30 days)."""
    if not assets or not flagged:
        return flagged
    registered: list[tuple[float, Optional[date]]] = []
    for a in assets:
        if not isinstance(a, dict):
            continue
        try:
            price = abs(float(a.get("purchasePrice")))
        except (TypeError, ValueError):
            continue
        registered.append((price, _as_date(a.get("purchaseDate"))))
    if not registered:
        return flagged
    tx_date = {
        t.get("transaction_id"): _as_date(t.get("date"))
        for t in transactions if isinstance(t, dict)
    }

    def _already_capitalised(flag: dict[str, Any]) -> bool:
        if flag.get("issue_type") not in _CAPITAL_ISSUE_TYPES:
            return False
        mr = flag.get("match_reasons") or {}
        try:
            amt = abs(float(mr.get("line_amount")))
        except (TypeError, ValueError):
            return False
        fdate = tx_date.get(flag.get("transaction_id"))
        for price, adate in registered:
            if abs(price - amt) <= 1.0 and fdate and adate and abs((fdate - adate).days) <= 30:
                return True
        return False

    kept = [f for f in flagged if not _already_capitalised(f)]
    if len(kept) != len(flagged):
        logger.info(
            "[SuHe][Audit] SOP asset-dedup dropped %d already-capitalised flag(s)",
            len(flagged) - len(kept),
        )
    return kept


def _call_rules_batch(
    transactions: list[dict[str, Any]],
    org_ctx: Optional[dict[str, Any]] = None,
    audit_config: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Synchronous HTTP call to the existing rules-batch endpoint.

    ``org_ctx`` carries live COA/tax-rates fetched from Xero; falls back
    to hardcoded fixtures when absent (seed-data runs).

    ``audit_config`` is the company's per-client rule config
    (``{"disabled_rules": [...], "ignore_before": "YYYY-MM-DD"}``) — it
    skips disabled checks and date-floors the batch.

    Fail-open: any HTTP/timeout/decode error returns ``[]`` so the
    audit completes with zero trapped rows rather than blowing up.
    """
    context = _build_context(
        coa=(org_ctx or {}).get("coa"),
        tax_rates=(org_ctx or {}).get("tax_rates"),
        base_currency=(org_ctx or {}).get("base_currency"),
        duplicate_contact_pairs=(org_ctx or {}).get("duplicate_contact_pairs"),
        contact_defaults=(org_ctx or {}).get("contact_defaults"),
        contact_vat=(org_ctx or {}).get("contact_vat"),
    )
    cfg = audit_config or {}
    disabled_rules = cfg.get("disabled_rules") or []
    ignore_before = cfg.get("ignore_before") or None
    # Per-client tunable thresholds (duplicate window, overdue days, outlier
    # multiple, …). Forwarded verbatim; the rule engine ignores unknown keys
    # and keeps defaults for missing ones.
    # Org's financial year-end (auto-derived from Xero) is the base; any manual
    # setting overrides it. Feeds the prepayment_review check.
    _year_end = (org_ctx or {}).get("financial_year_end") or {}
    rule_settings = {**_year_end, **(cfg.get("settings") or {})} or None

    # Run the rules engine in-process (the pure logic in app/services/healthcheck)
    # rather than over an HTTP hop to the web service, which was fragile.
    from app.shared.transaction import BatchHealthCheckRequest
    from app.modules.healthcheck.engine.orchestrator import run_batch_health_check

    flagged: list[dict[str, Any]] = []
    for start in range(0, len(transactions), _AI_BATCH_CHUNK):
        chunk = transactions[start:start + _AI_BATCH_CHUNK]
        payload: dict[str, Any] = {
            "transactions": chunk,
            "context": context,
            "disabled_rules": disabled_rules,
        }
        if ignore_before:
            payload["ignore_before"] = ignore_before
        if rule_settings:
            payload["settings"] = rule_settings
        try:
            req = BatchHealthCheckRequest(**payload)
            resp = asyncio.run(run_batch_health_check(req))
            flagged.extend(
                f.model_dump(mode="json") if hasattr(f, "model_dump") else f
                for f in (resp.flagged or [])
            )
        except Exception:
            logger.exception(
                "[SuHe][Audit] in-process rules batch failed for "
                "%d transactions; failing open",
                len(chunk),
            )
            continue
    # SOP Step 7: drop capital flags for items already on the fixed-asset register.
    flagged = _drop_already_capitalised(flagged, transactions, (org_ctx or {}).get("assets") or [])
    return flagged


def _fire_and_forget_enrich(
    *,
    batch_id: str,
    company_id: str,
    total_documents: int,
    trapped_rows: list[dict[str, Any]],
) -> None:
    payload = {
        "batch_id": batch_id,
        "company_id": company_id,
        "total_documents": total_documents,
        "trapped_rows": trapped_rows,
    }
    timeout_s = settings.HEALTHCHECK_AI_ENRICH_TIMEOUT_MS / 1000
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(settings.HEALTHCHECK_AI_ENRICH_URL, json=payload)
        logger.info(
            "[SuHe][Audit] enrich-audit queued: HTTP %s for batch=%s rows=%d",
            resp.status_code, batch_id, len(trapped_rows),
        )
    except Exception:
        logger.exception(
            "[SuHe][Audit] enrich-audit fire-and-forget failed (non-fatal)",
        )


# =====================================================================
# Trapped-row grouping + persistence
# =====================================================================

def _group_flags_by_transaction(
    flagged: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Multiple flagged items can share a transaction_id — collapse to
    one bucket per document so we record one HealthCheckResult per
    trapped row."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in flagged:
        tx_id = str(item.get("transaction_id") or "").strip()
        if not tx_id:
            continue
        grouped[tx_id].append(item)
    return grouped


def _persist_trapped(
    *,
    db,
    batch_id: str,
    company_id: UUID,
    transactions_by_id: dict[str, dict[str, Any]],
    grouped: dict[str, list[dict[str, Any]]],
    evaluated_types: Optional[set[str]] = None,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Returns (flagged_total, new_trapped, trapped_rows_for_enrich).

    Works the same for Nango-fetched and seeded data: the
    ``transactions_by_id`` lookup gives us back the already-reshaped
    14-field dict for each flagged transaction.
    """
    new_trapped = 0
    flagged_total = 0
    trapped_rows_for_enrich: list[dict[str, Any]] = []

    for tx_id, items in grouped.items():
        transaction = transactions_by_id.get(tx_id)
        if transaction is None:
            logger.warning(
                "[SuHe][Audit] batch=%s flag for unknown transaction_id=%s",
                batch_id, tx_id,
            )
            continue
        try:
            document_uuid = UUID(tx_id)
        except (TypeError, ValueError):
            logger.warning(
                "[SuHe][Audit] batch=%s flagged transaction_id=%s is not a UUID",
                batch_id, tx_id,
            )
            continue
        document_type = str(transaction.get("type") or "unknown")

        flagged_total += 1
        rule_ids = [str(i.get("issue_type") or "") for i in items if i.get("issue_type")]
        messages = [str(i.get("message") or "") for i in items if i.get("message")]
        joined_msgs = " | ".join(m for m in messages if m)

        result_payload = {
            "flagged": items,
            "rule_ids": rule_ids,
            "messages": joined_msgs,
            "target_ledger": DEFAULT_TARGET_LEDGER,
            "audit_batch_id": batch_id,
            "vendor_name": (transaction.get("vendor_name") or "").strip() or None,
            # Xero ContactID — the STABLE identifier for "Ignore this contact"
            # (vendor_name also works, but the id survives a rename).
            "contact_id": (transaction.get("contact_id") or "").strip() or None,
            "invoice_number": (transaction.get("invoice_number") or "").strip() or None,
            # Human identifier (never the Xero GUID): invoice number for sales
            # invoices, reference for bills.
            "display_number": (
                (transaction.get("invoice_number") or "").strip()
                or (transaction.get("reference") or "").strip()
                or None
            ),
            "amount": str(transaction.get("amount") or ""),
            "currency_code": (transaction.get("currency_code") or "GBP").strip(),
            "invoice_date": transaction.get("date") or None,        # Invoice Date column
            "posted_date": transaction.get("posted_date") or None,  # created/updated in Xero
            "due_date": transaction.get("due_date") or None,
            "amount_due": transaction.get("amount_due"),            # precise Paid? + Void gate
            "amount_paid": transaction.get("amount_paid"),
            "reconciled": transaction.get("reconciled"),            # bank-matched (IsReconciled)
            # payment_status, editable and editable_reason for the "Edit in Xero" hint.
            **_payment_and_edit_state(transaction),
            # "Details" column = line-item description; "Reference" = the Xero
            # Reference field.
            "details": (
                ((transaction.get("line_items") or [{}])[0].get("description")
                 if transaction.get("line_items") else None)
                or transaction.get("description") or ""
            ).strip()[:200] or None,
            # "Reference" column = the Xero Reference field, not the line
            # description; for bills this is the human identifier.
            "reference": (transaction.get("reference") or "").strip()[:200] or None,
            "xero_reference": (transaction.get("reference") or "").strip() or None,
            "invoice_status": (transaction.get("status") or "").strip().upper() or None,
        }

        # Is this document already trapped from a previous run?
        existing_row = db.execute(
            select(HealthCheckResult).where(
                HealthCheckResult.document_id == document_uuid,
                HealthCheckResult.company_id == company_id,
                HealthCheckResult.kind == KIND_POST_LEDGER,
                HealthCheckResult.status == STATUS_BLOCKED,
            ).limit(1)
        ).scalar_one_or_none()
        if existing_row is not None:
            er = dict(existing_row.result or {})
            # Respect explicit user end-states — never resurrect or re-score
            # something the user resolved / dismissed / accepted.
            if er.get("resolved") or er.get("dismissed") or er.get("marked_ok"):
                continue
            # Otherwise re-score with the latest run. For a scoped run merge:
            # replace only the issue types this run evaluated, keep the rest.
            if evaluated_types is None:
                merged_flagged = items
            else:
                kept = [
                    f for f in (er.get("flagged") or [])
                    if (f.get("issue_type") or "") not in evaluated_types
                ]
                merged_flagged = kept + items
            merged_rids = [str(f.get("issue_type")) for f in merged_flagged if f.get("issue_type")]
            merged_msgs = " | ".join(
                m for m in (f.get("message") or "" for f in merged_flagged) if m
            )
            existing_row.result = {
                **result_payload,
                "flagged": merged_flagged,
                "rule_ids": merged_rids,
                "messages": merged_msgs,
            }
            existing_row.error_msgs = (merged_msgs[:1000] or None)
            # Bump ``ran_at`` for a fresh "last checked" time; it only auto-fills
            # on insert, so a re-scored row would otherwise keep its old timestamp.
            existing_row.ran_at = datetime.now(timezone.utc)
            new_trapped += 1
            trapped_rows_for_enrich.append({
                "transaction_id": tx_id,
                "rule_ids": rule_ids,
                "messages": joined_msgs,
                "transaction": transaction,
                "flagged_items": items,
            })
            continue

        db.add(HealthCheckResult(
            company_id=company_id,
            document_id=document_uuid,
            document_type=document_type,
            kind=KIND_POST_LEDGER,
            status=STATUS_BLOCKED,
            error_msgs=(joined_msgs[:1000] or None),
            result=result_payload,
        ))
        new_trapped += 1

        trapped_rows_for_enrich.append({
            "transaction_id": tx_id,
            "rule_ids": rule_ids,
            "messages": joined_msgs,
            "transaction": transaction,
            "flagged_items": items,
        })

    db.flush()
    return flagged_total, new_trapped, trapped_rows_for_enrich


def _auto_clear_stale(
    db,
    *,
    company_id: UUID,
    batch_id: str,
    stale_doc_ids: set[UUID],
) -> int:
    """Reconcile a re-run: stamp ``auto_cleared`` on blocked post-ledger rows
    whose document was re-checked this run but is **no longer flagged**, so
    stale issues drop out of the feed while the row stays in the DB for history.

    ``stale_doc_ids`` = (documents evaluated this run) − (documents flagged this
    run). Documents NOT re-checked this run are never touched, so a partial
    fetch can't wrongly clear issues. Explicit user end-states (resolved /
    dismissed / marked-OK) and already-cleared rows are left untouched.
    Returns the number of rows cleared.
    """
    if not stale_doc_ids:
        return 0
    rows = db.execute(
        select(HealthCheckResult).where(
            HealthCheckResult.company_id == company_id,
            HealthCheckResult.kind == KIND_POST_LEDGER,
            HealthCheckResult.status == STATUS_BLOCKED,
            HealthCheckResult.document_id.in_(stale_doc_ids),
        )
    ).scalars().all()
    now_iso = datetime.now(timezone.utc).isoformat()
    cleared = 0
    for row in rows:
        res = dict(row.result or {})
        if (res.get("resolved") or res.get("dismissed")
                or res.get("marked_ok") or res.get("auto_cleared")):
            continue  # preserve user decisions + don't re-clear
        res["auto_cleared"] = True
        res["auto_cleared_batch_id"] = batch_id
        res["auto_cleared_at"] = now_iso
        row.result = res
        cleared += 1
    return cleared


_DUP_ISSUE_TYPES = {"duplicate_invoice", "duplicate_bill", "duplicate_credit_note"}


def _reconcile_stale_duplicates(
    db,
    *,
    company_id: UUID,
    batch_id: str,
    stale_doc_ids: set[UUID],
) -> int:
    """Duplicates-only reconcile. For each re-checked document no longer flagged
    as a duplicate, **strip just the duplicate part** of its row:

    * row is ONLY a duplicate → auto-clear the whole row (drops from the feed);
    * row also has other issues (e.g. old-unpaid) → remove the duplicate entry
      from ``flagged`` / ``rule_ids`` but keep the row + its other issues.

    User end-states (resolved / dismissed / marked-OK) are left untouched.
    Returns the number of rows changed.
    """
    if not stale_doc_ids:
        return 0
    rows = db.execute(
        select(HealthCheckResult).where(
            HealthCheckResult.company_id == company_id,
            HealthCheckResult.kind == KIND_POST_LEDGER,
            HealthCheckResult.status == STATUS_BLOCKED,
            HealthCheckResult.document_id.in_(stale_doc_ids),
        )
    ).scalars().all()
    now_iso = datetime.now(timezone.utc).isoformat()
    changed = 0
    for row in rows:
        res = dict(row.result or {})
        if (res.get("resolved") or res.get("dismissed")
                or res.get("marked_ok") or res.get("auto_cleared")):
            continue
        rids = res.get("rule_ids") or []
        if not any(r in _DUP_ISSUE_TYPES for r in rids):
            continue  # nothing duplicate to strip
        kept_flagged = [
            f for f in (res.get("flagged") or [])
            if (f.get("issue_type") or "") not in _DUP_ISSUE_TYPES
        ]
        kept_rids = [r for r in rids if r not in _DUP_ISSUE_TYPES]
        if not kept_rids:
            # purely a duplicate → clear the whole row
            res["auto_cleared"] = True
            res["auto_cleared_batch_id"] = batch_id
            res["auto_cleared_at"] = now_iso
        else:
            # keep the other issues, drop the (now stale) duplicate part
            res["flagged"] = kept_flagged
            res["rule_ids"] = kept_rids
            res["messages"] = " | ".join(
                m for m in (f.get("message") or "" for f in kept_flagged) if m
            )
        row.result = res
        changed += 1
    return changed


# =====================================================================
# Task entrypoint
# =====================================================================

@celery_app.task(name="healthcheck.historical_audit", bind=False, max_retries=0)
def historical_audit_task(
    batch_id: str,
    company_id: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    scope: str = "full",
) -> dict[str, Any]:
    """Audit one company's invoices end-to-end, optionally scoped to a period.

    ``date_from`` / ``date_to`` are inclusive ISO date strings (or None for
    all). They come from the frontend's Period selector and limit which
    transactions are audited.

    Returns a small summary dict for Celery's result backend / logs.
    All progress reporting is via the Redis meta hash so the frontend
    can poll it independently of Celery's own state.
    """
    r = _redis_client()
    company_uuid = UUID(company_id)
    summary: dict[str, Any] = {
        "batch_id": batch_id,
        "company_id": company_id,
    }
    period_from = date.fromisoformat(date_from) if date_from else None
    period_to = date.fromisoformat(date_to) if date_to else None

    try:
        # --- 1. fetch ---------------------------------------------------
        _patch_meta(
            r, batch_id,
            stage="fetching",
            stage_label="Loading invoices…",
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
        audit_config: dict[str, Any] = {}
        with SyncSessionLocal() as db:
            company = db.get(Company, company_uuid)
            if company is None:
                raise RuntimeError(f"Company {company_id} vanished mid-audit")
            audit_config = dict(company.audit_config or {})

        # "Run duplicates only": disable every rule except the duplicate checks
        # for this run, so no LLM checks fire and only the duplicate engine runs.
        dup_only = scope == "duplicates"
        if dup_only:
            from app.modules.healthcheck.rules_registry import ALL_RULE_KEYS
            keep = set(_DUP_ISSUE_TYPES)
            existing = set(audit_config.get("disabled_rules") or [])
            audit_config["disabled_rules"] = sorted(existing | (ALL_RULE_KEYS - keep))

        # Fetch invoices for every scope (full + duplicates-only).
        transactions, source = _fetch_audit_transactions(db, company)
        # Accruals need the whole year, so keep the pre-period-filter set.
        full_transactions = list(transactions)

        # Period filter — keep only transactions in [date_from, date_to]
        # (inclusive). Driven by the frontend's Period selector.
        if period_from or period_to:
            def _in_period(tx: dict[str, Any]) -> bool:
                raw = tx.get("date")
                if not raw:
                    return True  # undated → keep (rare)
                try:
                    td = date.fromisoformat(str(raw)[:10])
                except ValueError:
                    return True
                if period_from and td < period_from:
                    return False
                if period_to and td > period_to:
                    return False
                return True

            before = len(transactions)
            transactions = [t for t in transactions if _in_period(t)]
            logger.info(
                "[SuHe][Audit] period filter %s..%s kept %d of %d transactions",
                period_from, period_to, len(transactions), before,
            )

        total = len(transactions)
        transactions_by_id = {tx["transaction_id"]: tx for tx in transactions}
        summary["total"] = total
        summary["source"] = source

        # Fetch live COA + tax rates from Xero when connected via Nango.
        # Seed-data runs keep the hardcoded fixtures as fallback.
        org_ctx: dict[str, Any] = {}
        if source == "db":
            org_ctx = _db_org_context(company_uuid)
        elif source == "nango" and company.nango_connection_id and company.xero_tenant_id:
            org_ctx = asyncio.run(
                _fetch_xero_org_context(
                    IntegrationService(),
                    company.nango_connection_id,
                    company.xero_tenant_id,
                )
            )

        # Backfill the org shortcode for tenant-scoped Xero deep-links; the
        # webhook creates Company rows without it, so it's filled on first audit.
        shortcode = org_ctx.get("shortcode")
        if shortcode:
            with SyncSessionLocal() as db:
                c = db.get(Company, company_uuid)
                if c is not None and not (c.xero_shortcode or "").strip():
                    c.xero_shortcode = shortcode
                    db.commit()

        # Cache the COA in Redis so suggest-fix can pass it to the LLM
        # without an extra Xero round-trip per suggestion call.
        coa = org_ctx.get("coa") or []
        if coa:
            import json as _json_coa
            r.set(
                f"xero_coa:{company_id}",
                _json_coa.dumps(coa),
                ex=7200,  # 2-hour TTL — refreshed on next audit
            )

        # Fetch contacts + run contact checks in parallel with the audit.
        # Contacts come via the Nango proxy (tenant-scoped) so the right
        # org's contacts come back even on a multi-org connection.
        contacts: list[dict[str, Any]] = []
        if source == "db":
            with SyncSessionLocal() as s:
                contacts = db_read.read_raw(s, company_uuid, "contact")
            logger.info(
                "[SuHe][Audit] loaded %d contacts from DB sync for company=%s",
                len(contacts), company_id,
            )
        elif source == "nango" and company.nango_connection_id and company.xero_tenant_id:
            try:
                contacts = asyncio.run(
                    IntegrationService().fetch_contacts(
                        company.nango_connection_id, company.xero_tenant_id,
                    )
                )
                logger.info(
                    "[SuHe][Audit] fetched %d contacts for company=%s",
                    len(contacts), company_id,
                )
            except Exception:
                logger.exception(
                    "[SuHe][Audit] contact fetch failed for company=%s — skipping contact checks",
                    company_id,
                )

        # Duplicate Contacts is flag-for-human only: no aliasing or merging of
        # ContactIDs. run_contact_checks just surfaces the pairs for the user.
        if contacts:
            # Contact defaults → default-based Unexpected-Account (per the spec).
            # Only contacts with at least one saved default are sent.
            try:
                defaults = []
                vat_map: dict[str, str] = {}
                for c in contacts:
                    cid = (c.get("ContactID") or "").strip()
                    if not cid:
                        continue
                    vat = (c.get("TaxNumber") or "").strip()
                    if vat:
                        vat_map[cid] = vat
                    sales = (c.get("SalesDefaultAccountCode") or "").strip() or None
                    purch = (c.get("PurchasesDefaultAccountCode") or "").strip() or None
                    sales_tax = (c.get("AccountsReceivableTaxType") or "").strip() or None
                    purch_tax = (c.get("AccountsPayableTaxType") or "").strip() or None
                    if sales or purch or sales_tax or purch_tax:
                        defaults.append({
                            "contact_id": cid,
                            "sales_account": sales,
                            "purchase_account": purch,
                            "sales_tax": sales_tax,
                            "purchase_tax": purch_tax,
                        })
                if defaults:
                    org_ctx["contact_defaults"] = defaults
                if vat_map:
                    org_ctx["contact_vat"] = vat_map
            except Exception:
                logger.exception(
                    "[SuHe][Audit] contact-defaults computation failed — "
                    "default-based Unexpected-Account/Tax skipped for company=%s",
                    company_id,
                )

        # --- 2. shape ---------------------------------------------------
        _patch_meta(
            r, batch_id,
            stage="shaping",
            stage_label=(
                f"Preparing {total} invoice(s) for AI review "
                f"({source})"
            ),
            total=total,
            source=source,
        )

        # --- 3. rules-batch call ---------------------------------------
        _patch_meta(
            r, batch_id,
            stage="auditing",
            stage_label=f"AI analysing {total} invoice(s)…",
        )
        flagged_items = _call_rules_batch(transactions, org_ctx, audit_config)
        grouped = _group_flags_by_transaction(flagged_items)

        # --- 4. persist -------------------------------------------------
        _patch_meta(
            r, batch_id,
            stage="persisting",
            stage_label=f"Saving {len(grouped)} flagged item(s)",
        )
        with SyncSessionLocal() as db:
            flagged_total, new_trapped, trapped_rows = _persist_trapped(
                db=db,
                batch_id=batch_id,
                company_id=company_uuid,
                transactions_by_id=transactions_by_id,
                grouped=grouped,
                # Scoped run: only these issue types were re-evaluated, so merge
                # rather than overwrite other checks' flags on the same document.
                evaluated_types=(set(_DUP_ISSUE_TYPES) if dup_only else None),
            )
            db.commit()

        # --- 5. finalise -----------------------------------------------
        completed_at = datetime.now(timezone.utc).isoformat()
        _patch_meta(
            r, batch_id,
            status="completed",
            stage="completed",
            stage_label=(
                f"Audit complete — {flagged_total} of {total} flagged "
                f"({new_trapped} new)"
            ),
            total=total,
            trapped=flagged_total,
            new_trapped=new_trapped,
            completed_at=completed_at,
        )

        with SyncSessionLocal() as db:
            row = db.get(AuditBatch, UUID(batch_id))
            if row is not None:
                row.status = "completed"
                row.total = total
                row.trapped = flagged_total
                row.new_trapped = new_trapped
                row.contacts_total = len(contacts)
                row.completed_at = datetime.now(timezone.utc)
                db.commit()

        # --- 6. contact health checks -----------------------------------
        contact_flags: list = []
        if contacts and not dup_only:   # duplicates-only run skips contact checks
            from app.modules.healthcheck.engine.contact_checks import run_contact_checks
            from app.modules.healthcheck.engine.audit_settings import AuditSettings
            from datetime import date as _date
            # Pass the audited transactions (incl. bank txns) so the inactive
            # check can build each contact's last-activity date + age.
            contact_flags = run_contact_checks(
                contacts, transactions, today=_date.today(),
                settings=AuditSettings.from_config(audit_config.get("settings")),
            )
            if contact_flags:
                with SyncSessionLocal() as db:
                    # Group all of a contact's flags by ContactID into one row so
                    # the contact shows under every relevant check without one
                    # issue type clobbering another on the shared ContactID.
                    flags_by_contact: dict[str, list] = defaultdict(list)
                    for flag in contact_flags:
                        cid = (flag.get("contact_id") or "").strip()
                        if cid:
                            flags_by_contact[cid].append(flag)
                    new_contact_issues = 0
                    for cid_str, cflags in flags_by_contact.items():
                        try:
                            contact_uuid = UUID(cid_str)
                        except (TypeError, ValueError):
                            continue
                        existing_row = db.execute(
                            select(HealthCheckResult).where(
                                HealthCheckResult.document_id == contact_uuid,
                                HealthCheckResult.company_id == company_uuid,
                                HealthCheckResult.kind == KIND_POST_LEDGER,
                                HealthCheckResult.status == STATUS_BLOCKED,
                            ).limit(1)
                        ).scalar_one_or_none()
                        # Respect explicit user end-states — never resurrect or
                        # re-score something the user resolved/dismissed/accepted.
                        if existing_row is not None:
                            er = dict(existing_row.result or {})
                            if er.get("resolved") or er.get("dismissed") or er.get("marked_ok"):
                                continue
                        # Each flag carries its own fields (duplicate: helper /
                        # partner_helper / name_similarity / vat_status; defaults:
                        # missing_defaults / current_defaults). Keep them all.
                        flagged_items = []
                        for flag in cflags:
                            item = {k: v for k, v in flag.items() if k != "contact_id"}
                            item["transaction_id"] = cid_str
                            if flag.get("partner_id"):
                                item.setdefault("duplicate_of_transaction_id", flag["partner_id"])
                            flagged_items.append(item)
                        messages = " | ".join(
                            f.get("message", "") for f in cflags if f.get("message")
                        )
                        result_payload = {
                            "flagged": flagged_items,
                            "rule_ids": [f.get("issue_type") for f in cflags if f.get("issue_type")],
                            "messages": messages,
                            "target_ledger": DEFAULT_TARGET_LEDGER,
                            "audit_batch_id": batch_id,
                            "vendor_name": cflags[0].get("contact_name", ""),
                        }
                        if existing_row is not None:
                            # Re-score in place so a re-run reflects the latest
                            # logic and a fresh "last checked" time.
                            existing_row.result = result_payload
                            existing_row.error_msgs = messages[:1000]
                            existing_row.ran_at = datetime.now(timezone.utc)
                        else:
                            db.add(HealthCheckResult(
                                company_id=company_uuid,
                                document_id=contact_uuid,
                                document_type="CONTACT",
                                kind=KIND_POST_LEDGER,
                                status=STATUS_BLOCKED,
                                error_msgs=messages[:1000],
                                result=result_payload,
                            ))
                        new_contact_issues += 1
                    db.commit()
                logger.info(
                    "[SuHe][Audit] batch=%s persisted %d contact issues",
                    batch_id, new_contact_issues,
                )

        # --- 6a2. missing accruals (account-level, whole financial year) --
        if not dup_only and "missing_accrual" not in set(audit_config.get("disabled_rules") or []):
            from app.modules.healthcheck.checks.missing_accrual import find_missing_accruals
            from app.shared.transaction import BatchTransaction
            with SyncSessionLocal() as s:
                accounts_raw = db_read.read_raw(s, company_uuid, "account")
            coa_id, coa_nm, coa_ty = {}, {}, {}
            for a in accounts_raw:
                c = str(a.get("Code") or "").strip()
                if c:
                    coa_id[c] = (a.get("AccountID") or "").strip() or None
                    coa_nm[c], coa_ty[c] = a.get("Name") or c, a.get("Type") or ""
            acc_txs = []
            for t in full_transactions:
                try:
                    acc_txs.append(BatchTransaction(**t))
                except Exception:  # noqa: BLE001 — skip a malformed row
                    pass
            ye = _accrual_year_end((org_ctx or {}).get("financial_year_end") or {}, date.today())
            accrual_flags = find_missing_accruals(acc_txs, coa_nm, coa_ty, ye, coa_id=coa_id)

            flagged_acc_uuids: set[UUID] = set()
            with SyncSessionLocal() as db:
                by_account: dict[str, list] = defaultdict(list)
                for flag in accrual_flags:
                    aid = (flag.get("account_id") or "").strip()
                    if aid:
                        by_account[aid].append(flag)
                for aid_str, aflags in by_account.items():
                    try:
                        acc_uuid = UUID(aid_str)
                    except (TypeError, ValueError):
                        continue
                    flagged_acc_uuids.add(acc_uuid)
                    existing_row = db.execute(
                        select(HealthCheckResult).where(
                            HealthCheckResult.document_id == acc_uuid,
                            HealthCheckResult.company_id == company_uuid,
                            HealthCheckResult.kind == KIND_POST_LEDGER,
                            HealthCheckResult.status == STATUS_BLOCKED,
                        ).limit(1)
                    ).scalar_one_or_none()
                    if existing_row is not None:
                        er = dict(existing_row.result or {})
                        if er.get("resolved") or er.get("dismissed") or er.get("marked_ok"):
                            continue
                    items = []
                    for f in aflags:
                        item = {k: v for k, v in f.items() if k != "account_id"}
                        item["transaction_id"] = aid_str
                        items.append(item)
                    messages = " | ".join(f.get("message", "") for f in aflags if f.get("message"))
                    payload = {
                        "flagged": items,
                        "rule_ids": ["missing_accrual"],
                        "messages": messages,
                        "target_ledger": DEFAULT_TARGET_LEDGER,
                        "audit_batch_id": batch_id,
                        "vendor_name": aflags[0].get("account_name", ""),
                    }
                    if existing_row is not None:
                        existing_row.result = payload
                        existing_row.error_msgs = messages[:1000]
                        existing_row.ran_at = datetime.now(timezone.utc)
                    else:
                        db.add(HealthCheckResult(
                            company_id=company_uuid, document_id=acc_uuid,
                            document_type="ACCOUNT", kind=KIND_POST_LEDGER,
                            status=STATUS_BLOCKED, error_msgs=messages[:1000], result=payload,
                        ))
                db.commit()

            # Auto-clear accrual rows for accounts no longer flagged this run.
            with SyncSessionLocal() as db:
                prior_acc = db.scalars(
                    select(HealthCheckResult.document_id).where(
                        HealthCheckResult.company_id == company_uuid,
                        HealthCheckResult.document_type == "ACCOUNT",
                        HealthCheckResult.kind == KIND_POST_LEDGER,
                        HealthCheckResult.status == STATUS_BLOCKED,
                    )
                ).all()
                stale_acc = {d for d in prior_acc if d not in flagged_acc_uuids}
                if stale_acc:
                    _auto_clear_stale(db, company_id=company_uuid, batch_id=batch_id, stale_doc_ids=stale_acc)
                    db.commit()
            logger.info("[SuHe][Audit] batch=%s persisted %d accrual account(s)", batch_id, len(flagged_acc_uuids))

        # --- 6b. reconcile: auto-clear rows this run no longer flags ------
        # "Latest run wins": documents re-checked this run that are no longer
        # flagged get auto-cleared (drop out of the feed). Scoped to documents
        # we actually evaluated, so a partial fetch can't wrongly clear issues.
        evaluated_doc_ids: set[UUID] = set()
        for tx_id in transactions_by_id:
            try:
                evaluated_doc_ids.add(UUID(str(tx_id)))
            except (TypeError, ValueError):
                pass
        for c in (contacts or []):
            cid = (c.get("ContactID") or "").strip()
            if cid:
                try:
                    evaluated_doc_ids.add(UUID(cid))
                except (TypeError, ValueError):
                    pass
        flagged_doc_ids: set[UUID] = set()
        for tx_id in grouped:
            try:
                flagged_doc_ids.add(UUID(str(tx_id)))
            except (TypeError, ValueError):
                pass
        for flag in contact_flags:
            cid = (flag.get("contact_id") or "").strip()
            if cid:
                try:
                    flagged_doc_ids.add(UUID(cid))
                except (TypeError, ValueError):
                    pass
        stale_doc_ids = evaluated_doc_ids - flagged_doc_ids
        if stale_doc_ids:
            with SyncSessionLocal() as db:
                if dup_only:
                    # Strip only the stale duplicate part; keep other issues.
                    cleared = _reconcile_stale_duplicates(
                        db, company_id=company_uuid, batch_id=batch_id,
                        stale_doc_ids=stale_doc_ids,
                    )
                else:
                    cleared = _auto_clear_stale(
                        db, company_id=company_uuid, batch_id=batch_id,
                        stale_doc_ids=stale_doc_ids,
                    )
                db.commit()
            if cleared:
                logger.info(
                    "[SuHe][Audit] batch=%s auto-cleared %d stale row(s)",
                    batch_id, cleared,
                )

        # --- 7. background pre-warm AI cache ----------------------------
        # Enrich only newly-trapped rows so the cache is warm before the user
        # opens the dashboard. Skipped on a duplicates-only run and whenever LLM
        # checks are disabled, since the pre-warm would only loop on errors.
        if (
            trapped_rows and not dup_only
            and settings.LLM_CHECKS_ENABLED and settings.GROQ_API_KEY
        ):
            enrich_payloads = [
                {
                    "document_id": row.get("transaction_id", ""),
                    "row": {
                        "transaction_id": row.get("transaction_id", ""),
                        "rule_ids": row.get("rule_ids", []),
                        "messages": row.get("messages", ""),
                        "transaction": row.get("transaction", {}),
                        "flagged_items": row.get("flagged_items", []),
                    },
                }
                for row in trapped_rows
            ]
            prewarm_insights_task.delay(enrich_payloads)
            logger.info(
                "[SuHe][Audit] batch=%s queued prewarm for %d new row(s)",
                batch_id, len(enrich_payloads),
            )

        summary.update({
            "status": "completed",
            "trapped": flagged_total,
            "new_trapped": new_trapped,
        })
        return summary

    except Exception as exc:
        logger.exception(
            "[SuHe][Audit] batch=%s FAILED: %s", batch_id, exc,
        )
        _patch_meta(
            r, batch_id,
            status="failed",
            stage="failed",
            stage_label="Audit failed — see error",
            error=str(exc)[:500],
        )
        try:
            with SyncSessionLocal() as db:
                row = db.get(AuditBatch, UUID(batch_id))
                if row is not None:
                    row.status = "failed"
                    row.completed_at = datetime.now(timezone.utc)
                    db.commit()
        except Exception:
            logger.exception(
                "[SuHe][Audit] failed to mark audit_batch=%s as failed",
                batch_id,
            )
        raise


# =====================================================================
# Fast parallel pre-warm (post-audit background cache fill)
# =====================================================================

@celery_app.task(name="healthcheck.prewarm_insights", bind=False, max_retries=0)
def prewarm_insights_task(rows_payload: list[dict]) -> dict:
    """Enrich trapped rows directly via Groq (no HTTP roundtrip) and write
    to Redis.  Processes rows in parallel batches of 3 so the cache is warm
    within seconds of audit completion.

    Each payload entry has the same shape as reenrich_missing_task.
    """
    from app.core.config import settings as _settings

    # Skip when LLM checks are off or no Groq key is set: without a provider the
    # enrichment can only loop on connection errors and starve syncs.
    if not (_settings.LLM_CHECKS_ENABLED and _settings.GROQ_API_KEY):
        logger.info(
            "[SuHe][Prewarm] LLM unavailable (disabled or no GROQ_API_KEY) — "
            "skipping enrichment for %d row(s)", len(rows_payload),
        )
        return {"cached": 0, "skipped": len(rows_payload), "reason": "llm_unavailable"}

    import json as _json
    import redis as _redis_sync
    from groq import Groq

    rc = _redis_sync.from_url(_settings.REDIS_URL, decode_responses=True)
    client = Groq(api_key=_settings.GROQ_API_KEY)
    _ROW_KEY_PREFIX = "health_check_ai"
    _PARALLEL = 2  # rows per batch — kept small to stay under Groq 6k TPM free limit

    system_prompt = (
        "You are a senior UK chartered accountant reviewing flagged Xero transactions. "
        "For EACH input item write a specific, informative insight covering: "
        "(1) exactly what is wrong (vendor, amount, account), "
        "(2) why it matters financially or for compliance, "
        "(3) the precise corrective action. "
        "For an account recode (miscategorisation / wrong_category) do NOT mention VAT, tax recovery, "
        "or tax treatment at all — not even conditionally — because recoding an account never changes "
        "VAT (VAT follows the tax code, not the account); state it as a misstated P&L line. Only "
        "discuss VAT/tax when the finding itself is about a tax code or treatment. "
        "Use the vendor name and amounts from the data. Be direct. "
        'Return ONLY JSON: {"results": [{"id": int, "explanation": string (<=440 chars, complete sentence), '
        '"severity_ai": "critical"|"high"|"medium"|"low", "confidence": 0..1, "regulatory_ref": string|null}]}. '
        "No markdown, no extra keys."
    )

    def _enrich_batch(batch: list[dict]) -> int:
        """Enrich up to _PARALLEL rows in one LLM call. Returns count written."""
        from app.modules.ai.templates import get_context as _get_context
        items = []
        for i, entry in enumerate(batch):
            row = entry.get("row") or {}
            tx = row.get("transaction") or {}
            rule_ids = row.get("rule_ids") or []
            primary_rule = rule_ids[0] if rule_ids else ""
            so_what, solution = _get_context(primary_rule)
            items.append({
                "id": i,
                "vendor": tx.get("vendor_name") or tx.get("vendor") or "Unknown",
                "amount": tx.get("amount") or "unknown",
                "currency": tx.get("currency_code") or "GBP",
                "account_code": tx.get("current_account_code") or tx.get("account_code") or "unknown",
                "doc_type": tx.get("type") or "unknown",
                "rule_ids": rule_ids,
                "deterministic_finding": (row.get("messages") or "")[:300],
                "flagged_detail": (row.get("flagged_items") or [])[:3],
                "business_impact": so_what,
                "recommended_action": solution,
            })
        user_msg = (
            f"Transactions ({len(items)} items): {_json.dumps(items, default=str)}\n"
            "Return one result per item matched by id. Ground the explanation in "
            "the business_impact — what financially goes wrong if this is not fixed."
        )
        try:
            resp = client.chat.completions.create(
                model=_settings.GROQ_INSIGHT_MODEL,
                max_tokens=350 * len(batch) + 600,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
            )
            data = _json.loads(resp.choices[0].message.content or "{}")
        except Exception as exc:
            logger.warning("[SuHe][Prewarm] LLM batch failed: %s", exc)
            return 0

        results = data.get("results") if isinstance(data, dict) else []
        if not isinstance(results, list):
            return 0

        by_id = {int(r["id"]): r for r in results if isinstance(r, dict) and "id" in r}
        written = 0
        for i, entry in enumerate(batch):
            row = entry.get("row") or {}
            doc_id = row.get("transaction_id") or entry.get("document_id") or ""
            if not doc_id:
                continue
            item = by_id.get(i)
            if not item:
                continue
            explanation = str(item.get("explanation") or "").strip()[:450]
            if not explanation:
                continue
            severity = str(item.get("severity_ai") or "medium").lower()
            if severity not in {"critical", "high", "medium", "low"}:
                severity = "medium"
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence == 0.0 and len(explanation) > 40:
                confidence = 0.6
            reg_ref = item.get("regulatory_ref")
            record = {
                "explanation": explanation,
                "severity_ai": severity,
                "confidence": confidence,
                "regulatory_ref": reg_ref if isinstance(reg_ref, str) and reg_ref.strip() else None,
            }
            try:
                rc.set(
                    f"{_ROW_KEY_PREFIX}:{doc_id}",
                    _json.dumps(record),
                    ex=_settings.HEALTHCHECK_AI_TTL_SECONDS,
                )
                written += 1
            except Exception as exc:
                logger.warning("[SuHe][Prewarm] Redis write failed doc=%s: %s", doc_id, exc)
        return written

    total = len(rows_payload)
    enriched = 0
    for start in range(0, total, _PARALLEL):
        batch = rows_payload[start:start + _PARALLEL]
        enriched += _enrich_batch(batch)
        logger.info("[SuHe][Prewarm] %d/%d rows cached", enriched, total)
        # Stay under Groq free tier: 6000 TPM / ~1400 tokens per 2-row call
        # = ~4 calls/min safe. Sleep 15s between batches.
        if start + _PARALLEL < total:
            _time.sleep(15)

    logger.info("[SuHe][Prewarm] done — %d/%d enriched", enriched, total)
    return {"total": total, "enriched": enriched}


# =====================================================================
# Re-enrichment sweep
# =====================================================================

import time as _time  # local import keeps the top-of-file imports tidy


@celery_app.task(
    name="healthcheck.reenrich_missing",
    bind=False,
    max_retries=0,
)
def reenrich_missing_task(
    rows_payload: list[dict[str, Any]],
    *,
    inter_call_sleep_s: float = 1.0,
) -> dict[str, Any]:
    """Process re-enrichment one row at a time with a polite sleep
    so Groq's free-tier TPM cap doesn't clip the burst again.

    Each payload entry has the shape ``/api/v1/enrich-row`` expects:
    ``{"row_id": ..., "document_id": ..., "row": {transaction_id,
    rule_ids, messages, transaction, flagged_items}}``.
    """
    # Skip when LLM checks are off or no Groq key is set, rather than firing a
    # burst of enrich-row calls that would only fail and tie up the worker.
    if not (settings.LLM_CHECKS_ENABLED and settings.GROQ_API_KEY):
        logger.info(
            "[SuHe][Reenrich] LLM unavailable (disabled or no GROQ_API_KEY) — "
            "skipping re-enrichment for %d row(s)", len(rows_payload),
        )
        return {
            "processed": 0, "enriched": 0, "failed": 0,
            "skipped": len(rows_payload), "reason": "llm_disabled",
        }

    rows_processed = 0
    rows_enriched = 0
    rows_failed = 0
    timeout_s = max(2.0, settings.HEALTHCHECK_AI_ENRICH_TIMEOUT_MS / 1000)
    base_url = settings.HEALTHCHECK_AI_ENRICH_URL.rsplit("/", 1)[0]
    enrich_row_url = f"{base_url}/enrich-row"

    with httpx.Client(timeout=timeout_s) as client:
        for entry in rows_payload:
            rows_processed += 1
            row_id = entry.get("row_id")
            try:
                resp = client.post(
                    enrich_row_url,
                    json={"row": entry.get("row") or {}},
                )
                if resp.status_code == 200 and (
                    (resp.json() or {}).get("status") == "enriched"
                ):
                    rows_enriched += 1
                else:
                    rows_failed += 1
                    logger.warning(
                        "[SuHe][Reenrich] row=%s HTTP %s — %s",
                        row_id, resp.status_code, resp.text[:140],
                    )
            except Exception:
                rows_failed += 1
                logger.exception(
                    "[SuHe][Reenrich] row=%s transport error", row_id,
                )
            if inter_call_sleep_s > 0 and rows_processed < len(rows_payload):
                _time.sleep(inter_call_sleep_s)

    logger.info(
        "[SuHe][Reenrich] processed=%d enriched=%d failed=%d",
        rows_processed, rows_enriched, rows_failed,
    )
    return {
        "rows_processed": rows_processed,
        "rows_enriched": rows_enriched,
        "rows_failed": rows_failed,
    }


# =====================================================================
# Multi-org reconcile — pick up orgs the accountant gained/lost access to
# =====================================================================

@celery_app.task(name="healthcheck.reconcile_connections", bind=False, max_retries=0)
def reconcile_connections_task() -> dict[str, Any]:
    """Re-enumerate every accountant's Xero orgs and reconcile.

    Xero fires NO 'new org' event, so we poll: for each distinct
    accountant connection, list the live tenants and:
      * NEW org (live, not in DB)  → create Company + link user + audit.
      * MISSING org (in DB, gone from live) → mark Company.is_active = False
        (kept, not deleted, so audit history survives; reappears → reactivate).

    Run on a daily Celery-beat schedule. Safe to run ad-hoc.
    """
    integration = IntegrationService()
    created = 0
    deactivated = 0
    reactivated = 0
    connections_checked = 0

    # One row per distinct accountant connection.
    with SyncSessionLocal() as db:
        users = db.execute(
            select(User).where(User.nango_connection_id.isnot(None))
        ).scalars().all()
        conn_to_user = {
            u.nango_connection_id: (u.id, u.firm_id)
            for u in users if u.nango_connection_id
        }

    for connection_id, (user_id, firm_id) in conn_to_user.items():
        connections_checked += 1
        try:
            live = asyncio.run(integration.list_tenants(connection_id))
        except Exception:
            logger.exception(
                "[SuHe][Reconcile] tenant enumeration raised for connection=%s "
                "— leaving its orgs untouched",
                connection_id,
            )
            continue

        # list_tenants is fail-open: it returns [] on any transient error, which
        # is indistinguishable from "couldn't reach Xero". Treat empty as
        # unconfirmed and skip so a flaky run never mass-deactivates a client list.
        if not live:
            logger.warning(
                "[SuHe][Reconcile] connection=%s returned no tenants — skipping "
                "(transient failure or dead connection; not deactivating its orgs)",
                connection_id,
            )
            continue

        live_ids = {t["tenant_id"] for t in live}
        live_names = {t["tenant_id"]: t["tenant_name"] for t in live}

        with SyncSessionLocal() as db:
            companies = db.execute(
                select(Company).where(Company.nango_connection_id == connection_id)
            ).scalars().all()
            db_ids = {c.xero_tenant_id for c in companies}
            new_company_ids: list[str] = []

            # New orgs the accountant gained access to.
            for tenant_id in live_ids - db_ids:
                company = Company(
                    name=live_names.get(tenant_id) or "Untitled org",
                    firm_id=firm_id,
                    nango_connection_id=connection_id,
                    xero_tenant_id=tenant_id,
                    is_active=True,
                )
                db.add(company)
                db.flush()
                db.add(UserCompanyAccess(user_id=user_id, company_id=company.id))
                new_company_ids.append(str(company.id))
                created += 1

            # Orgs gone from the live grant → deactivate (don't delete).
            # Reappeared orgs → reactivate.
            for company in companies:
                if company.xero_tenant_id not in live_ids and company.is_active:
                    company.is_active = False
                    deactivated += 1
                elif company.xero_tenant_id in live_ids and not company.is_active:
                    company.is_active = True
                    reactivated += 1
            db.commit()

        # Audit the newly-discovered orgs.
        for cid in new_company_ids:
            try:
                _dispatch_audit_sync(cid)
            except Exception:
                logger.exception(
                    "[SuHe][Reconcile] audit dispatch failed for company=%s", cid,
                )

    logger.info(
        "[SuHe][Reconcile] connections=%d created=%d deactivated=%d reactivated=%d",
        connections_checked, created, deactivated, reactivated,
    )
    return {
        "connections_checked": connections_checked,
        "created": created,
        "deactivated": deactivated,
        "reactivated": reactivated,
    }


def _dispatch_audit_sync(company_id: str) -> None:
    """Sync audit dispatch for Celery context: insert AuditBatch, seed Redis
    meta, enqueue the worker. Mirrors AuditService.dispatch_audit without the
    async session."""
    import json as _json
    from datetime import datetime, timezone
    from uuid import uuid4

    batch_id = str(uuid4())
    with SyncSessionLocal() as db:
        db.add(AuditBatch(
            id=UUID(batch_id),
            company_id=UUID(company_id),
            status="in_progress",
            total=0, trapped=0, new_trapped=0,
        ))
        db.commit()

    r = _redis_client()
    meta = {
        "company_id": company_id,
        "batch_id": batch_id,
        "status": "in_progress",
        "stage": "dispatched",
        "stage_label": "Audit queued…",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total": 0, "trapped": 0, "new_trapped": 0,
    }
    key = batch_key(batch_id)
    r.hset(key, META_FIELD, _json.dumps(meta))
    r.expire(key, settings.HEALTHCHECK_BATCH_HASH_TTL_SECONDS)

    historical_audit_task.delay(batch_id, company_id)
