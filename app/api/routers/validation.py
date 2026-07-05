"""Pre-ledger firewall: single-invoice validation + categorisation."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.schemas.transaction import InvoicePayload, InvoiceValidationResponse
from app.modules.healthcheck.engine import validate_invoice as run_validate_invoice

logger = logging.getLogger("uvicorn.error")

router = APIRouter(tags=["validation"])


@router.post(
    "/validate-invoice",
    response_model=InvoiceValidationResponse,
    status_code=status.HTTP_200_OK,
    summary="Pre-ledger firewall: validate and categorize a single invoice.",
)
async def validate_invoice(payload: InvoicePayload) -> InvoiceValidationResponse:
    try:
        return await run_validate_invoice(payload)
    except Exception as exc:
        logger.exception("validate_invoice endpoint failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Invoice validation failed.",
        ) from exc
