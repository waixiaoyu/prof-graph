"""T15：图谱与查询 API（FR-5.1~5.5）。

- GET /api/graph —— direction/track/strength_min/org/person/limit 过滤，strength 降序 Top limit
  （org=机构切入：任一端为该机构成员的关系；person=老师切入：以其为中心的合作子网）
- GET /api/persons/search —— 姓名/机构 LIKE，限 20（M1 范围：仅含中国学者论文上出现的人）
- GET /api/persons/{id} —— 详情（机构/研究方向/论文）
- GET /api/relationships/{id}/evidence —— 证据论文列表（标题/年份）

M1 范围约束（2026-08-31）：只治理含中国学者的论文，节点聚合/搜索均按
papers.has_cn_scholar 过滤；关系由 linker 仅对范围内论文建立。
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import (
    Organization,
    Paper,
    PaperAuthor,
    Person,
    PersonOrg,
    PersonResearchTag,
    Relationship,
    RelationshipEvidence,
)
from app.services.openalex import normalize_org
from app.utils.names import normalize_name

router = APIRouter(prefix="/api")

SEARCH_LIMIT = 20


def _in_scope_person_ids() -> select:
    """M1 范围内的人：出现在 ≥1 篇含中国学者论文上的作者。"""
    return (
        select(PaperAuthor.person_id)
        .join(Paper, PaperAuthor.paper_id == Paper.id)
        .where(Paper.has_cn_scholar.is_(True))
    )


def _year(published: dt.datetime | None) -> int | None:
    return published.year if published else None


async def _orgs_by_person(
    session: AsyncSession, person_ids: list[int]
) -> dict[int, list[dict]]:
    if not person_ids:
        return {}
    rows = (
        await session.execute(
            select(PersonOrg.person_id, Organization.name, PersonOrg.org_confidence)
            .join(Organization, PersonOrg.org_id == Organization.id)
            .where(PersonOrg.person_id.in_(person_ids))
            .order_by(PersonOrg.org_confidence.desc())
        )
    ).all()
    result: dict[int, list[dict]] = {}
    for person_id, name, conf in rows:
        result.setdefault(person_id, []).append(
            {"name": name, "confidence": float(conf or 0.4)}
        )
    return result


@router.get("/graph")
async def get_graph(
    direction: str | None = None,
    track: str | None = None,
    strength_min: float = Query(0.0, ge=0.0, le=1.0),
    coop_min: int = Query(0, ge=0, le=20, description="合作次数下限（隐藏单次合作等弱关系）"),
    org: str | None = Query(None, description="机构切入：机构名（与 /filters/options 的 orgs 一致）"),
    person: int | None = Query(None, description="老师切入：以其为中心的合作子网"),
    limit: int = Query(1000, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """合作图谱：edges 按 strength 降序取 Top limit，nodes 为涉及的人。

    - org：任一端为该机构成员的关系（成员的完整合作网络）
    - person：以该老师为中心的 1-hop 关系 + 邻居之间的关联（有剩余额度时）
    """
    stmt = select(Relationship).where(
        Relationship.type == "paper_cooperation",
        Relationship.strength >= strength_min,
        Relationship.coop_count >= coop_min,
    )

    if person is not None:
        stmt = stmt.where(
            or_(
                Relationship.person_a_id == person,
                Relationship.person_b_id == person,
            )
        )
    elif org:
        # 与写入侧 upsert_organization 同一归一化（去 University 等通用词、保留空格）
        org_ids = select(Organization.id).where(
            or_(
                Organization.name == org,
                Organization.name_normalized == normalize_org(org),
            )
        )
        member_ids = select(PersonOrg.person_id).where(
            PersonOrg.org_id.in_(org_ids)
        )
        stmt = stmt.where(
            or_(
                Relationship.person_a_id.in_(member_ids),
                Relationship.person_b_id.in_(member_ids),
            )
        )

    if direction or track:
        conds = [Paper.has_cn_scholar.is_(True)]
        if direction:
            conds.append(Paper.directions.contains([direction]))
        if track:
            conds.append(Paper.tracks.contains([track]))
        in_dir = (
            select(PaperAuthor.person_id)
            .join(Paper, PaperAuthor.paper_id == Paper.id)
            .where(and_(*conds))
        )
        stmt = stmt.where(
            Relationship.person_a_id.in_(in_dir),
            Relationship.person_b_id.in_(in_dir),
        )
    rels = list(
        (await session.execute(stmt.order_by(Relationship.strength.desc()).limit(limit)))
        .scalars()
        .all()
    )

    # 老师切入：额度有剩时补邻居之间的关联（同参数内仍按 strength 优先）
    if person is not None and len(rels) < limit:
        neighbor_ids = {person}
        for r in rels:
            neighbor_ids.add(r.person_a_id)
            neighbor_ids.add(r.person_b_id)
        known = {(r.person_a_id, r.person_b_id) for r in rels}
        extra = (
            (
                await session.execute(
                    select(Relationship)
                    .where(
                        Relationship.type == "paper_cooperation",
                        Relationship.strength >= strength_min,
                        Relationship.person_a_id.in_(neighbor_ids),
                        Relationship.person_b_id.in_(neighbor_ids),
                    )
                    .order_by(Relationship.strength.desc())
                    .limit(limit - len(rels))
                )
            )
            .scalars()
            .all()
        )
        rels.extend(r for r in extra if (r.person_a_id, r.person_b_id) not in known)

    node_ids = sorted({p for r in rels for p in (r.person_a_id, r.person_b_id)})
    persons = {
        p.id: p
        for p in (
            await session.execute(select(Person).where(Person.id.in_(node_ids)))
        ).scalars()
    }
    orgs = await _orgs_by_person(session, node_ids)

    # 节点方向/赛道/论文数（只聚合 M1 范围内的论文，供前端高亮子网）
    paper_rows = (
        await session.execute(
            select(
                PaperAuthor.person_id,
                Paper.directions,
                Paper.tracks,
            )
            .join(Paper, PaperAuthor.paper_id == Paper.id)
            .where(PaperAuthor.person_id.in_(node_ids), Paper.has_cn_scholar.is_(True))
        )
    ).all()
    agg: dict[int, dict] = {pid: {"directions": set(), "tracks": set(), "papers": 0} for pid in node_ids}
    for pid, dirs, tracks in paper_rows:
        agg[pid]["directions"].update(dirs or [])
        agg[pid]["tracks"].update(tracks or [])
        agg[pid]["papers"] += 1

    nodes = [
        {
            "id": pid,
            "name": persons[pid].name,
            "orgs": orgs.get(pid, []),
            "directions": sorted(a["directions"]),
            "tracks": sorted(a["tracks"]),
            "paper_count": a["papers"],
        }
        for pid, a in agg.items()
    ]
    edges = [
        {
            "id": r.id,
            "source": r.person_a_id,
            "target": r.person_b_id,
            "strength": float(r.strength),
            "coop_count": r.coop_count,
            "time_start": r.time_start.isoformat() if r.time_start else None,
            "time_end": r.time_end.isoformat() if r.time_end else None,
            "evidence_summary": r.evidence_summary,
        }
        for r in rels
    ]
    return {"nodes": nodes, "edges": edges}


@router.get("/persons/search")
async def search_persons(
    q: str = Query(..., min_length=1),
    type: str = Query("name", pattern="^(name|org)$"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """姓名/机构搜索（FR-5.4）。LIKE 前缀模糊，限 20。M1 范围内的人。"""
    in_scope = Person.id.in_(_in_scope_person_ids())
    if type == "name":
        # name_normalized 无空格，查询词同步去空格（"wei zh" → "weizh"）
        needle = f"%{q.lower().replace(' ', '')}%"
        stmt = select(Person).where(
            Person.name_normalized.like(needle),
            Person.merged_into_id.is_(None),  # 排除审核合并墓碑
            in_scope,
        )
    else:
        needle = f"%{q.lower()}%"
        stmt = (
            select(Person)
            .join(PersonOrg, PersonOrg.person_id == Person.id)
            .join(Organization, PersonOrg.org_id == Organization.id)
            .where(
                Organization.name_normalized.like(needle),
                Person.merged_into_id.is_(None),
                in_scope,
            )
            .distinct()
        )
    persons = (await session.execute(stmt.limit(SEARCH_LIMIT))).scalars().all()
    orgs = await _orgs_by_person(session, [p.id for p in persons])
    return {
        "items": [
            {"id": p.id, "name": p.name, "org": (orgs.get(p.id) or [{}])[0].get("name")}
            for p in persons
        ]
    }


@router.get("/persons/{person_id}")
async def person_detail(
    person_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    person = await session.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="person 不存在")

    orgs = (await _orgs_by_person(session, [person_id])).get(person_id, [])
    tags = (
        await session.execute(
            select(PersonResearchTag.tag).where(PersonResearchTag.person_id == person_id)
        )
    ).scalars().all()
    papers = (
        await session.execute(
            select(Paper)
            .join(PaperAuthor, PaperAuthor.paper_id == Paper.id)
            .where(PaperAuthor.person_id == person_id)
            .order_by(Paper.published_at.desc().nulls_last())
        )
    ).scalars().all()

    # 合作伙伴（按强度降序 Top 20）：对方名字/机构 + 关系摘要，
    # 供详情面板直接点开证据链（替代瞄准细边点击）
    rel_rows = (
        await session.execute(
            select(Relationship, Person)
            .join(
                Person,
                or_(
                    Person.id == Relationship.person_a_id,
                    Person.id == Relationship.person_b_id,
                ),
            )
            .where(
                or_(
                    Relationship.person_a_id == person_id,
                    Relationship.person_b_id == person_id,
                ),
                Person.id != person_id,
            )
            .order_by(Relationship.strength.desc())
            .limit(20)
        )
    ).all()
    partner_orgs = await _orgs_by_person(session, [p.id for _, p in rel_rows])

    return {
        "id": person.id,
        "name": person.name,
        "openalex_id": person.openalex_id,
        "orgs": orgs,
        "research_tags": sorted(tags),
        "partners": [
            {
                "relationship_id": rel.id,
                "person_id": p.id,
                "name": p.name,
                "org": partner_orgs.get(p.id, [{}])[0].get("name"),
                "coop_count": rel.coop_count,
                "strength": float(rel.strength or 0),
                "summary": rel.evidence_summary,
            }
            for rel, p in rel_rows
        ],
        "papers": [
            {
                "id": p.id,
                "arxiv_id": p.arxiv_id,
                "title": p.title,
                "year": _year(p.published_at),
                "directions": p.directions or [],
                "tracks": p.tracks or [],
            }
            for p in papers
        ],
    }


@router.get("/relationships/{rel_id}/evidence")
async def relationship_evidence(
    rel_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    rel = await session.get(Relationship, rel_id)
    if rel is None:
        raise HTTPException(status_code=404, detail="relationship 不存在")
    papers = (
        await session.execute(
            select(Paper)
            .join(RelationshipEvidence, RelationshipEvidence.paper_id == Paper.id)
            .where(RelationshipEvidence.relationship_id == rel_id)
            .order_by(Paper.published_at.desc().nulls_last())
        )
    ).scalars().all()
    return {
        "relationship_id": rel_id,
        "items": [
            {"paper_id": p.id, "arxiv_id": p.arxiv_id, "title": p.title, "year": _year(p.published_at)}
            for p in papers
        ],
    }
