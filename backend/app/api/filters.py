"""T4：/api/filters/options —— 筛选器选项（FR-5.2）。

2026-08-31 增补：orgs —— M1 范围内人员数 Top 50 机构（机构切入用）。
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
    }
