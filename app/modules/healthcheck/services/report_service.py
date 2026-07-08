"""Health-check CSV report.

Turns one company's open issues into a downloadable CSV, grouped by check so an
accountant can work through them in Excel. Reads the same ``health_check_result``
rows the trapped feed shows; the router scopes to a single company
(``get_current_company_id`` enforces firm / assignment access), so this service
only ever sees one tenant's data — no cross-company mixing.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.healthcheck.checks.base import collect_issue_labels
from app.modules.healthcheck.models import Company
from app.modules.healthcheck.repository import HealthCheckResultRepository

_COLUMNS = (
    "Check", "Severity", "Contact", "Transaction",
    "Date", "Amount", "Status", "Reason", "Confidence",
)
# One company's full open-issue set — well above any real audit, but bounds the query.
_REPORT_LIMIT = 5000


class HealthCheckReportService:
    """Builds the per-company issue CSV: one company in, CSV text + filename out."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._repo = HealthCheckResultRepository(db)
        self._labels = collect_issue_labels()

    async def build_csv(self, company_id: UUID) -> tuple[str, str]:
        company = await self._db.get(Company, company_id)
        rows = await self._repo.list_post_ledger_trapped(company_id, limit=_REPORT_LIMIT)
        records = self._flatten(rows)
        # Group by check (then contact) so same-type issues sit together in Excel.
        records.sort(key=lambda r: (r["Check"], r["Contact"]))

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
        return buf.getvalue(), self._filename(company.name if company else None)

    def _flatten(self, rows: list[Any]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for row in rows:
            result = row.result or {}
            for flag in result.get("flagged") or []:
                out.append(self._row(result, flag))
        return out

    def _row(self, result: dict[str, Any], flag: dict[str, Any]) -> dict[str, str]:
        issue_type = str(flag.get("issue_type") or "")
        return {
            "Check": self._labels.get(issue_type) or issue_type.replace("_", " ").title(),
            "Severity": str(flag.get("severity") or "").title(),
            "Contact": str(result.get("vendor_name") or ""),
            "Transaction": str(
                result.get("invoice_number")
                or result.get("reference")
                or result.get("display_number")
                or ""
            ),
            "Date": str(result.get("invoice_date") or ""),
            "Amount": self._amount(result),
            "Status": str(result.get("payment_status") or result.get("invoice_status") or ""),
            "Reason": str(flag.get("message") or ""),
            "Confidence": self._confidence(flag.get("confidence")),
        }

    @staticmethod
    def _amount(result: dict[str, Any]) -> str:
        amount = result.get("amount")
        if amount in (None, ""):
            return ""
        currency = str(result.get("currency_code") or "GBP").strip().upper()
        symbol = "£" if currency == "GBP" else f"{currency} "
        try:
            return f"{symbol}{float(amount):,.2f}"
        except (TypeError, ValueError):
            return f"{symbol}{amount}"

    @staticmethod
    def _confidence(value: Any) -> str:
        try:
            return f"{round(float(value) * 100)}%"
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _filename(name: Optional[str]) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", (name or "company").strip()).strip("_")
        return f"{slug or 'company'}_Health_Check_Report.csv"
