"""SQLAlchemy 模型（M1 plan §2 11 张表 + M2 plan §2 增量 5 张新表）。"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy import DateTime as SADateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    arxiv_id: Mapped[str] = mapped_column(String(20), unique=True)
    title: Mapped[str] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    authors_raw: Mapped[list] = mapped_column(JSONB)  # 原始作者名单（顺序保留，含括号机构）
    published_at: Mapped[dt.datetime | None] = mapped_column(SADateTime(timezone=True))
    categories: Mapped[list[str]] = mapped_column(ARRAY(Text))
    rss_entry: Mapped[dict | None] = mapped_column(JSONB)
    ai_relevant: Mapped[bool] = mapped_column(Boolean, default=True)
    directions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    tracks: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    # GLM 抽取的研究方向标签（plan §3，§6 消歧 Jaccard 用；T8 的 tracks 是配置库关键词标签）
    research_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    # pending_extraction / extracted / extraction_failed / filtered_out
    status: Mapped[str] = mapped_column(String(20), default="pending_extraction")
    # M1 范围约束（2026-08-31）：论文是否含中国学者（含其在国外机构任职）；
    # False 的论文不进关系网络（linker 跳过），图谱/搜索按范围过滤
    has_cn_scholar: Mapped[bool] = mapped_column(Boolean, default=False)
    # D2 重筛优化（M2-T1）：上次 GLM 细筛时间，已筛且未变化的论文不重复细筛
    last_filtered_at: Mapped[dt.datetime | None] = mapped_column(SADateTime(timezone=True))
    # M2-T7 致谢信号（RD-M2-8）：GLM 全文抽取的师生信号，disambiguate 后由
    # mentor_linker 消费建关系（证据挂 relationship_evidence），保留作审计
    mentorship_signals: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())


class Person(Base):
    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    name_normalized: Mapped[str] = mapped_column(String(200))
    openalex_id: Mapped[str | None] = mapped_column(String(50), unique=True)
    # 审核合并墓碑：被并入者保留行（disambiguation_queue FK 审计需要），
    # 图谱/搜索/消歧候选按 merged_into_id IS NULL 排除
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id"))
    # M2 扩展（FR-3.6，网页抽取补全；论文抽取不动这三列）
    title: Mapped[str | None] = mapped_column(String(100))  # 职位/职称：教授 / 长聘副教授 / ...
    homepage: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String(200))
    # M2.5 合规删除墓碑（FR-5）：与 merged_into 同口径，图谱/搜索/消歧候选按
    # deleted_at IS NULL 排除；行保留作审计
    deleted_at: Mapped[dt.datetime | None] = mapped_column(SADateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now(), onupdate=_now)

    research_tags: Mapped[list["PersonResearchTag"]] = relationship(back_populates="person", cascade="all, delete-orphan")


Index("idx_persons_name", Person.name_normalized)


class PersonResearchTag(Base):
    __tablename__ = "person_research_tags"
    __table_args__ = (PrimaryKeyConstraint("person_id", "tag"),)

    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    tag: Mapped[str] = mapped_column(String(100))
    person: Mapped[Person] = relationship(back_populates="research_tags")


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(300))
    name_normalized: Mapped[str] = mapped_column(String(300))
    level: Mapped[str | None] = mapped_column(String(20))  # university / institute / company / lab
    website: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("name_normalized"),)


class PersonOrg(Base):
    __tablename__ = "person_org"
    __table_args__ = (PrimaryKeyConstraint("person_id", "org_id"),)

    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id", ondelete="CASCADE"))
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    org_confidence: Mapped[float] = mapped_column(Numeric(3, 2), default=0.4)
    source: Mapped[str] = mapped_column(String(20))  # glm / openalex / merged / webpage（M2 网页抽取）
    paper_id: Mapped[int | None] = mapped_column(ForeignKey("papers.id"))


class PaperAuthor(Base):
    __tablename__ = "paper_authors"
    __table_args__ = (PrimaryKeyConstraint("paper_id", "author_seq"),)

    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))
    author_seq: Mapped[int] = mapped_column(Integer)  # 0-based
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id"))
    raw_name: Mapped[str] = mapped_column(String(200))
    name_confidence: Mapped[float] = mapped_column(Numeric(3, 2), default=1.0)
    # GLM 抽取的署名机构（T10 机构双源补全读此列；无则为 NULL）
    affiliation: Mapped[str | None] = mapped_column(Text)
    # OpenAlex 匹配结果（T11 强匹配信号；'openalex' 表示机构来自 OpenAlex 而非 GLM）
    openalex_id: Mapped[str | None] = mapped_column(String(50))
    org_source: Mapped[str | None] = mapped_column(String(20))


class Relationship(Base):
    __tablename__ = "relationships"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    person_a_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False)
    person_b_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)  # paper_cooperation / academic_mentorship / project_cooperation
    # RD-M2-2 学术传承四子类型；论文/项目合作为 ''（存量 paper_cooperation 行零迁移）
    subtype: Mapped[str] = mapped_column(String(30), nullable=False, server_default="")  # mentor_student / same_lab / same_advisor / same_cohort
    identity_confidence: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    strength: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    coop_count: Mapped[int] = mapped_column(Integer, default=0)
    time_start: Mapped[dt.date | None] = mapped_column(Date)
    time_end: Mapped[dt.date | None] = mapped_column(Date)
    evidence_summary: Mapped[str | None] = mapped_column(Text)
    # M2.5 墓碑删除（FR-4，RD-2）：非空 = 管理员已删，管线不得复活，证据保留作审计
    deleted_at: Mapped[dt.datetime | None] = mapped_column(SADateTime(timezone=True))
    deleted_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now(), onupdate=_now)

    # FR-4.4 三保险：代码排序 + 唯一约束 + CHECK 防反向重复
    # M2：同一对人可同时存在 same_lab 与 mentor_student → 唯一键含 subtype（RD-M2-2）
    __table_args__ = (
        UniqueConstraint("person_a_id", "person_b_id", "type", "subtype", name="uq_rel_pair_type_subtype"),
        CheckConstraint("person_a_id < person_b_id", name="ck_rel_a_lt_b"),
    )


class RelationshipEvidence(Base):
    __tablename__ = "relationship_evidence"
    __table_args__ = (PrimaryKeyConstraint("relationship_id", "paper_id"),)

    relationship_id: Mapped[int] = mapped_column(ForeignKey("relationships.id", ondelete="CASCADE"))
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))


# ---- M2 新表（plan §2.2，RD-M2-1 分类型证据表沿用 M1 模式）----


class WebPage(Base):
    """爬取页面快照（学术传承主线证据 + 高校新闻公示页，FR-2）。"""

    __tablename__ = "web_pages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, unique=True)
    seed_id: Mapped[str] = mapped_column(String(100))  # 来源种子标识（sources.yaml 的 seed.id）
    page_type: Mapped[str] = mapped_column(String(30))  # faculty / lab_members / grad_list / news
    title: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())
    content_text: Mapped[str | None] = mapped_column(Text)  # 去导航/脚本后的正文
    content_hash: Mapped[str | None] = mapped_column(String(64))  # SHA-256，增量重爬跳过用
    # pending_extraction / extracted / no_signal / extraction_failed
    status: Mapped[str] = mapped_column(String(20), default="pending_extraction")
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())
    last_extracted_hash: Mapped[str | None] = mapped_column(String(64))  # 上次已抽取内容的指纹（区分"变了"与"没变"）


class NewsItem(Base):
    """资讯条目（RSS 源，项目合作证据，FR-1）。"""

    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(50))  # sources.yaml 的 rss.id
    url: Mapped[str] = mapped_column(Text, unique=True)  # 去重键（link/guid 归一）
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[dt.datetime | None] = mapped_column(SADateTime(timezone=True))  # 缺 pubDate 时用抓取时间
    rss_entry: Mapped[dict | None] = mapped_column(JSONB)  # 原始条目（审计）
    # pending_screen / screened_no_signal / extracted / no_signal / extraction_failed
    status: Mapped[str] = mapped_column(String(20), default="pending_screen")
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())


class Project(Base):
    """项目轻量实体（RD-M2-6 降级：仅作关系证据锚点，不做项目管理）。"""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(300))
    name_normalized: Mapped[str] = mapped_column(String(300))
    project_type: Mapped[str | None] = mapped_column(String(50))  # 国家重点研发 / 省市科技项目 / 企业合作 / 联合实验室 / other
    time_start: Mapped[dt.date | None] = mapped_column(Date)
    time_end: Mapped[dt.date | None] = mapped_column(Date)
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("name_normalized"),)


class RelationshipEvidencePage(Base):
    """传承关系 × 网页证据（RD-M2-1 分类型证据表）。"""

    __tablename__ = "relationship_evidence_pages"
    __table_args__ = (PrimaryKeyConstraint("relationship_id", "web_page_id"),)

    relationship_id: Mapped[int] = mapped_column(ForeignKey("relationships.id", ondelete="CASCADE"))
    web_page_id: Mapped[int] = mapped_column(ForeignKey("web_pages.id", ondelete="CASCADE"))


class RelationshipEvidenceNews(Base):
    """项目合作关系 × 资讯证据（RD-M2-1 分类型证据表）。"""

    __tablename__ = "relationship_evidence_news"
    __table_args__ = (PrimaryKeyConstraint("relationship_id", "news_item_id"),)

    relationship_id: Mapped[int] = mapped_column(ForeignKey("relationships.id", ondelete="CASCADE"))
    news_item_id: Mapped[int] = mapped_column(ForeignKey("news_items.id", ondelete="CASCADE"))


class DisambiguationQueue(Base):
    __tablename__ = "disambiguation_queue"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    person_a_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False)
    person_b_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False)
    score: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    score_detail: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending / merged / rejected
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[dt.datetime | None] = mapped_column(SADateTime(timezone=True))

    # reject 语义（R 轮确认）：A≠B 结论持久化，同对组合不再重复入队
    __table_args__ = (UniqueConstraint("person_a_id", "person_b_id", name="uq_disamb_pair"),)


class FailedJob(Base):
    __tablename__ = "failed_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(40))  # rss_fetch / glm_extract / openalex_lookup / web_crawl / news_fetch / news_extract
    target: Mapped[str] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(SADateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="retrying")  # retrying / dead / done
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    day: Mapped[dt.date] = mapped_column(Date)
    job_type: Mapped[str] = mapped_column(String(40))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())


Index("idx_token_usage_day", TokenUsage.day)


# ---- M2.5 新表（plan §1）----


class AdminEdit(Base):
    """后台手动编辑操作日志（FR-6，RD-7 无操作者列：单管理员内网系统）。"""

    __tablename__ = "admin_edits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # update_person / set_orgs / set_research_tags / delete_relationship /
    # adjust_strength / delete_person
    action: Mapped[str] = mapped_column(String(50))
    entity_type: Mapped[str] = mapped_column(String(20))  # person / relationship
    entity_id: Mapped[int] = mapped_column(BigInteger)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())


Index("idx_admin_edits_entity", AdminEdit.entity_type, AdminEdit.entity_id)
Index("idx_admin_edits_created", AdminEdit.created_at)


# ---- M3 新表（plan §1）----


class PotentialRelationship(Base):
    """潜在关系（M3，RD-1：纯派生数据、无证据链、不进 relationships 表；
    每周日 06:00 全量重算，整表替换式更新）。"""

    __tablename__ = "potential_relationships"
    __table_args__ = (
        UniqueConstraint(
            "person_a_id", "person_b_id", "discovery_method", name="uq_potential_pair_method"
        ),
        CheckConstraint("person_a_id < person_b_id", name="ck_potential_a_lt_b"),
        CheckConstraint("confidence BETWEEN 0.10 AND 0.70", name="ck_potential_confidence"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    person_a_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False)
    person_b_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), nullable=False)
    discovery_method: Mapped[str] = mapped_column(String(30), nullable=False)  # common_network / research_similarity
    confidence: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    supporting_signals: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        SADateTime(timezone=True), server_default=func.now(), onupdate=_now
    )


Index("idx_potential_a", PotentialRelationship.person_a_id)
Index("idx_potential_b", PotentialRelationship.person_b_id)
