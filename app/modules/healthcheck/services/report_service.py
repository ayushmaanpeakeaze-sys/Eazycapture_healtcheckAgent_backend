"""Health-check CSV report.

Turns one company's open issues into a downloadable CSV, grouped into a section
per check so an accountant can work through them in Excel — each section carries
a headline (check name + count) and its own header row. Reads the same
``health_check_result`` rows the trapped feed shows; the router scopes to a
single company (``get_current_company_id`` enforces firm / assignment access), so
this service only ever sees one tenant's data — no cross-company mixing.
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.healthcheck.checks.base import collect_issue_labels
from app.modules.healthcheck.models import Company
from app.modules.healthcheck.repository import HealthCheckResultRepository

# Per-row columns. "Duplicate Of" + "Action" make a duplicate's partner and the
# keep/void call explicit instead of buried in the reason; the long "Reason" text
# sits last so the structured columns read first.
_COLUMNS = (
    "Contact", "Transaction", "Date", "Amount", "Status",
    "Duplicate Of", "Action", "Severity", "Confidence", "Reason",
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

        # Group by check so each becomes its own titled section.
        groups: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            result = row.result or {}
            for flag in result.get("flagged") or []:
                check, record = self._row(result, flag)
                groups[check].append(record)

        buf = io.StringIO()
        buf.write("﻿")  # UTF-8 BOM so Excel renders £ / symbols correctly
        writer = csv.writer(buf)
        for check in sorted(groups):
            items = sorted(groups[check], key=lambda r: r["Contact"])
            writer.writerow([f"{check} — {len(items)} issue(s)"])
            writer.writerow(_COLUMNS)
            for r in items:
                writer.writerow([r[c] for c in _COLUMNS])
            writer.writerow([])  # blank row between sections

        return buf.getvalue(), self._filename(company.name if company else None)

    def _row(self, result: dict[str, Any], flag: dict[str, Any]) -> tuple[str, dict[str, str]]:
        issue_type = str(flag.get("issue_type") or "")
        check = self._labels.get(issue_type) or issue_type.replace("_", " ").title()
        record = {
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
            "Duplicate Of": str(flag.get("duplicate_of_invoice_number") or ""),
            "Action": self._action(flag),
            "Severity": str(flag.get("severity") or "").title(),
            "Confidence": self._confidence(flag.get("confidence")),
            "Reason": str(flag.get("message") or ""),
        }
        return check, record

    @staticmethod
    def _action(flag: dict[str, Any]) -> str:
        original = flag.get("this_is_likely_original")
        if original is True:
            return "KEEP (original)"
        if original is False:
            return "VOID (duplicate)"
        return ""

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
