"""Demo outbound health-check endpoint.

Simulates EazyCapture publishing invoices to Xero — runs them through
the pre-ledger firewall so the frontend can demonstrate the outbound
flow without a live Django backend.

POST /api/v1/demo/run-outbound
    Runs a fixed set of demo invoices through the rules engine.
    Returns flagged issues exactly as the real pre-ledger firewall would.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter

from app.schemas.transaction import (
    BatchContext,
    BatchHealthCheckRequest,
    BatchTransaction,
)
from app.modules.healthcheck.engine import run_batch_health_check

router = APIRouter(tags=["demo"])

# Demo invoices representing documents about to be published to Xero.
# Each has at least one deliberate issue so the demo surfaces real flags.

_TODAY = date.today()

_DEMO_TRANSACTIONS: list[dict[str, Any]] = [
    {
        "transaction_id": "demo-001",
        "date": _TODAY.isoformat(),
        "description": "Adobe Creative Cloud subscription",
        "amount": "84.99",
        "vendor_name": "Adobe Systems",
        "tax_code": None,                     # ← missing purchase tax
        "current_account_code": "412",
        "invoice_number": "ADB-2026-001",
        "status": "DRAFT",
        "currency_code": "GBP",
        "type": "ACCPAY",
    },
    {
        "transaction_id": "demo-002",
        "date": (_TODAY + timedelta(days=45)).isoformat(),  # ← future dated
        "description": "Office furniture",
        "amount": "2400.00",
        "vendor_name": "IKEA Business",
        "tax_code": "INPUT",
        "current_account_code": "461",
        "invoice_number": "IKB-88821",
        "status": "DRAFT",
        "currency_code": "GBP",
        "type": "ACCPAY",
    },
    {
        "transaction_id": "demo-003",
        "date": _TODAY.isoformat(),
        "description": "Dell XPS 15 laptop",
        "amount": "1899.00",
        "vendor_name": "Dell Technologies",
        "tax_code": "INPUT",
        "current_account_code": "461",         # ← should be fixed asset (720)
        "invoice_number": "DELL-56781",
        "status": "DRAFT",
        "currency_code": "GBP",
        "type": "ACCPAY",
    },
    {
        "transaction_id": "demo-004",
        "date": _TODAY.isoformat(),
        "description": "Monthly consulting retainer",
        "amount": "3500.00",
        "vendor_name": "McKinsey & Company",
        "tax_code": "OUTPUT",                  # ← sales tax on a purchase bill
        "current_account_code": "412",
        "invoice_number": "MCK-2026-Q2",
        "status": "DRAFT",
        "currency_code": "GBP",
        "type": "ACCPAY",
    },
    {
        "transaction_id": "demo-005",
        "date": _TODAY.isoformat(),
        "description": "Monthly consulting retainer",
        "amount": "3500.00",
        "vendor_name": "McKinsey & Company",   # ← duplicate of demo-004
        "tax_code": "INPUT",
        "current_account_code": "412",
        "invoice_number": "MCK-2026-Q2-DUP",
        "status": "DRAFT",
        "currency_code": "GBP",
        "type": "ACCPAY",
    },
    {
        "transaction_id": "demo-006",
        "date": _TODAY.isoformat(),
        "description": "AWS cloud services",
        "amount": "1240.00",
        "vendor_name": "Amazon Web Services",
        "tax_code": "INPUT",
        "current_account_code": "412",         # ← should be 485 (subscriptions)
        "invoice_number": "AWS-INV-20260601",
        "status": "DRAFT",
        "currency_code": "GBP",
        "type": "ACCPAY",
    },
    {
        "transaction_id": "demo-007",
        "date": _TODAY.isoformat(),
        "description": "Sales revenue — Q2 services",
        "amount": "12500.00",
        "vendor_name": "Acme Corp",
        "tax_code": "INPUT",                   # ← purchase tax on a sales invoice
        "current_account_code": "200",
        "invoice_number": "INV-2026-Q2",
        "status": "DRAFT",
        "currency_code": "GBP",
        "type": "ACCREC",
    },
    {
        "transaction_id": "demo-008",
        "date": _TODAY.isoformat(),
        "description": "Office rent — June 2026",
        "amount": "4200.00",
        "vendor_name": "Landlord Holdings Ltd",
        "tax_code": "INPUT",
        "current_account_code": "445",
        "invoice_number": None,                # ← missing invoice number
        "status": "AUTHORISED",
        "currency_code": "GBP",
        "type": "ACCPAY",
    },
]

_DEMO_COA = [
    {"code": "200", "name": "Sales", "type": "REVENUE"},
    {"code": "412", "name": "Consulting & Accounting", "type": "EXPENSE"},
    {"code": "445", "name": "Rent", "type": "EXPENSE"},
    {"code": "461", "name": "Office Equipment", "type": "EXPENSE"},
    {"code": "485", "name": "Subscriptions", "type": "EXPENSE"},
    {"code": "493", "name": "Travel", "type": "EXPENSE"},
    {"code": "720", "name": "Computer Equipment", "type": "FIXEDASSET"},
    {"code": "740", "name": "Furniture & Fittings", "type": "FIXEDASSET"},
]

_DEMO_TAX_RATES = [
    {"code": "INPUT", "name": "Tax on Purchases", "rate": "20.0"},
    {"code": "OUTPUT", "name": "Tax on Sales", "rate": "20.0"},
    {"code": "NONE", "name": "No Tax", "rate": "0.0"},
]


@router.post(
    "/demo/run-outbound",
    summary="Demo: simulate EazyCapture publishing documents to Xero.",
)
async def run_outbound_demo() -> dict[str, Any]:
    """Runs a fixed set of demo invoices through the pre-ledger rules engine.

    Returns EVERYTHING the frontend needs in one payload:
      - transactions: the rows to render in the inspector table
      - flagged:      the issues found, keyed by transaction_id
      - flags_by_txn: same flags pre-grouped by transaction_id (convenience)
      - summary:      scanned / flagged / duplicate-group counts

    Simulates what happens when EazyCapture publishes invoices to Xero —
    each demo invoice has at least one deliberate issue so the frontend
    can show realistic trapped results without a live backend.
    """
    from app.schemas.transaction import ChartOfAccount, TaxRate
    from app.core.redis_client import get_redis

    # Cache the demo result for a consistent response on every run, since the
    # LLM-based wrong_category checks vary and can rate-limit between calls.
    redis = get_redis()
    cache_key = "demo:run-outbound:v1"
    try:
        cached = await redis.get(cache_key)
        if cached:
            import json as _json
            return _json.loads(cached)
    except Exception:
        pass  # fall through to a live run if Redis is unavailable

    transactions = [
        BatchTransaction(**{k: v for k, v in tx.items() if v is not None or k in (
            "tax_code", "current_account_code", "invoice_number",
            "status", "amount_paid", "amount_due",
        )})
        for tx in _DEMO_TRANSACTIONS
    ]

    context = BatchContext(
        chart_of_accounts=[ChartOfAccount(**a) for a in _DEMO_COA],
        tax_rates=[TaxRate(**t) for t in _DEMO_TAX_RATES],
        base_currency="GBP",
    )

    req = BatchHealthCheckRequest(transactions=transactions, context=context)
    result = await run_batch_health_check(req)

    flagged = [f.model_dump(mode="json") for f in result.flagged]

    # Pre-group flags by transaction_id so the frontend can render badges
    # per row without doing the grouping itself.
    flags_by_txn: dict[str, list[dict[str, Any]]] = {}
    for f in flagged:
        flags_by_txn.setdefault(f["transaction_id"], []).append(f)

    # Count distinct duplicate groups (pairs sharing a duplicate link).
    dup_partners = {
        tuple(sorted([f["transaction_id"], f["duplicate_of_transaction_id"]]))
        for f in flagged
        if f.get("duplicate_of_transaction_id")
    }

    # Echo the original transactions back as plain dicts for the table.
    txn_rows = [
        {k: v for k, v in tx.items()}
        for tx in _DEMO_TRANSACTIONS
    ]

    payload = {
        "status": "complete",
        "transactions": txn_rows,
        "flagged": flagged,
        "flags_by_txn": flags_by_txn,
        "summary": {
            "scanned": len(txn_rows),
            "flagged_count": len(flagged),
            "flagged_rows": len(flags_by_txn),
            "duplicate_groups": len(dup_partners),
        },
    }

    # Only cache once the LLM wrong_category checks landed (>=12 flags),
    # so a rate-limited partial run doesn't become the permanent demo result.
    if len(flagged) >= 12:
        try:
            import json as _json
            await redis.set(cache_key, _json.dumps(payload), ex=86400)
        except Exception:
            pass

    return payload
