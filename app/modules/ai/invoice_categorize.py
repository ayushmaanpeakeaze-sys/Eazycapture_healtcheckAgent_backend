"""Groq invoice categorisation for the pre-ledger firewall."""
from __future__ import annotations

import logging
from typing import Optional

from app.core.config import settings
from app.modules.ai.client import get_groq
from app.schemas.transaction import InvoicePayload
from app.modules.healthcheck.engine.shared import _parse_json_object

logger = logging.getLogger("uvicorn.error")

_INVOICE_SYSTEM_PROMPT = (
    "You are a UK bookkeeping reviewer categorizing one invoice for a "
    "small-business ledger. Be concise and opinionated; no hedging. "
    "Return ONLY a JSON object with exactly these keys: "
    '{"suggested_category": string, "confidence_score": number between 0 and 1, '
    '"reasoning": string}. '
    "Rules: reasoning <= 140 chars; lead with the fact, not 'the transaction'; "
    "strip 'Ltd/Inc/LLC' from vendor names; no 'might/could/potentially'. "
    "No prose, no markdown fences."
)


async def classify_invoice(
    payload: InvoicePayload,
) -> tuple[Optional[str], float, str]:
    user_prompt = (
        f"Vendor: {payload.vendor_name}\n"
        f"Description: {payload.description}\n"
        f"Amount: {payload.amount}\n"
        f"Date: {payload.date.isoformat()}\n"
        f"Tax code: {payload.tax_code or 'MISSING'}\n"
        "Suggest the most appropriate accounting category."
    )
    try:
        client = get_groq()
        completion = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            max_tokens=400,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _INVOICE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = completion.choices[0].message.content or ""
        data = _parse_json_object(raw)
        return (
            data.get("suggested_category"),
            float(data.get("confidence_score", 0.0)),
            str(data.get("reasoning", "")),
        )
    except Exception:
        logger.exception("Groq invoice classification failed")
        return (
            None,
            0.0,
            "LLM unavailable; manual review required.",
        )
