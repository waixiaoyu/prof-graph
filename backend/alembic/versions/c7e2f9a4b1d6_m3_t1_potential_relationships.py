"""M3-T1: potential_relationships 潜在关系表

Revision ID: c7e2f9a4b1d6
Revises: a1f3c8d20b95
Create Date: 2026-08-31 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'c7e2f9a4b1d6'
down_revision: Union[str, Sequence[str], None] = 'a1f3c8d20b95'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """M3 plan §1 DDL（存量零迁移，新表）。"""
    op.create_table(
        'potential_relationships',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('person_a_id', sa.BigInteger(), nullable=False),
        sa.Column('person_b_id', sa.BigInteger(), nullable=False),
        sa.Column('discovery_method', sa.String(length=30), nullable=False),
        sa.Column('confidence', sa.Numeric(3, 2), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('supporting_signals', JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['person_a_id'], ['persons.id']),
        sa.ForeignKeyConstraint(['person_b_id'], ['persons.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('person_a_id', 'person_b_id', 'discovery_method', name='uq_potential_pair_method'),
        sa.CheckConstraint('person_a_id < person_b_id', name='ck_potential_a_lt_b'),
        sa.CheckConstraint('confidence BETWEEN 0.10 AND 0.70', name='ck_potential_confidence'),
    )
    op.create_index('idx_potential_a', 'potential_relationships', ['person_a_id'])
    op.create_index('idx_potential_b', 'potential_relationships', ['person_b_id'])


def downgrade() -> None:
    op.drop_index('idx_potential_b', table_name='potential_relationships')
    op.drop_index('idx_potential_a', table_name='potential_relationships')
    op.drop_table('potential_relationships')
