"""SyncEngine — full + incremental Xero sync into the DB.

One generic loop drives every entity (config in ``ENTITY_SPECS``):

  full sync       (watermark NULL) → no If-Modified-Since → pull everything
  incremental     (watermark set)  → If-Modified-Since = watermark − overlap →
                                     only changed records

Each page is upserted then committed immediately (page → DB → forget), so a
12 000-row entity never sits in memory and the action's 2 MB response cap is a
non-issue. The watermark (max ``UpdatedDateUTC`` seen) is advanced once the
entity finishes; a mid-run crash just re-pulls from the old watermark next time
(upsert is idempotent).

Reads run through the deployed custom ACTIONS — they honour If-Modified-Since
(the Nango proxy strips it). Small / watermark-less entities (tax rates,
payments, organisation) full-refresh via the proxy and prune deletions.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.modules.integrations.nango.client import NangoAuthError
from app.modules.integrations.nango.service import NangoService
from app.modules.integrations.sync.models import (
    SYNC_ENTITIES,
    XeroDocument,
    XeroSyncState,
)

logger = logging.getLogger("uvicorn.error")

# Safety backstop so a misbehaving connection can't page forever (1000 pages ×
# 100 = 100k records — far beyond any real org; the loop stops on the first
# empty page well before this).
MAX_SYNC_PAGES = 1000
# Re-ask for a small window before the watermark so a record updated in the
# same second as the last sync isn't missed (Xero truncates If-Modified-Since
# to seconds). Upsert is idempotent, so re-seeing a row is harmless.
WATERMARK_OVERLAP = timedelta(seconds=60)
# Upsert batch size (rows per INSERT … ON CONFLICT).
_UPSERT_CHUNK = 500

_MS_DATE_RE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")


def parse_xero_datetime(value: Any) -> Optional[datetime]:
    """Parse Xero's ``UpdatedDateUTC`` → tz-aware UTC datetime.

    Xero's Accounting API returns MS-AJAX ``/Date(1229650679057+0000)/``; some
    paths return ISO-8601. Handles both, returns None on anything unparseable.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value)
    m = _MS_DATE_RE.search(s)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000.0, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_if_modified_since(dt: datetime) -> str:
    """Watermark datetime → the string Xero's If-Modified-Since expects
    (UTC, ``YYYY-MM-DDTHH:MM:SS``)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# A page fetcher: (nango, connection_id, tenant_id, page, since_str) -> rows.
PageFetcher = Callable[
    [NangoService, str, str, int, Optional[str]], Awaitable[list[dict[str, Any]]]
]


@dataclass(frozen=True)
class EntitySpec:
    entity: str
    mode: str  # "incremental" | "full"
    id_field: str  # Xero's native id (or natural key) on each raw record
    fetch_page: PageFetcher
    paginates: bool = True  # False → single call, no page loop


@dataclass
class SyncResult:
    entity: str
    status: str = "ok"
    records: int = 0
    mode: str = ""           # "full" | "incremental"
    since: Optional[str] = None
    watermark: Optional[datetime] = None
    error: Optional[str] = None
    auth_error: bool = False


# --- page fetchers -------------------------------------------------------

def _inc_fb(action_name: str, proxy_name: str, *, paged: bool = True) -> PageFetcher:
    """Action-first fetcher with a proxy fallback. Normally the deployed
    ``list-*-full`` action serves each page (honouring If-Modified-Since); if the
    action returns nothing on page 1 — e.g. it is disabled on Nango — the same
    data is pulled through the proxy instead, so the sync keeps working. ``paged``
    proxies are looped page-by-page; single-call proxies return everything at once.
    """
    async def _f(nango, conn, tenant, page, since):
        rows = await getattr(nango, action_name)(conn, tenant_id=tenant, page=page, modified_since=since)
        if rows:
            return rows
        if page != 1:
            return []  # action empty on a later page = end (page 1 already did the fallback)
        proxy = getattr(nango, proxy_name)
        try:
            if not paged:
                return await proxy(conn, tenant)
            out: list = []
            p = 1
            while p <= 200:  # safety cap (~20k records at 100/page)
                batch = await proxy(conn, tenant, p)
                if not batch:
                    break
                out.extend(batch)
                p += 1
            return out
        except Exception:
            return []
    return _f


async def _fetch_tax_rates(nango, conn, tenant, page, since):
    if page != 1:
        return []
    rows = await nango.action_list_tax_rates(conn, tenant_id=tenant)
    if rows:
        return rows
    try:
        return await nango.fetch_xero_tax_rates(conn, tenant)  # proxy fallback
    except Exception:
        return []  # dead/expired connection → empty, like the action-only entities


async def _fetch_payments(nango, conn, tenant, page, since):
    rows = await nango.action_list_payments(conn, tenant_id=tenant, page=page)
    if rows or page > 1:
        return rows
    try:
        return await nango.fetch_xero_payments_page(conn, tenant, page)  # proxy fallback (page 1)
    except Exception:
        return []


async def _fetch_org(nango, conn, tenant, page, since):
    if page != 1:
        return []
    rows = await nango.action_list_organisation(conn, tenant_id=tenant)
    if rows:
        return rows
    try:
        org = await nango.fetch_xero_organisation(conn, tenant)  # proxy fallback
    except Exception:
        return []
    return [org] if isinstance(org, dict) and org else []


# Journals + Assets (SOP) need extra Xero scopes (accounting.journals.read /
# assets.read) that a connection may not have yet. Both action AND proxy are
# wrapped so a missing scope just yields 0 records — it never surfaces an auth
# error that would falsely mark an otherwise-healthy connection needs_reconnect.
async def _fetch_journals(nango, conn, tenant, page, since):
    try:
        rows = await nango.action_list_journals(conn, tenant_id=tenant, page=page, modified_since=since)
    except Exception:
        rows = []
    if rows or page > 1:
        return rows
    try:
        return await nango.fetch_xero_journals_page(conn, tenant, page)  # proxy fallback (page 1)
    except Exception:
        return []  # journals.read scope not granted yet → 0 records


async def _fetch_assets(nango, conn, tenant, page, since):
    try:
        rows = await nango.action_list_assets(conn, tenant_id=tenant, page=page)
    except Exception:
        rows = []
    if rows or page > 1:
        return rows
    try:
        return await nango.fetch_xero_assets_page(conn, tenant, page)  # proxy fallback (page 1)
    except Exception:
        return []  # assets.read scope not granted yet → 0 records


# The ten mirrored entities. First five are incremental (actions honour the
# watermark); the rest are small / watermark-less full refreshes. EVERY entity is
# action-first with a proxy fallback, so a disabled or failing action (e.g. a
# Nango plan limit) transparently drops to the proxy and the sync keeps pulling
# data. journal + asset (SOP) need extra scopes → 0 records until granted.
ENTITY_SPECS: dict[str, EntitySpec] = {
    "invoice": EntitySpec(
        "invoice", "incremental", "InvoiceID",
        _inc_fb("action_list_invoices_full", "fetch_xero_invoices_page")),
    "bank_transaction": EntitySpec(
        "bank_transaction", "incremental", "BankTransactionID",
        _inc_fb("action_list_bank_transactions_full", "fetch_xero_bank_transactions_page")),
    "credit_note": EntitySpec(
        "credit_note", "incremental", "CreditNoteID",
        _inc_fb("action_list_credit_notes_full", "fetch_xero_credit_notes_page")),
    "contact": EntitySpec(
        "contact", "incremental", "ContactID",
        _inc_fb("action_list_contacts_full", "fetch_xero_contacts", paged=False)),
    "account": EntitySpec(
        "account", "incremental", "AccountID",
        _inc_fb("action_list_accounts_full", "fetch_xero_accounts", paged=False), paginates=False),
    "tax_rate": EntitySpec(
        "tax_rate", "full", "TaxType", _fetch_tax_rates, paginates=False),
    "payment": EntitySpec(
        "payment", "full", "PaymentID", _fetch_payments),
    "organisation": EntitySpec(
        "organisation", "full", "OrganisationID", _fetch_org, paginates=False),
    "journal": EntitySpec(
        "journal", "full", "JournalID", _fetch_journals),
    "asset": EntitySpec(
        "asset", "full", "assetId", _fetch_assets),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _chunks(seq: list[Any], size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class SyncEngine:
    """Owns the sync loop. One instance per process is fine (stateless besides
    the Nango client)."""

    def __init__(self, nango: Optional[NangoService] = None) -> None:
        self._nango = nango or NangoService()

    async def _get_or_create_state(
        self, db: AsyncSession, company_id: uuid.UUID, entity: str
    ) -> XeroSyncState:
        state = (
            await db.execute(
                select(XeroSyncState).where(
                    XeroSyncState.company_id == company_id,
                    XeroSyncState.entity == entity,
                )
            )
        ).scalar_one_or_none()
        if state is None:
            state = XeroSyncState(
                id=uuid.uuid4(), company_id=company_id, entity=entity,
            )
            db.add(state)
            await db.flush()
        return state

    async def _upsert_page(
        self, db: AsyncSession, rows: list[dict[str, Any]]
    ) -> None:
        for chunk in _chunks(rows, _UPSERT_CHUNK):
            stmt = pg_insert(XeroDocument).values(chunk)
            stmt = stmt.on_conflict_do_update(
                # Target the unique INDEX by its columns (the constraint is a
                # unique index in the DB, so ON CONFLICT (cols) is the form that
                # matches — ON CONFLICT ON CONSTRAINT needs a real constraint).
                index_elements=["company_id", "entity", "xero_id"],
                set_={
                    "raw_json": stmt.excluded.raw_json,
                    "updated_date_utc": stmt.excluded.updated_date_utc,
                    "synced_at": func.now(),
                },
            )
            await db.execute(stmt)

    async def sync_entity(
        self,
        db: AsyncSession,
        company,
        entity: str,
        *,
        force_full: bool = False,
    ) -> SyncResult:
        """Full or incremental sync of ONE entity. Commits per page (so progress
        survives a crash) and advances the watermark at the end. Never raises —
        failures land in ``SyncResult.error`` and the entity's state row."""
        spec = ENTITY_SPECS[entity]
        conn = company.nango_connection_id
        tenant = company.xero_tenant_id
        if not conn or not tenant:
            return SyncResult(entity, status="error", error="company not connected")

        state = await self._get_or_create_state(db, company.id, entity)
        state.last_status = "in_progress"
        await db.commit()

        is_incremental = (
            spec.mode == "incremental"
            and state.watermark_utc is not None
            and not force_full
        )
        since_str: Optional[str] = None
        if is_incremental:
            since_str = format_if_modified_since(
                state.watermark_utc - WATERMARK_OVERLAP
            )
        max_updated: Optional[datetime] = state.watermark_utc if is_incremental else None
        seen_ids: set[str] = set()
        total = 0
        started = _utcnow()

        try:
            page = 1
            while page <= MAX_SYNC_PAGES:
                rows = await spec.fetch_page(self._nango, conn, tenant, page, since_str)
                if not rows:
                    break
                batch: list[dict[str, Any]] = []
                for raw in rows:
                    if not isinstance(raw, dict):
                        continue
                    xid = str(raw.get(spec.id_field) or "").strip()
                    if not xid:
                        continue
                    upd = parse_xero_datetime(raw.get("UpdatedDateUTC"))
                    batch.append({
                        "id": uuid.uuid4(),
                        "company_id": company.id,
                        "entity": entity,
                        "xero_id": xid[:64],
                        "raw_json": raw,
                        "updated_date_utc": upd,
                    })
                    seen_ids.add(xid[:64])
                    if upd and (max_updated is None or upd > max_updated):
                        max_updated = upd
                if batch:
                    await self._upsert_page(db, batch)
                    await db.commit()
                    total += len(batch)
                if not spec.paginates:
                    break
                page += 1

            # Full-refresh entities own the WHOLE set each run → prune anything
            # Xero no longer returns (handles deletions). Incremental entities
            # must NOT prune (a page of "only changed" rows isn't the full set).
            if spec.mode == "full":
                prune = delete(XeroDocument).where(
                    XeroDocument.company_id == company.id,
                    XeroDocument.entity == entity,
                )
                if seen_ids:
                    prune = prune.where(XeroDocument.xero_id.notin_(seen_ids))
                await db.execute(prune)
                await db.commit()

            if spec.mode == "incremental" and max_updated is not None:
                state.watermark_utc = max_updated
            state.last_sync_at = started
            if not is_incremental:
                state.last_full_sync_at = started
            state.last_status = "ok"
            state.last_error = None
            state.last_record_count = total
            await db.commit()
            logger.info(
                "[Sync] company=%s entity=%s mode=%s records=%d since=%s watermark=%s",
                company.id, entity, "incremental" if is_incremental else "full",
                total, since_str, max_updated,
            )
            return SyncResult(
                entity, status="ok", records=total,
                mode="incremental" if is_incremental else "full",
                since=since_str, watermark=max_updated,
            )
        except Exception as exc:  # noqa: BLE001 — record + continue, never abort
            await db.rollback()
            state = await self._get_or_create_state(db, company.id, entity)
            state.last_status = "error"
            state.last_error = str(exc)[:500]
            state.last_sync_at = started
            await db.commit()
            logger.exception(
                "[Sync] FAILED company=%s entity=%s", company.id, entity)
            return SyncResult(
                entity, status="error", error=str(exc),
                auth_error=isinstance(exc, NangoAuthError),
            )

    async def sync_company(
        self,
        db: AsyncSession,
        company,
        *,
        entities: Optional[list[str]] = None,
        force_full: bool = False,
    ) -> dict[str, SyncResult]:
        """Sync every (or a subset of) entity for one company.

        Entities run CONCURRENTLY — each on its OWN DB session — so the
        wall-clock is the slowest single entity, not the sum of all eight
        (~3-4x faster on a real org). Concurrent commits on one AsyncSession
        aren't allowed, and a session-per-entity also keeps each entity's
        transaction (and watermark) isolated. Distinct entities never touch the
        same ``xero_sync_state`` / ``xero_document`` rows, so there's no
        cross-entity contention. Per-entity isolation: one entity failing (or
        crashing) never aborts the others. ``db`` is intentionally unused here —
        each entity opens a fresh session.
        """
        targets = [
            e for e in (entities or list(SYNC_ENTITIES)) if e in ENTITY_SPECS
        ]

        async def _sync_one(entity: str) -> SyncResult:
            async with AsyncSessionLocal() as entity_db:
                return await self.sync_entity(
                    entity_db, company, entity, force_full=force_full,
                )

        gathered = await asyncio.gather(
            *(_sync_one(e) for e in targets), return_exceptions=True,
        )
        results: dict[str, SyncResult] = {}
        for entity, res in zip(targets, gathered):
            if isinstance(res, BaseException):
                logger.exception(
                    "[Sync] entity=%s crashed", entity, exc_info=res)
                results[entity] = SyncResult(
                    entity, status="error", error=str(res))
            else:
                results[entity] = res

        ok = sum(1 for r in results.values() if r.status == "ok")
        records = sum(r.records for r in results.values())
        logger.info(
            "[Sync] company=%s done: %d/%d entities ok, %d records",
            company.id, ok, len(results), records,
        )
        await self._update_connection_health(company.id, results)
        return results

    async def _update_connection_health(
        self, company_id, results: dict[str, SyncResult],
    ) -> None:
        if any(r.auth_error for r in results.values()):
            target = True
        elif any(r.status == "ok" for r in results.values()):
            target = False
        else:
            return
        from app.modules.healthcheck.models import Company
        async with AsyncSessionLocal() as db:
            company = await db.get(Company, company_id)
            if company is not None and company.needs_reconnect != target:
                company.needs_reconnect = target
                await db.commit()


__all__ = [
    "SyncEngine",
    "SyncResult",
    "EntitySpec",
    "ENTITY_SPECS",
    "parse_xero_datetime",
    "format_if_modified_since",
]
