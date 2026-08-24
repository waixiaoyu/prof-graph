"""persons merged_into_id tombstone

Revision ID: 86b8c7390822
Revises: 21e0b339dab1
Create Date: 2026-08-24 21:06:28.389960

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '86b8c7390822'
down_revision: Union[str, Sequence[str], None] = '21e0b339dab1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """T16 审核合并：被并入者保留墓碑行（队列 FK 审计），merged_into_id 标记归属。"""
    op.add_column(
        "persons",
        sa.Column("merged_into_id", sa.BigInteger(), sa.ForeignKey("persons.id"), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("persons", "merged_into_id")
