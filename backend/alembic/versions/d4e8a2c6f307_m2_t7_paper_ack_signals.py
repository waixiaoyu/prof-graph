"""M2-T7: papers.mentorship_signals（致谢信号跨阶段暂存，RD-M2-8）

Revision ID: d4e8a2c6f307
Revises: b7c4e9a1d203
Create Date: 2026-08-26 16:00:00.000000

致谢信号在 extract 阶段产出，但建关系要等 disambiguate 之后（作者需已
person_id），故落 papers JSONB 暂存；link 阶段消费后保留作审计。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'd4e8a2c6f307'
down_revision: Union[str, Sequence[str], None] = 'b7c4e9a1d203'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE papers ADD COLUMN IF NOT EXISTS mentorship_signals JSONB NULL"
    )


def downgrade() -> None:
    op.drop_column('papers', 'mentorship_signals')
