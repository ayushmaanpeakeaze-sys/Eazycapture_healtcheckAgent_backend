"""ORM models for the DB-backed Xero sync.

Two tables, both company-scoped (multi-tenant: every row carries
``company_id``, NOT NULL, indexed — every query MUST filter on it):

  ``xero_sync_state``  — one row per (company, entity). Holds the incremental
                         **watermark** (the high-water ``UpdatedDateUTC`` we've
                         synced) plus bookkeeping (last run, status, counts).

  ``xero_document``    — the mirrored data. One row per (company, entity,
                         xero_id) holding the RAW Xero JSON. We store raw (not
                         typed columns) so the audit's existing reshape logic
                         maps it exactly as it did the live payload — "only the
                         data source changes, checks stay unchanged".
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, uuid_pk

# Mirrored entities: the first five sync incrementally; the rest are
# watermark-less and full-refresh each sync. journal + asset (SOP) need extra
# Xero scopes (accounting.journals.read / assets.read) → 0 records until granted.
SYNC_ENTITIES: tuple[str, ...] = (
    "invoice",
    "bank_transaction",
    "credit_note",
    "contact",
    "account",
    "tax_rate",
    "payment",
    "organisation",
    "journal",
    "asset",
)


class XeroSyncState(Base):
    """Per-(company, entity) sync watermark + run metadata."""

    __tablename__ = "xero_sync_state"
    __table_args__ = (
        UniqueConstraint("company_id", "entity", name="uq_sync_state_company_entity"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity: Mapped[str] = mapped_column(String(32), nullable=False)
    # High-water mark: max UpdatedDateUTC durably stored for this entity.
    # NULL means never synced, so the next run is a full sync.
    watermark_utc: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_full_sync_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # ok | error | in_progress
    last_status: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Rows touched on the last run (for observability / the Refresh UI).
    last_record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class XeroDocument(Base):
    """One mirrored Xero record (raw JSON), keyed by (company, entity, xero_id)."""

    __tablename__ = "xero_document"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "entity", "xero_id",
            name="uq_xero_document_company_entity_xeroid",
        ),
        # The audit's read path: all rows for one company+entity.
        Index("ix_xero_document_company_entity", "company_id", "entity"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("company.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity: Mapped[str] = mapped_column(String(32), nullable=False)
    # Xero's native id (e.g. InvoiceID, ContactID), or a natural key
    # (TaxType) for entities Xero gives no id.
    xero_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # The complete Xero object exactly as returned, so the audit's reshape
    # sees byte-for-byte what the live fetch produced.
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Parsed UpdatedDateUTC, drives the watermark and ordering.
    # NULL for watermark-less entities (tax rates).
    updated_date_utc: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["XeroSyncState", "XeroDocument", "SYNC_ENTITIES"]
