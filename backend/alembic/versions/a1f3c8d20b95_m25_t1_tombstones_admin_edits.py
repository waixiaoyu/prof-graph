"""M2.5-T1: persons/relationships 墓碑列 + admin_edits 操作日志表

Revision ID: a1f3c8d20b95
Revises: d4e8a2c6f307
Create Date: 2026-08-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'a1f3c8d20b95'
down_revision: Union[str, Sequence[str], None] = 'd4e8a2c6f307'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """M2.5 plan §1 DDL（存量数据零迁移，新列全可空）。"""
    op.add_column('persons', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('relationships', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('relationships', sa.Column('deleted_reason', sa.Text(), nullable=True))
    op.create_table(
        'admin_edits',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('action', sa.String(length=50), nullable=False),
        sa.Column('entity_type', sa.String(length=20), nullable=False),
        sa.Column('entity_id', sa.BigInteger(), nullable=False),
        sa.Column('before', JSONB(), nullable=True),
        sa.Column('after', JSONB(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_admin_edits_entity', 'admin_edits', ['entity_type', 'entity_id'])
    op.create_index('idx_admin_edits_created', 'admin_edits', ['created_at'])


def downgrade() -> None:
    op.drop_index('idx_admin_edits_created', table_name='admin_edits')
    op.drop_index('idx_admin_edits_entity', table_name='admin_edits')
    op.drop_table('admin_edits')
    op.drop_column('relationships', 'deleted_reason')
    op.drop_column('relationships', 'deleted_at')
    op.drop_column('persons', 'deleted_at')
