"""T16：消歧审核队列 API（FR-3.4，AC-9）。

- GET  /api/disambiguation?status=pending —— 待审列表（两人 + 总分 + 分项得分）
- POST /api/disambiguation/{id}/merge —— body {keep: person_id}：
  论文署名/标签/机构归属迁入保留者，关系与证据合并去重后重算，队列记 merged
- POST /api/disambiguation/{id}/reject —— 记 rejected；uq_disamb_pair 保证
  同对组合不再入队（disambiguator._enqueue ON CONFLICT DO NOTHING），
  新相似作者仍可与 A/B 正常入队
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.graph import _in_scope_person_ids
from app.db import get_session
from app.models import (
    DisambiguationQueue,
    Paper,
    PaperAuthor,
    Person,
    PersonOrg,
    PersonResearchTag,
    Relationship,
    RelationshipEvidence,
)
from app.services.linker import _identity_confidence, tier

router = APIRouter(prefix="/api/disambiguation")

VALID_STATUS = ("pending", "merged", "rejected")


class MergeBody(BaseModel):
    keep: int  # 保留 person id（须为队列条目两端之一）


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _get_queue_row(session: AsyncSession, queue_id: int) -> DisambiguationQueue:
    row = await session.get(DisambiguationQueue, queue_id)
    if row is None:
        raise HTTPException(status_code=404, detail="审核条目不存在")
    return row


@router.get("")
async def list_queue(
    status: str = Query("pending", pattern="^(pending|merged|rejected)$"),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> dict:
    pa = aliased(Person)
    pb = aliased(Person)
    # M1 范围（2026-08-31）：至少一端出现在含中国学者的论文上，纯外国重复对不占审核队列
    in_scope = _in_scope_person_ids()
    rows = (
        await session.execute(
            select(DisambiguationQueue, pa, pb)
            .join(pa, DisambiguationQueue.person_a_id == pa.id)
            .join(pb, DisambiguationQueue.person_b_id == pb.id)
            .where(
                DisambiguationQueue.status == status,
                or_(
                    DisambiguationQueue.person_a_id.in_(in_scope),
                    DisambiguationQueue.person_b_id.in_(in_scope),
                ),
            )
            .order_by(DisambiguationQueue.created_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "id": q.id,
                "person_a": {"id": a.id, "name": a.name},
                "person_b": {"id": b.id, "name": b.name},
                "score": float(q.score),
                "score_detail": q.score_detail or {},
                "created_at": q.created_at.isoformat() if q.created_at else None,
            }
            for q, a, b in rows
        ]
    }


@router.post("/{queue_id}/merge")
async def merge(
    queue_id: int, body: MergeBody, session: AsyncSession = Depends(get_session)
) -> dict:
    q = await _get_queue_row(session, queue_id)
    if q.status != "pending":
        raise HTTPException(status_code=409, detail=f"条目已处理（{q.status}）")
    pair = {q.person_a_id, q.person_b_id}
    if body.keep not in pair:
        raise HTTPException(status_code=422, detail="keep 必须是条目两端 person 之一")
    keep_id = body.keep
    drop_id = q.person_b_id if keep_id == q.person_a_id else q.person_a_id
    keep = await session.get(Person, keep_id)
    drop = await session.get(Person, drop_id)

    # 1. 论文署名迁入保留者
    await session.execute(
        update(PaperAuthor).where(PaperAuthor.person_id == drop_id).values(person_id=keep_id)
    )

    # 2. 研究方向标签合并（幂等）
    drop_tags = (
        await session.execute(
            select(PersonResearchTag.tag).where(PersonResearchTag.person_id == drop_id)
        )
    ).scalars().all()
    for tag in drop_tags:
        await session.execute(
            pg_insert(PersonResearchTag)
            .values(person_id=keep_id, tag=tag)
            .on_conflict_do_nothing()
        )
    await session.execute(
        delete(PersonResearchTag).where(PersonResearchTag.person_id == drop_id)
    )

    # 3. 机构归属合并（同机构保留更高置信度）
    drop_orgs = (
        await session.execute(
            select(PersonOrg).where(PersonOrg.person_id == drop_id)
        )
    ).scalars().all()
    for po in drop_orgs:
        existing = (
            await session.execute(
                select(PersonOrg).where(
                    PersonOrg.person_id == keep_id, PersonOrg.org_id == po.org_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                PersonOrg(
                    person_id=keep_id,
                    org_id=po.org_id,
                    org_confidence=po.org_confidence,
                    source="merged",
                    paper_id=po.paper_id,
                )
            )
        elif float(existing.org_confidence) < float(po.org_confidence):
            existing.org_confidence = po.org_confidence
            existing.source = "merged"
    await session.execute(delete(PersonOrg).where(PersonOrg.person_id == drop_id))

    # 4. 强身份信号：openalex_id 缺则补（同一 id 只能挂在保留者身上）
    if not keep.openalex_id and drop.openalex_id:
        keep.openalex_id = drop.openalex_id
        drop.openalex_id = None

    # 5. 关系迁移：第三者关系并入/改挂；keep-drop 直接关系删除（自环无意义）
    drop_rels = (
        await session.execute(
            select(Relationship).where(
                (Relationship.person_a_id == drop_id) | (Relationship.person_b_id == drop_id)
            )
        )
    ).scalars().all()
    for rel in drop_rels:
        third = rel.person_b_id if rel.person_a_id == drop_id else rel.person_a_id
        if third == keep_id:
            await session.execute(
                delete(RelationshipEvidence).where(RelationshipEvidence.relationship_id == rel.id)
            )
            await session.delete(rel)
            continue
        lo, hi = min(keep_id, third), max(keep_id, third)
        target = (
            await session.execute(
                select(Relationship).where(
                    Relationship.person_a_id == lo,
                    Relationship.person_b_id == hi,
                    Relationship.type == rel.type,
                )
            )
        ).scalar_one_or_none()
        if target is None:
            rel.person_a_id, rel.person_b_id = lo, hi  # 改挂 + 重排（三保险）
        else:
            # 证据并入目标（PK 幂等），删除被并关系
            ev_ids = (
                await session.execute(
                    select(RelationshipEvidence.paper_id).where(
                        RelationshipEvidence.relationship_id == rel.id
                    )
                )
            ).scalars().all()
            for pid in ev_ids:
                await session.execute(
                    pg_insert(RelationshipEvidence)
                    .values(relationship_id=target.id, paper_id=pid)
                    .on_conflict_do_nothing()
                )
            await session.execute(
                delete(RelationshipEvidence).where(RelationshipEvidence.relationship_id == rel.id)
            )
            await session.delete(rel)

    # 6. drop 置为墓碑（保留行：队列 FK 审计需要）；其涉及的其它 pending 队列
    #    行随本决定一并结案 merged
    drop.merged_into_id = keep_id
    now = _now()
    subsumed = (
        await session.execute(
            select(DisambiguationQueue).where(
                DisambiguationQueue.status == "pending",
                (DisambiguationQueue.person_a_id == drop_id)
                | (DisambiguationQueue.person_b_id == drop_id),
                DisambiguationQueue.id != q.id,
            )
        )
    ).scalars().all()
    for other in subsumed:
        other.status = "merged"
        other.resolved_at = now
    await session.flush()

    # 7. 重算保留者的全部关系（evidence 为合作次数的事实来源）
    await _recompute_person_relationships(session, keep_id)

    q.status = "merged"
    q.resolved_at = now
    await session.commit()
    return {"status": "merged", "kept": keep_id, "merged_into": keep_id, "removed": drop_id}


@router.post("/{queue_id}/reject")
async def reject(queue_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    q = await _get_queue_row(session, queue_id)
    if q.status != "pending":
        raise HTTPException(status_code=409, detail=f"条目已处理（{q.status}）")
    q.status = "rejected"
    q.resolved_at = _now()
    await session.commit()
    return {"status": "rejected", "queue_id": queue_id}


async def _recompute_person_relationships(session: AsyncSession, person_id: int) -> None:
    """合并后重算：coop_count=证据数、时间范围、identity、strength、summary。"""
    rels = (
        await session.execute(
            select(Relationship).where(
                (Relationship.person_a_id == person_id)
                | (Relationship.person_b_id == person_id)
            )
        )
    ).scalars().all()
    for rel in rels:
        published = (
            await session.execute(
                select(Paper.published_at)
                .join(RelationshipEvidence, RelationshipEvidence.paper_id == Paper.id)
                .where(RelationshipEvidence.relationship_id == rel.id)
            )
        ).scalars().all()
        dates = [d.date() for d in published if d is not None]
        rel.coop_count = max(len(dates), 1)
        rel.time_start = min(dates) if dates else rel.time_start
        rel.time_end = max(dates) if dates else rel.time_end
        rel.identity_confidence = min(
            await _identity_confidence(session, rel.person_a_id),
            await _identity_confidence(session, rel.person_b_id),
        )
        rel.strength = float(rel.identity_confidence) * tier(rel.coop_count)
        rel.evidence_summary = (
            f"基于 {rel.coop_count} 篇合作论文，最近合作于 {rel.time_end.year} 年"
            if rel.time_end
            else f"基于 {rel.coop_count} 篇合作论文"
        )
