"""T4：/api/filters/options —— 筛选器选项（FR-5.2）。

2026-08-31 增补：orgs —— M1 范围内人员数 Top 50 机构（机构切入用）。
M2-T0 增补：relationship_types —— 关系类型固定三项（RD-M2-13：图谱边样式统一，
类型靠筛选与详情区分）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.graph import _in_scope_person_ids
from app.config import load_directions
from app.db import get_session
from app.models import Organization, PersonOrg

router = APIRouter(prefix="/api")

# 关系类型（M2 固定三项；relationships.type 的全集）
RELATIONSHIP_TYPES = [
    {"id": "paper_cooperation", "name": "论文合作"},
    {"id": "academic_mentorship", "name": "学术传承"},
    {"id": "project_cooperation", "name": "项目合作"},
]


@router.get("/filters/options")
async def filter_options(session: AsyncSession = Depends(get_session)) -> dict:
    cfg = load_directions()
    orgs = (
        await session.execute(
            select(Organization.name)
            .join(PersonOrg, PersonOrg.org_id == Organization.id)
            .where(PersonOrg.person_id.in_(_in_scope_person_ids()))
            .group_by(Organization.name)
            .order_by(func.count(PersonOrg.person_id).desc())
            .limit(50)
        )
    ).scalars().all()
    return {
        "directions": [{"id": d.id, "name": d.name} for d in cfg.directions],
        "tracks": [{"id": t.id, "name": t.name} for t in cfg.tracks],
        "arxiv_categories": list(cfg.arxiv_categories),
        "orgs": list(orgs),
        "relationship_types": RELATIONSHIP_TYPES,
    }
