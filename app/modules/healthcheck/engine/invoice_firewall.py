"""Pre-ledger firewall — validate a single invoice before it hits Xero.

Deterministic field checks + a Groq categorisation (``modules.ai``) when the
data is ambiguous (missing tax code or generic vendor name).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.modules.ai.invoice_categorize import classify_invoice
from app.shared.transaction import InvoicePayload, InvoiceValidationResponse
from app.modules.healthcheck.engine.shared import _LLM_MIN_CONFIDENCE


async def validate_invoice(payload: InvoicePayload) -> InvoiceValidationResponse:
    """Deterministic rules + Groq categorization for a single invoice."""
    validation_errors = _run_invoice_rules(payload)

    needs_llm = not payload.tax_code or _is_ambiguous_vendor(payload.vendor_name)

    if needs_llm:
        category, confidence, reasoning = await classify_invoice(payload)
        if confidence < _LLM_MIN_CONFIDENCE:
            category = None
    else:
        category, confidence, reasoning = (
            None,
            1.0,
            "Vendor and tax code present; deterministic rules sufficient.",
        )

    return InvoiceValidationResponse(
        suggested_category=category,
        confidence_score=confidence,
        reasoning=reasoning,
        validation_errors=validation_errors,
    )


def _run_invoice_rules(payload: InvoicePayload) -> list[str]:
    errors: list[str] = []
    if not payload.invoice_number:
        errors.append("invoice_number is missing.")
    if not payload.tax_code:
        errors.append("tax_code is missing.")
    if payload.amount <= Decimal("0"):
        errors.append("amount must be greater than zero.")
    if not payload.vendor_name.strip():
        errors.append("vendor_name is blank.")
    if payload.date > date.today():
        errors.append("date is in the future.")
    return errors


def _is_ambiguous_vendor(vendor_name: str) -> bool:
    cleaned = vendor_name.strip().lower()
    if len(cleaned) < 4:
        return True
    return cleaned in {"misc", "miscellaneous", "n/a", "unknown", "vendor"}
