"""org.platform_overflow_disabled — the overflow opt-out (docs/context/ops/capacity.md)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-28

Added with a server default so existing rows are valid, then the default is dropped to match the
legacy create_all shape (no server default; the default lives in Python), which the parity test
compares.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0008'
down_revision: str | Sequence[str] | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('org', sa.Column('platform_overflow_disabled', sa.Boolean(), nullable=False,
                                   server_default=sa.false()))
    with op.batch_alter_table('org') as batch:
        batch.alter_column('platform_overflow_disabled', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('org') as batch:
        batch.drop_column('platform_overflow_disabled')
