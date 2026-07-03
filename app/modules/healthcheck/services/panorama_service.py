"""Panorama (all-companies dashboard) + per-company health summary.

Both views derive from the same primitives:

* ``audit_batch`` — latest run per company gives ``post_audited_total``
  + ``last_audit_at``.
* ``health_check_result`` — current trapped rows give ``trapped_count``
  and the ``top_issue`` / ``top_issues`` breakdown; historical rows
  within the window give ``resolved_count`` / ``dismissed_count``.

Health-score formula (single source of truth — change here only):

    ``score = round(100 * (audited - trapped) / audited)`` clamped to
    [0, 100].

When the company hasn't been audited yet, ``health_score`` is ``None``
(distinct from "0" so the UI can show a "never audited" state instead
of an alarming red).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.healthcheck.models import (
    AuditBatch,
    Company,
    HealthCheckResult,
)
from app.modules.healthcheck.schemas import (
    CompaniesPanoramaResponse,
    CompanyHealthRow,
    HealthSummaryIssue,
    HealthSummaryResponse,
)

logger = logging.getLogger("eazycapture.panorama")

# Resolution / dismissal flags inside the ``result`` JSONB.
_FLAG_RESOLVED = "resolved"
_FLAG_DISMISSED = "dismissed"


def _compute_health_score(
    audited: int,
    trapped: int,
) -> Optional[int]:
    """Single source of truth for the score formula — BLENDED across both
    pools (documents + contacts).

    ``audited`` = documents audited + contacts audited.
    ``trapped`` = document issues + contact issues.
    So a clean document AND a clean contact both count toward the score,
    and every fixable issue (whichever pool) drags it down.

    ``None`` when nothing has been audited (UI shows 'no data' vs 'score 0')."""
    if audited <= 0:
        return None
    raw = 100.0 * (audited - trapped) / audited
    return max(0, min(100, round(raw)))


class CompaniesPanoramaService:
    """Multi-tenant dashboard + per-company summary."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Panorama (all companies)
    # ------------------------------------------------------------------

    async def get_panorama(
        self,
        days: int = 30,
        allowed_company_ids: list[UUID] | None = None,
    ) -> CompaniesPanoramaResponse:
        """Cross-company dashboard.

        ``allowed_company_ids`` scopes the result for team members in
        "selected" mode — they only see the companies assigned to them.
        ``None`` means no restriction (admin / all-mode team member).
        An empty list means the user has no assigned companies → no rows.
        """
        days = max(1, min(365, days))

        stmt = (
            select(Company)
            .where(Company.is_active.is_(True))
            .order_by(Company.created_at.asc())
        )
        if allowed_company_ids is not None:
            if not allowed_company_ids:
                return CompaniesPanoramaResponse(
                    results=[], total=0, window_days=days,
                    generated_at=datetime.now(timezone.utc),
                )
            stmt = stmt.where(Company.id.in_(allowed_company_ids))

        companies = (await self._db.execute(stmt)).scalars().all()

        rows: list[CompanyHealthRow] = []
        for company in companies:
            last_audit = await self._latest_audit_batch(company.id)
            trapped_count, doc_count, _contact_count, top_issue = (
                await self._trapped_stats(company.id)
            )

            # Denominator = broadest recent audit (not the last, possibly
            # period-scoped, run). Health score is BLENDED — documents AND
            # contacts both count, so every fixable issue drags it down.
            post_audited_total = await self._max_audited_total(company.id)
            contacts_audited = await self._contacts_audited(company.id)
            last_audit_at = last_audit.completed_at if last_audit else None
            health_score = _compute_health_score(
                post_audited_total + contacts_audited,
                doc_count + _contact_count,
            )

            rows.append(CompanyHealthRow(
                company_id=company.id,
                name=company.name,
                is_active=company.is_active,
                needs_reconnect=company.needs_reconnect,
                nango_connection_id=company.nango_connection_id,
                xero_tenant_id=company.xero_tenant_id,
                health_score=health_score,
                trapped_count=trapped_count,
                post_audited_total=post_audited_total,
                # Contact denominator + the doc/contact trapped split — all
                # already computed above for the blended score. AUDITED column
                # = post_audited_total + audited_contacts (62 + 51 = 113).
                audited_contacts=contacts_audited,
                open_document_issues=doc_count,
                open_contact_issues=_contact_count,
                last_audit_at=last_audit_at,
                top_issue=top_issue,
            ))

        rows.sort(key=_panorama_sort_key)
        return CompaniesPanoramaResponse(
            results=rows,
            total=len(rows),
            window_days=days,
            generated_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Per-company summary
    # ------------------------------------------------------------------

    async def get_company_summary(
        self,
        company_id: UUID,
        days: int = 30,
        top_n_issues: int = 5,
    ) -> HealthSummaryResponse:
        days = max(1, min(365, days))
        top_n_issues = max(1, min(20, top_n_issues))
        window_start = datetime.now(timezone.utc) - timedelta(days=days)

        last_audit = await self._latest_audit_batch(company_id)
        # Broadest recent audit as the denominator (not the last period run).
        post_audited_total = await self._max_audited_total(company_id)
        last_audit_at = last_audit.completed_at if last_audit else None

        trapped_rows = (
            await self._db.execute(
                select(HealthCheckResult)
                .where(
                    HealthCheckResult.company_id == company_id,
                    HealthCheckResult.kind == "post_ledger",
                    HealthCheckResult.status == "blocked",
                    ~HealthCheckResult.result.contains({_FLAG_RESOLVED: True}),
                    ~HealthCheckResult.result.contains({_FLAG_DISMISSED: True}),
                    ~HealthCheckResult.result.contains({"marked_ok": True}),
                    ~HealthCheckResult.result.contains({"auto_cleared": True}),
                )
                # Stable order so ``_top_issues`` picks a deterministic
                # sample_msg (the earliest-recorded row for each rule), rather
                # than relying on Postgres heap order.
                .order_by(HealthCheckResult.ran_at.asc(), HealthCheckResult.id.asc())
            )
        ).scalars().all()
        trapped_count = len(trapped_rows)

        resolved_count = await self._count_with_flag(
            company_id, _FLAG_RESOLVED, since=window_start,
        )
        dismissed_count = await self._count_with_flag(
            company_id, _FLAG_DISMISSED, since=window_start,
        )

        top_issues = _top_issues(trapped_rows, top_n_issues)
        # Blended score: documents + contacts both count.
        contacts_audited = await self._contacts_audited(company_id)
        health_score = _compute_health_score(
            post_audited_total + contacts_audited,
            trapped_count,
        )

        return HealthSummaryResponse(
            company_id=company_id,
            health_score=health_score,
            window_days=days,
            post_audited_total=post_audited_total,
            trapped_count=trapped_count,
            resolved_count=resolved_count,
            dismissed_count=dismissed_count,
            last_audit_at=last_audit_at,
            top_issues=top_issues,
        )

    # ------------------------------------------------------------------
    # Internal queries
    # ------------------------------------------------------------------

    async def _latest_audit_batch(
        self,
        company_id: UUID,
    ) -> Optional[AuditBatch]:
        stmt = (
            select(AuditBatch)
            .where(
                AuditBatch.company_id == company_id,
                AuditBatch.status == "completed",
            )
            .order_by(AuditBatch.completed_at.desc().nullslast(),
                      AuditBatch.started_at.desc())
            .limit(1)
        )
        return (await self._db.execute(stmt)).scalar_one_or_none()

    async def _trapped_stats(
        self,
        company_id: UUID,
    ) -> tuple[int, int, int, Optional[str]]:
        """Return (total, document_count, contact_count, top_rule).

        Documents and contacts are counted separately — 'documents trapped'
        should not include contact-hygiene issues (CONTACT rows).
        """
        rows = (
            await self._db.execute(
                select(
                    HealthCheckResult.result,
                    HealthCheckResult.document_type,
                )
                .where(
                    HealthCheckResult.company_id == company_id,
                    HealthCheckResult.kind == "post_ledger",
                    HealthCheckResult.status == "blocked",
                    ~HealthCheckResult.result.contains({_FLAG_RESOLVED: True}),
                    ~HealthCheckResult.result.contains({_FLAG_DISMISSED: True}),
                    ~HealthCheckResult.result.contains({"marked_ok": True}),
                    ~HealthCheckResult.result.contains({"auto_cleared": True}),
                )
            )
        ).all()
        trapped_count = len(rows)
        if trapped_count == 0:
            return 0, 0, 0, None
        rule_counter: Counter[str] = Counter()
        contact_count = 0
        for (result, doc_type) in rows:
            if (doc_type or "").upper() == "CONTACT":
                contact_count += 1
            rule_id = _primary_rule_id(result)
            if rule_id:
                rule_counter[rule_id] += 1
        document_count = trapped_count - contact_count
        top_rule = rule_counter.most_common(1)
        return trapped_count, document_count, contact_count, (
            top_rule[0][0] if top_rule else None
        )

    async def _max_audited_total(self, company_id: UUID) -> int:
        """Broadest recent completed audit total — the document denominator.
        MAX (not the last batch) so a period-scoped run (e.g. 'April' = 22
        docs) doesn't shrink it below a prior full sweep (62)."""
        val = (
            await self._db.execute(
                select(func.max(AuditBatch.total)).where(
                    AuditBatch.company_id == company_id,
                    AuditBatch.status == "completed",
                )
            )
        ).scalar_one_or_none()
        return int(val or 0)

    async def _contacts_audited(self, company_id: UUID) -> int:
        """Contacts audited in the broadest recent sweep — the contact
        denominator for the blended health score."""
        val = (
            await self._db.execute(
                select(func.max(AuditBatch.contacts_total)).where(
                    AuditBatch.company_id == company_id,
                    AuditBatch.status == "completed",
                )
            )
        ).scalar_one_or_none()
        return int(val or 0)

    async def _count_with_flag(
        self,
        company_id: UUID,
        flag: str,
        since: datetime,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(HealthCheckResult)
            .where(
                HealthCheckResult.company_id == company_id,
                HealthCheckResult.ran_at >= since,
                HealthCheckResult.result.contains({flag: True}),
            )
        )
        return int((await self._db.execute(stmt)).scalar_one())


# ----------------------- module-level helpers -----------------------

def _primary_rule_id(result: Any) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    rule_ids = result.get("rule_ids")
    if isinstance(rule_ids, list) and rule_ids:
        first = rule_ids[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    flagged = result.get("flagged")
    if isinstance(flagged, list) and flagged and isinstance(flagged[0], dict):
        return (
            flagged[0].get("rule_id")
            or flagged[0].get("issue_type")
        )
    return None


def _sample_msg(result: Any) -> Optional[str]:
    if not isinstance(result, dict):
        return None
    msg = result.get("messages")
    if isinstance(msg, str) and msg.strip():
        return msg.strip()[:160]
    flagged = result.get("flagged")
    if isinstance(flagged, list) and flagged and isinstance(flagged[0], dict):
        candidate = flagged[0].get("message")
        if isinstance(candidate, str):
            return candidate.strip()[:160]
    return None


def _top_issues(
    rows: list[HealthCheckResult],
    top_n: int,
) -> list[HealthSummaryIssue]:
    counter: Counter[str] = Counter()
    samples: dict[str, str] = {}
    for row in rows:
        rule_id = _primary_rule_id(row.result) or "unknown"
        counter[rule_id] += 1
        if rule_id not in samples:
            sample = _sample_msg(row.result)
            if sample:
                samples[rule_id] = sample
    return [
        HealthSummaryIssue(
            issue_type=rule_id,
            count=count,
            sample_msg=samples.get(rule_id),
        )
        for rule_id, count in counter.most_common(top_n)
    ]


def _panorama_sort_key(row: CompanyHealthRow) -> tuple:
    """Worst score first, nulls last, ties broken by name."""
    if row.health_score is None:
        return (1, 0, row.name.lower())
    return (0, row.health_score, row.name.lower())
