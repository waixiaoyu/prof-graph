"""关系建立器（T12，FR-4.1~4.5）。

对每篇已消歧论文的两两作者建 paper_cooperation：
- ID 排序防重（a<b）+ UNIQUE + CHECK 三保险（模型层已定义约束）
- identity_confidence = 0.4 × name_confidence + 0.6 × org_confidence
- strength = identity_confidence × tier(coop_count)
  （1 次 0.85 / 2 次 0.90 / 3-4 次 0.95 / 5 次+ 1.00，plan §5）
- 已存在：coop_count += 1、重算 strength、追加 relationship_evidence、
  更新时间范围与 evidence_summary（"基于 N 篇合作论文，最近合作于 YYYY 年"）
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Paper, PaperAuthor, Person, PersonOrg, Relationship, RelationshipEvidence

log = logging.getLogger("prof-graph.linker")

REL_TYPE = "paper_cooperation"


def tier(coop_count: int) -> float:
    """合作次数阶梯（plan §5，N=5 封顶）。"""
    if coop_count >= 5:
        return 1.00
    if coop_count >= 3:
        return 0.95
    if coop_count == 2:
        return 0.90
    return 0.85


async def _identity_confidence(session: AsyncSession, person_id: int) -> float:
    """0.4 × name + 0.6 × org。name_confidence：M1 抽取名即原始名 1.0；
    org_confidence：取该人最高置信度机构归属，无机构 0.4 兜底。"""
    name_confidence = 1.0
    best_org = (
        await session.execute(
            select(func.max(PersonOrg.org_confidence)).where(
                PersonOrg.person_id == person_id
            )
        )
    ).scalar()
    org_confidence = float(best_org) if best_org is not None else 0.4
    return 0.4 * name_confidence + 0.6 * org_confidence


async def link_paper(session: AsyncSession, paper: Paper) -> int:
    """单篇论文：两两作者建/更新 paper_cooperation。返回新建关系数。"""
    rows = (
        await session.execute(
            select(PaperAuthor)
            .where(PaperAuthor.paper_id == paper.id, PaperAuthor.person_id.is_not(None))
            .order_by(PaperAuthor.author_seq)
        )
    ).scalars().all()
    persons = [
        await session.get(Person, pa.person_id)
        for pa in rows
    ]
    person_ids = [p.id for p in persons if p is not None]

    created = 0
    for i in range(len(person_ids)):
        for j in range(i + 1, len(person_ids)):
            a, b = person_ids[i], person_ids[j]  # 按 seq 顺序
            lo, hi = min(a, b), max(a, b)        # 三保险之一：代码排序

            rel = (
                await session.execute(
                    select(Relationship).where(
                        Relationship.person_a_id == lo,
                        Relationship.person_b_id == hi,
                        Relationship.type == REL_TYPE,
                    )
                )
            ).scalar_one_or_none()

            if rel is None:
                identity = min(
                    await _identity_confidence(session, lo),
                    await _identity_confidence(session, hi),
                )
                rel = Relationship(
                    person_a_id=lo,
                    person_b_id=hi,
                    type=REL_TYPE,
                    identity_confidence=identity,
                    strength=identity * tier(1),
                    coop_count=1,
                    time_start=paper.published_at.date() if paper.published_at else None,
                    time_end=paper.published_at.date() if paper.published_at else None,
                )
                session.add(rel)
                await session.flush()
                created += 1
            else:
                rel.coop_count += 1
                rel.identity_confidence = min(
                    await _identity_confidence(session, lo),
                    await _identity_confidence(session, hi),
                )
                rel.strength = float(rel.identity_confidence) * tier(rel.coop_count)
                if paper.published_at is not None:
                    d = paper.published_at.date()
                    if rel.time_start is None or d < rel.time_start:
                        rel.time_start = d
                    if rel.time_end is None or d > rel.time_end:
                        rel.time_end = d

            # 证据（(relationship, paper) 主键幂等）
            ev_exists = (
                await session.execute(
                    select(RelationshipEvidence).where(
                        RelationshipEvidence.relationship_id == rel.id,
                        RelationshipEvidence.paper_id == paper.id,
                    )
                )
            ).scalar_one_or_none()
            if ev_exists is None:
                session.add(
                    RelationshipEvidence(relationship_id=rel.id, paper_id=paper.id)
                )

            rel.evidence_summary = (
                f"基于 {rel.coop_count} 篇合作论文，"
                f"最近合作于 {rel.time_end.year} 年" if rel.time_end
                else f"基于 {rel.coop_count} 篇合作论文"
            )

    return created


async def run_linker(session: AsyncSession, paper_ids: list[int] | None = None) -> dict:
    """对全部已消歧（extracted）论文建关系。"""
    stmt = select(Paper).where(Paper.status == "extracted")
    if paper_ids is not None:
        stmt = stmt.where(Paper.id.in_(paper_ids))
    papers = (await session.execute(stmt)).scalars().all()

    created = 0
    for paper in papers:
        created += await link_paper(session, paper)
    await session.commit()
    return {"papers": len(papers), "relationships_created": created}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
