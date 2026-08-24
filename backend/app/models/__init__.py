"""SQLAlchemy 模型（plan §2 全部 11 张表）。"""
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
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())


class Person(Base):
    __tablename__ = "persons"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    name_normalized: Mapped[str] = mapped_column(String(200))
    openalex_id: Mapped[str | None] = mapped_column(String(50), unique=True)
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
    source: Mapped[str] = mapped_column(String(20))  # glm / openalex / merged
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
    type: Mapped[str] = mapped_column(String(40), nullable=False)  # M1: paper_cooperation
    identity_confidence: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    strength: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    coop_count: Mapped[int] = mapped_column(Integer, default=0)
    time_start: Mapped[dt.date | None] = mapped_column(Date)
    time_end: Mapped[dt.date | None] = mapped_column(Date)
    evidence_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(SADateTime(timezone=True), server_default=func.now(), onupdate=_now)

    # FR-4.4 三保险：代码排序 + 唯一约束 + CHECK 防反向重复
    __table_args__ = (
        UniqueConstraint("person_a_id", "person_b_id", "type"),
        CheckConstraint("person_a_id < person_b_id", name="ck_rel_a_lt_b"),
    )


class RelationshipEvidence(Base):
    __tablename__ = "relationship_evidence"
    __table_args__ = (PrimaryKeyConstraint("relationship_id", "paper_id"),)

    relationship_id: Mapped[int] = mapped_column(ForeignKey("relationships.id", ondelete="CASCADE"))
    paper_id: Mapped[int] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"))


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
    job_type: Mapped[str] = mapped_column(String(40))  # rss_fetch / glm_extract / openalex_lookup
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
