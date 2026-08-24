"""业务方向打标器（T8，FR-1.3）。

对 AI 过滤后论文的标题+摘要按 directions.yaml 关键词匹配（不区分大小写），
写 papers.directions / papers.tracks。一篇可命中多个方向和赛道；
无命中保持空数组。被剔除（filtered_out）的论文不处理。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DirectionsConfig, load_directions
from app.models import Paper


@dataclass
class TagReport:
    tagged: int = 0
    untouched: int = 0


def _match_tags(paper: Paper, config: DirectionsConfig) -> tuple[list[str], list[str]]:
    text = f"{paper.title} {paper.abstract or ''}".lower()
    directions = [r.id for r in config.directions if any(k in text for k in r.keywords)]
    tracks = [r.id for r in config.tracks if any(k in text for k in r.keywords)]
    return directions, tracks


async def run_tagger(
    session: AsyncSession,
    paper_ids: list[int] | None = None,
    config: DirectionsConfig | None = None,
) -> TagReport:
    """给 AI 相关论文打方向/赛道标签。paper_ids 缺省处理全部 AI 相关论文。"""
    cfg = config or load_directions()
    report = TagReport()

    stmt = select(Paper).where(Paper.ai_relevant.is_(True))
    if paper_ids is not None:
        stmt = stmt.where(Paper.id.in_(paper_ids))

    papers = (await session.execute(stmt)).scalars().all()
    for paper in papers:
        directions, tracks = _match_tags(paper, cfg)
        if directions or tracks:
            paper.directions = directions
            paper.tracks = tracks
            report.tagged += 1
        else:
            report.untouched += 1

    await session.commit()
    return report
