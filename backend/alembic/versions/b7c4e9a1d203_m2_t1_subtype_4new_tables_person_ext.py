"""M2-T1: relationships.subtype + 唯一键改造 + 4 张新表 + persons/papers 扩展

Revision ID: b7c4e9a1d203
Revises: 86b8c7390822
Create Date: 2026-08-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'b7c4e9a1d203'
down_revision: Union[str, Sequence[str], None] = '86b8c7390822'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """M2 plan §2 DDL 增量（存量 paper_cooperation 行 subtype='' 零迁移）。"""
    # persons 扩展（FR-3.6 网页抽取补全字段）
    op.add_column('persons', sa.Column('title', sa.String(length=100), nullable=True))
    op.add_column('persons', sa.Column('homepage', sa.Text(), nullable=True))
    op.add_column('persons', sa.Column('email', sa.String(length=200), nullable=True))
    # papers.last_filtered_at（D2 重筛优化：已筛且未变化的论文不重复细筛）
    op.add_column('papers', sa.Column('last_filtered_at', sa.DateTime(timezone=True), nullable=True))
    # 漂移修复：has_cn_scholar 曾由 scripts/rebuild_cn_scope.py 直接加列（未经 alembic），
    # 幂等补记使空库 upgrade 到 head 也能得到与模型一致的完整 schema
    op.execute("ALTER TABLE papers ADD COLUMN IF NOT EXISTS has_cn_scholar BOOLEAN NOT NULL DEFAULT FALSE")
    # relationships：subtype + 唯一键 (a,b,type) → (a,b,type,subtype)（RD-M2-2）
    op.add_column('relationships', sa.Column('subtype', sa.String(length=30), nullable=False, server_default=''))
    op.drop_constraint('relationships_person_a_id_person_b_id_type_key', 'relationships', type_='unique')
    op.create_unique_constraint('uq_rel_pair_type_subtype', 'relationships', ['person_a_id', 'person_b_id', 'type', 'subtype'])

    # 新表（plan §2.2）
    op.create_table(
        'web_pages',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('seed_id', sa.String(length=100), nullable=False),
        sa.Column('page_type', sa.String(length=30), nullable=False),
        sa.Column('title', sa.Text(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('content_text', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending_extraction'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_extracted_hash', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('url'),
    )
    op.create_table(
        'news_items',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('source_id', sa.String(length=50), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rss_entry', JSONB(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending_screen'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('url'),
    )
    op.create_table(
        'projects',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=300), nullable=False),
        sa.Column('name_normalized', sa.String(length=300), nullable=False),
        sa.Column('project_type', sa.String(length=50), nullable=True),
        sa.Column('time_start', sa.Date(), nullable=True),
        sa.Column('time_end', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name_normalized'),
    )
    op.create_table(
        'relationship_evidence_pages',
        sa.Column('relationship_id', sa.BigInteger(), nullable=False),
        sa.Column('web_page_id', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['relationship_id'], ['relationships.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['web_page_id'], ['web_pages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('relationship_id', 'web_page_id'),
    )
    op.create_table(
        'relationship_evidence_news',
        sa.Column('relationship_id', sa.BigInteger(), nullable=False),
        sa.Column('news_item_id', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['relationship_id'], ['relationships.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['news_item_id'], ['news_items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('relationship_id', 'news_item_id'),
    )


def downgrade() -> None:
    op.drop_table('relationship_evidence_news')
    op.drop_table('relationship_evidence_pages')
    op.drop_table('projects')
    op.drop_table('news_items')
    op.drop_table('web_pages')
    op.drop_constraint('uq_rel_pair_type_subtype', 'relationships', type_='unique')
    op.create_unique_constraint('relationships_person_a_id_person_b_id_type_key', 'relationships', ['person_a_id', 'person_b_id', 'type'])
    op.drop_column('relationships', 'subtype')
    op.drop_column('papers', 'last_filtered_at')
    op.drop_column('persons', 'email')
    op.drop_column('persons', 'homepage')
    op.drop_column('persons', 'title')
