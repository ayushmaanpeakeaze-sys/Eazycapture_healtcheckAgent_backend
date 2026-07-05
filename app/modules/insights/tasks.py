"""Celery tasks that refresh the Insights snapshots.

* ``insights.refresh_company`` — recompute one company's snapshot (used by the
  manual "Refresh" button and by the nightly loop).
* ``insights.refresh_all``     — nightly: enumerate connected companies and
  dispatch a per-company refresh, STAGGERED so we never burst Xero's
  rate limits (each company is only 3 calls, but many companies together must
  be spread out).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.celery_app import celery_app
from app.core.db import SyncSessionLocal
from app.modules.healthcheck.models import AuditBatch, Company, HealthCheckResult
from app.modules.healthcheck.services.panorama_service import _compute_health_score
from app.modules.insights.models import ClientInsightSnapshot
from app.modules.insights.logic.snapshot import compute_company_snapshot

logger = logging.getLogger("eazycapture.insights.tasks")


def _bookkeeping_health(db, company_id: UUID) -> dict:
    """Bookkeeping Health KPI from stored audit data (not a Xero report).

    Reuses the panorama score formula so it matches /health/stats. Denominator
    is the broadest completed audit (documents + contacts); numerator is the
    open (not resolved/dismissed) blocked findings.
    """
    audited_docs = db.execute(
        select(func.max(AuditBatch.total)).where(
            AuditBatch.company_id == company_id, AuditBatch.status == "completed",
        )
    ).scalar() or 0
    audited_contacts = db.execute(
        select(func.max(AuditBatch.contacts_total)).where(
            AuditBatch.company_id == company_id, AuditBatch.status == "completed",
        )
    ).scalar() or 0
    last_audit_at = db.execute(
        select(func.max(AuditBatch.completed_at)).where(
            AuditBatch.company_id == company_id, AuditBatch.status == "completed",
        )
    ).scalar()

    rows = db.execute(
        select(HealthCheckResult.result).where(
            HealthCheckResult.company_id == company_id,
            HealthCheckResult.kind == "post_ledger",
            HealthCheckResult.status == "blocked",
        )
    ).all()
    open_issues = sum(
        1 for (res,) in rows
        if not (res or {}).get("resolved") and not (res or {}).get("dismissed")
    )

    return {
        "health_score": _compute_health_score(audited_docs + audited_contacts, open_issues),
        "open_issues": open_issues,
        "audited_documents": int(audited_docs),
        "audited_contacts": int(audited_contacts),
        "last_audit_at": last_audit_at.isoformat() if last_audit_at else None,
    }

# Seconds between each company's refresh dispatch — throttles the nightly run so
# we don't fire every company's Xero calls at once.
_STAGGER_SECONDS = 4


def _do_refresh_company_snapshot(company_id: str) -> dict:
    """Fetch + compute + upsert one company's Insights snapshot."""
    with SyncSessionLocal() as db:
        company = db.get(Company, UUID(company_id))
        if company is None:
            return {"company_id": company_id, "status": "skipped", "reason": "not found"}
        if not (company.nango_connection_id and company.xero_tenant_id):
            return {"company_id": company_id, "status": "skipped", "reason": "not connected"}

        try:
            snap = asyncio.run(
                compute_company_snapshot(
                    company.nango_connection_id, company.xero_tenant_id,
                    sales_target_config=(company.audit_config or {}).get("sales_target"),
                    cash_health_config=(company.audit_config or {}).get("cash_health"),
                )
            )
            # 9th KPI — Bookkeeping Health from stored audit data (DB, not Xero).
            bh = _bookkeeping_health(db, company.id)
            snap["payload"]["bookkeeping_health"] = bh
            # Snapshot the health score over time so the Alerts feed can detect a
            # REAL drop ("60% -> 2%") instead of just the current low number.
            _hs = bh.get("health_score")
            if _hs is not None:
                from app.modules.healthcheck.models import ScoreHistory
                db.add(ScoreHistory(company_id=company.id, health_score=int(_hs)))
            values = {
                "company_id": company.id,
                "computed_at": datetime.now(timezone.utc),
                "status": "ok",
                "error": None,
                **{k: snap[k] for k in (
                    "net_profit", "tax_estimate", "cash", "cash_coverage",
                    "working_capital", "working_capital_healthy",
                    "distributable_reserves", "net_asset_value",
                    "dla_detected", "dla_overdrawn", "payload",
                )},
            }
        except Exception as exc:   # don't let one client kill the run
            logger.exception("[Insights] snapshot failed for company=%s", company_id)
            values = {
                "company_id": company.id,
                "computed_at": datetime.now(timezone.utc),
                "status": "failed",
                "error": str(exc)[:500],
            }

        stmt = pg_insert(ClientInsightSnapshot).values(**values)
        update_cols = {c: stmt.excluded[c] for c in values if c != "company_id"}
        stmt = stmt.on_conflict_do_update(
            index_elements=["company_id"], set_=update_cols,
        )
        db.execute(stmt)
        db.commit()
        return {"company_id": company_id, "status": values["status"]}


@celery_app.task(name="insights.refresh_company")
def refresh_company_snapshot(company_id: str) -> dict:
    """Recompute one company's snapshot, then always clear the
    ``insights:refreshing`` flag the Refresh endpoint set — so the page's spinner
    stops the instant this finishes (or errors), independent of the data-sync
    flag. The flag's TTL is only a crash safety net. Nightly refreshes never set
    the flag, so the delete is a harmless no-op for them."""
    try:
        return _do_refresh_company_snapshot(company_id)
    finally:
        try:
            import redis as _redis_sync
            from app.core.config import settings as _settings
            _redis_sync.from_url(
                _settings.REDIS_URL, decode_responses=True
            ).delete(f"insights:refreshing:{company_id}")
        except Exception:
            logger.warning(
                "[Insights] could not clear insights:refreshing flag for %s",
                company_id,
            )


@celery_app.task(name="insights.refresh_all")
def refresh_all_snapshots() -> dict:
    """Nightly: dispatch a staggered per-company refresh for every connected org."""
    with SyncSessionLocal() as db:
        rows = db.execute(
            select(Company.id).where(
                Company.is_active.is_(True),
                Company.nango_connection_id.isnot(None),
                Company.xero_tenant_id.isnot(None),
            )
        ).all()
    company_ids = [str(r[0]) for r in rows]
    for i, cid in enumerate(company_ids):
        refresh_company_snapshot.apply_async(args=[cid], countdown=i * _STAGGER_SECONDS)
    logger.info("[Insights] dispatched %d snapshot refreshes", len(company_ids))
    return {"dispatched": len(company_ids)}
