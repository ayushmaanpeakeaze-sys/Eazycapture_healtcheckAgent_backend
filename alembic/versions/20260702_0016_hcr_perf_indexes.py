"""health_check_result performance indexes

GIN index on the ``result`` JSONB (speeds the ``result @> {...}`` containment
filters) and a composite ``(company_id, kind, status)`` for the trapped-feed
hot path. Both are additive and non-functional (no query result changes).

Revision ID: 20260702_0016
Revises: 20260630_0015
Create Date: 2026-07-02

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "20260702_0016"
down_revision: Union[str, None] = "20260630_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_hcr_company_kind_status",
        "health_check_result",
        ["company_id", "kind", "status"],
    )
    op.create_index(
        "ix_hcr_result_gin",
        "health_check_result",
        ["result"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_hcr_result_gin", table_name="health_check_result")
    op.drop_index("ix_hcr_company_kind_status", table_name="health_check_result")
