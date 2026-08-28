"""callrecord.hit — the adapter's found/not-found verdict (routing hit rate)

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-28

Nullable, no default: an instant metadata-only ALTER on Postgres even on the hot audit table.
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0006'
down_revision: str | Sequence[str] | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('callrecord', sa.Column('hit', sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('callrecord') as batch:
        batch.drop_column('hit')
