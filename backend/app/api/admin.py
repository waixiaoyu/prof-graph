"""T17：管理端 API（FR-1.4，NFR-3，AC-7）。

- POST /api/admin/trigger-update —— 进行中返回 409，否则后台启动管线返回 batch_id
- GET  /api/admin/update-status/{batch_id} —— 批次进度（阶段/计数）
- GET  /api/admin/metrics —— 当日/本周 token 用量、failed_jobs、熔断状态
- GET  /api/admin/integrity —— 数据不变量巡检（C1-C10 防护网，只读）
- POST /api/admin/breaker/resume —— 管理员手动放行熔断（当日有效）
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import FailedJob, TokenUsage, WebPage
from app.services import breaker
from app.services.integrity import check_integrity
from app.services.news_collector import rss_source_states
from app.services.pipeline import SCOPES, get_batch, trigger_pipeline
from app.sources_config import load_sources

router = APIRouter(prefix="/api/admin")


async def _crawl_seed_states(session: AsyncSession) -> list[dict]:
    """各爬取种子的最近状态（M2-T13，NFR-3）：页面数/抽取进度/最近抓取时间。

    按 sources.yaml 种子列表输出（从未爬取的种子 pages=0），聚合 web_pages。
    """
    rows = {
        seed_id: (pages, extracted, failed, last)
        for seed_id, pages, extracted, failed, last in (
            await session.execute(
                select(
                    WebPage.seed_id,
                    func.count(),
                    func.sum(case((WebPage.status == "extracted", 1), else_=0)),
                    func.sum(case((WebPage.status == "extraction_failed", 1), else_=0)),
                    func.max(WebPage.fetched_at),
                ).group_by(WebPage.seed_id)
            )
        ).all()
    }
    states = []
    for seed in load_sources().seeds:
        pages, extracted, failed, last = rows.get(seed.id, (0, 0, 0, None))
        states.append(
            {
                "id": seed.id,
                "url": seed.url,
                "school": seed.school,
                "pages": int(pages),
                "extracted": int(extracted),
                "failed": int(failed),
                "last_fetch": last.isoformat() if last else None,
            }
        )
    return states


@router.post("/trigger-update")
async def trigger_update(scope: str | None = None) -> dict:
    """scope=None 全链；scope="crawl" 学术传承子链（M2-T8）。"""
    if scope is not None and scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"未知 scope: {scope}（可用：{', '.join(SCOPES)}）")
    batch_id = trigger_pipeline(scope=scope)
    if batch_id is None:
        raise HTTPException(status_code=409, detail="已有采集批次在执行")
    return {"batch_id": batch_id, "scope": scope}


@router.get("/update-status/{batch_id}")
async def update_status(batch_id: str) -> dict:
    batch = get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="未知批次")
    return batch.as_dict()


@router.get("/metrics")
async def metrics(session: AsyncSession = Depends(get_session)) -> dict:
    status = await breaker.get_status(session)
    jobs = (
        await session.execute(
            select(FailedJob)
            .where(FailedJob.status != "done")
            .order_by(FailedJob.id.desc())
            .limit(100)
        )
    ).scalars().all()
    rss_states = rss_source_states()
    # 当日 token 按 job_type 分解（M2 新链路 page_extract/news_extract 的用量可见）
    # 日界与 breaker 一致（UTC）
    today = dt.datetime.now(dt.timezone.utc).date()
    by_job_type = {
        job_type: int(tokens)
        for job_type, tokens in (
            await session.execute(
                select(
                    TokenUsage.job_type,
                    func.sum(TokenUsage.input_tokens + TokenUsage.output_tokens),
                )
                .where(TokenUsage.day == today)
                .group_by(TokenUsage.job_type)
            )
        ).all()
    }
    return {
        "token_usage": {
            "daily_used": status.daily_used,
            "daily_budget": status.daily_budget,
            "weekly_used": status.weekly_used,
            "weekly_budget": status.weekly_budget,
            "by_job_type": by_job_type,
        },
        "rss_sources": {
            "disabled": [s["id"] for s in rss_states if s["disabled"]],
            "sources": rss_states,
        },
        "crawl_seeds": {"seeds": await _crawl_seed_states(session)},
        "breaker": {
            "level": status.level,
            "manual_override_until": (
                status.manual_override_until.isoformat()
                if status.manual_override_until
                else None
            ),
        },
        "failed_jobs": [
            {
                "id": j.id,
                "job_type": j.job_type,
                "target": j.target[:200],
                "attempt": j.attempt,
                "status": j.status,
                "next_retry_at": j.next_retry_at.isoformat() if j.next_retry_at else None,
                "error": (j.error or "")[:300],
            }
            for j in jobs
        ],
    }


@router.get("/integrity")
async def integrity(session: AsyncSession = Depends(get_session)) -> dict:
    """数据不变量巡检（C1-C10，详见 app/services/integrity.py），只读。"""
    return await check_integrity(session)


@router.post("/breaker/resume")
async def breaker_resume() -> dict:
    until = breaker.manual_resume()
    return {"resumed": True, "override_until": until.isoformat()}
