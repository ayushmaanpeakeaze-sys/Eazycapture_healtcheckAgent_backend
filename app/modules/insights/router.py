"""HTTP surface for the Insights module, mounted at ``/api/v1/insights``.

Serves PRE-COMPUTED snapshots (refreshed nightly + on demand) — not live Xero,
so it's fast and scalable. Three routes:

  GET  /firm-summary/           — roll-up across all the firm's clients (panorama)
  GET  /{company_id}/           — one client's full KPI snapshot (all 9 KPIs)
  POST /{company_id}/refresh/   — recompute one client now (background)

The heavy Xero fetches + KPI maths run in the Celery snapshot task
(``app.modules.insights.tasks``); this layer only reads the stored snapshot.
Multi-tenant: company routes gate on ``get_current_company_id``.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, get_current_user
from app.core.db import get_db
from app.core.multi_tenant import allowed_company_ids_for, get_current_company_id
from app.core.redis_client import get_redis
from app.modules.healthcheck.models import Company
from app.modules.insights.models import ClientInsightSnapshot
from app.modules.insights.schemas import (
    CashHealthSettingsModel,
    FirmClientRow,
    FirmSummaryResponse,
    RefreshResponse,
    SalesTargetConfigModel,
    SnapshotResponse,
)
from app.modules.insights.logic.cash_health.config import parse_config as parse_cash_health_config
from app.modules.insights.logic.sales_tracker.config import parse_config
from app.modules.insights.tasks import refresh_company_snapshot

_CASH_TIGHT = 0.2   # coverage below this = "cash tight" (firm rollup flag)

router = APIRouter(
    prefix="/api/v1/insights",
    tags=["insights"],
    dependencies=[Depends(get_current_user)],
)


@router.get(
    "/firm-summary/",
    response_model=FirmSummaryResponse,
    summary="Roll-up across all the firm's clients (from stored snapshots).",
)
async def firm_summary(
    user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    allowed = await allowed_company_ids_for(db, user)
    q = select(Company.id, Company.name).where(Company.is_active.is_(True))
    if allowed is not None:
        q = q.where(Company.id.in_(allowed))
    companies = {cid: name for cid, name in (await db.execute(q)).all()}

    snaps = (await db.execute(
        select(ClientInsightSnapshot).where(
            ClientInsightSnapshot.company_id.in_(list(companies.keys()) or [None])
        )
    )).scalars().all()
    by_company = {s.company_id: s for s in snaps}

    rows: list[FirmClientRow] = []
    in_profit = in_loss = cash_tight = wc_neg = dla_over = unrec_total = 0
    for cid, name in companies.items():
        s = by_company.get(cid)
        if s is None or s.status != "ok":
            rows.append(FirmClientRow(company_id=str(cid), name=name))
            continue
        np = float(s.net_profit) if s.net_profit is not None else None
        cov = float(s.cash_coverage) if s.cash_coverage is not None else None
        wc = float(s.working_capital) if s.working_capital is not None else None
        bank = (s.payload or {}).get("bank_reconciliation") or {}
        unrec = bank.get("unreconciled_count")
        if np is not None:
            in_profit += np >= 0
            in_loss += np < 0
        if cov is not None and cov < _CASH_TIGHT:
            cash_tight += 1
        if wc is not None and wc < 0:
            wc_neg += 1
        if s.dla_overdrawn:
            dla_over += 1
        if unrec:
            unrec_total += unrec
        rows.append(FirmClientRow(
            company_id=str(cid), name=name,
            computed_at=s.computed_at.isoformat() if s.computed_at else None,
            net_profit=np, working_capital=wc, cash_coverage=cov,
            dla_overdrawn=bool(s.dla_overdrawn),
            unreconciled_bank_items=unrec,
            last_bank_reconciled=bank.get("last_reconciled_date"),
            most_recent_transaction=bank.get("most_recent_transaction"),
        ))

    return {
        "totals": {
            "total_clients": len(companies),
            "with_snapshot": len(by_company),
            "in_profit": in_profit,
            "in_loss": in_loss,
            "cash_tight": cash_tight,
            "working_capital_negative": wc_neg,
            "dla_overdrawn": dla_over,
            "unreconciled_bank_items": unrec_total,
        },
        "clients": rows,
    }


@router.get(
    "/{company_id}/",
    response_model=SnapshotResponse,
    summary="Stored Insights snapshot for one client — all 9 KPIs (fast, no live Xero).",
)
async def get_snapshot(
    company_id: UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    refreshing = bool(await get_redis().exists(f"insights:refreshing:{company_id}"))
    snap = (await db.execute(
        select(ClientInsightSnapshot).where(
            ClientInsightSnapshot.company_id == company_id
        )
    )).scalars().first()
    if snap is None:
        return {
            "company_id": str(company_id), "computed_at": None,
            "status": "none", "stale": True, "refreshing": refreshing,
            "payload": {},
        }
    return {
        "company_id": str(company_id),
        "computed_at": snap.computed_at.isoformat() if snap.computed_at else None,
        "status": snap.status,
        "stale": snap.status != "ok",
        "refreshing": refreshing,
        "payload": snap.payload or {},
    }


@router.post(
    "/{company_id}/refresh/",
    response_model=RefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Recompute one client's snapshot now (live Xero, runs in background).",
)
async def refresh_snapshot(
    company_id: UUID = Depends(get_current_company_id),
) -> dict:
    # Short-lived flag on a dedicated key; the task clears it, the TTL is a
    # safety net. A second request while one is running is a no-op.
    key = f"insights:refreshing:{company_id}"
    started = await get_redis().set(key, "1", nx=True, ex=300)
    if not started:
        return {
            "company_id": str(company_id),
            "status": "already_refreshing",
            "refreshing": True,
        }
    refresh_company_snapshot.delay(str(company_id))
    return {"company_id": str(company_id), "status": "queued", "refreshing": True}


@router.get(
    "/{company_id}/sales-target/",
    response_model=SalesTargetConfigModel,
    summary="Get this client's Sales Tracker target settings.",
)
async def get_sales_target(
    company_id: UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    company = await db.get(Company, company_id)
    cfg = ((company.audit_config if company else None) or {}).get("sales_target") or {}
    return {
        "basis": cfg.get("basis", "average_3"),
        "adjustment_pct": cfg.get("adjustment_pct", 0.0),
        "manual_value": cfg.get("manual_value"),
    }


@router.put(
    "/{company_id}/sales-target/",
    response_model=SalesTargetConfigModel,
    summary="Set this client's Sales Tracker target — applied on the next snapshot refresh.",
)
async def set_sales_target(
    payload: SalesTargetConfigModel,
    company_id: UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Validate/sanitise through the same parser the tracker uses, then store the
    # config under the company's audit_config JSONB (the shared settings bucket).
    cfg = parse_config(payload.model_dump())
    company = await db.get(Company, company_id)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="company not found",
        )
    merged = dict(company.audit_config or {})
    merged["sales_target"] = {
        "basis": cfg.basis,
        "adjustment_pct": cfg.adjustment_pct,
        "manual_value": cfg.manual_value,
    }
    company.audit_config = merged  # reassign so SQLAlchemy flags the JSONB dirty
    await db.commit()
    return merged["sales_target"]


@router.get(
    "/{company_id}/cash-health-settings/",
    response_model=CashHealthSettingsModel,
    summary="Get this client's Cash Health Check settings.",
)
async def get_cash_health_settings(
    company_id: UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    company = await db.get(Company, company_id)
    cfg = ((company.audit_config if company else None) or {}).get("cash_health") or {}
    return {
        "included": cfg.get("included", {}),
        "overrides": cfg.get("overrides", {}),
        "account_overrides": cfg.get("account_overrides", {}),
        "disregarded_banks": cfg.get("disregarded_banks", []),
    }


@router.put(
    "/{company_id}/cash-health-settings/",
    response_model=CashHealthSettingsModel,
    summary="Set this client's Cash Health Check settings — applied on the next refresh.",
)
async def set_cash_health_settings(
    payload: CashHealthSettingsModel,
    company_id: UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Sanitise through the same parser the calculator uses, then store under the
    # company's audit_config JSONB (the shared settings bucket).
    cfg = parse_cash_health_config(payload.model_dump())
    company = await db.get(Company, company_id)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="company not found",
        )
    merged = dict(company.audit_config or {})
    merged["cash_health"] = {
        "included": cfg.included,
        "overrides": cfg.overrides,
        "account_overrides": cfg.account_overrides,
        "disregarded_banks": sorted(cfg.disregarded_banks),
    }
    company.audit_config = merged  # reassign so SQLAlchemy flags the JSONB dirty
    await db.commit()
    return merged["cash_health"]
