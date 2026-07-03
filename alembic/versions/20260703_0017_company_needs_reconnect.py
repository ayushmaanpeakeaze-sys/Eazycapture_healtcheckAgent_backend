"""company.needs_reconnect connection-health flag

Set when a live Xero fetch (audit/sync) hits a dead grant; cleared on the next
successful fetch. Drives the "Reconnect to Xero" badge so a broken connection
can't keep looking connected.

Revision ID: 20260703_0017
Revises: 20260702_0016
Create Date: 2026-07-03

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260703_0017"
down_revision: Union[str, None] = "20260702_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "company",
        sa.Column(
            "needs_reconnect",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("company", "needs_reconnect")
