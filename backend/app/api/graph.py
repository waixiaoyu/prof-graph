"""T15：图谱与查询 API（FR-5.1~5.5）。

- GET /api/graph —— direction/track/strength_min/limit 过滤，strength 降序 Top 1000
- GET /api/persons/search —— 姓名/机构 LIKE，限 20
- GET /api/persons/{id} —— 详情（机构/研究方向/论文）
- GET /api/relationships/{id}/evidence —— 证据论文列表（标题/年份）
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
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

router = APIRouter(prefix="/api")

SEARCH_LIMIT = 20


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
    limit: int = Query(1000, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """合作图谱：edges 按 strength 降序取 Top limit，nodes 为涉及的人。"""
    stmt = select(Relationship).where(
        Relationship.type == "paper_cooperation",
        Relationship.strength >= strength_min,
    )
    if direction or track:
        conds = []
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
    rels = (
        await session.execute(stmt.order_by(Relationship.strength.desc()).limit(limit))
    ).scalars().all()

    node_ids = sorted({p for r in rels for p in (r.person_a_id, r.person_b_id)})
    persons = {
        p.id: p
        for p in (
            await session.execute(select(Person).where(Person.id.in_(node_ids)))
        ).scalars()
    }
    orgs = await _orgs_by_person(session, node_ids)

    # 节点方向/赛道/论文数（从其论文聚合，供前端高亮子网）
    paper_rows = (
        await session.execute(
            select(
                PaperAuthor.person_id,
                Paper.directions,
                Paper.tracks,
            )
            .join(Paper, PaperAuthor.paper_id == Paper.id)
            .where(PaperAuthor.person_id.in_(node_ids))
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
    """姓名/机构搜索（FR-5.4）。LIKE 前缀模糊，限 20。"""
    if type == "name":
        # name_normalized 无空格，查询词同步去空格（"wei zh" → "weizh"）
        needle = f"%{q.lower().replace(' ', '')}%"
        stmt = select(Person).where(Person.name_normalized.like(needle))
    else:
        needle = f"%{q.lower()}%"
        stmt = (
            select(Person)
            .join(PersonOrg, PersonOrg.person_id == Person.id)
            .join(Organization, PersonOrg.org_id == Organization.id)
            .where(Organization.name_normalized.like(needle))
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

    return {
        "id": person.id,
        "name": person.name,
        "openalex_id": person.openalex_id,
        "orgs": orgs,
        "research_tags": sorted(tags),
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
