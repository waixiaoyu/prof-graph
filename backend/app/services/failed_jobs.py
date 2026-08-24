"""failed_jobs 重试调度（T13，FR-2.5）。

退避序列：1 / 5 / 25 分钟；同一 (job_type, target) 反复失败超过 3 次进死信
（status=dead，不再自动重试，后台可见，可经 CLI 手动重跑）。
重试执行器按 job_type 回调对应服务：
- rss_fetch → 重新拉取该分类 RSS 并入库
- ai_fine_filter → 对 target 中的 arxiv_id 列表重跑细筛
- glm_extract → 对 target 论文重跑抽取
"""
from __future__ import annotations

import datetime as dt
import json
import logging

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FailedJob, Paper

log = logging.getLogger("prof-graph.failed_jobs")

BACKOFF_MINUTES = (1, 5, 25)
MAX_ATTEMPTS = len(BACKOFF_MINUTES)


async def schedule_retry(
    session: AsyncSession, job_type: str, target: str, error: str
) -> FailedJob:
    """记录一次失败并安排下次重试；超过上限转死信。同目标复用已有行。"""
    now = dt.datetime.now(dt.timezone.utc)
    job = (
        await session.execute(
            select(FailedJob).where(
                FailedJob.job_type == job_type,
                FailedJob.target == target,
                FailedJob.status == "retrying",
            )
        )
    ).scalar_one_or_none()

    if job is None:
        job = FailedJob(job_type=job_type, target=target, attempt=0, error=error)

    job.attempt += 1
    job.error = error
    if job.attempt > MAX_ATTEMPTS:
        job.status = "dead"
        job.next_retry_at = None
        log.error("任务进死信：%s/%s（已尝试 %d 次）", job_type, target, MAX_ATTEMPTS)
    else:
        job.status = "retrying"
        job.next_retry_at = now + dt.timedelta(minutes=BACKOFF_MINUTES[job.attempt - 1])
    session.add(job)
    await session.flush()
    return job


# ---------- 重试执行器（T13） ----------


class RetryExecutor:
    """按 job_type 回调对应服务。glm/http 可注入以便单测。"""

    def __init__(self, glm=None, http: httpx.AsyncClient | None = None) -> None:
        self._glm = glm
        self._http = http

    def _http_client(self) -> httpx.AsyncClient:
        return self._http or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": "prof-graph/0.1"},
            follow_redirects=True,
        )

    def _glm_client(self):
        if self._glm is None:
            from app.services.glm import GLMClient

            self._glm = GLMClient()
        return self._glm

    async def execute(self, session: AsyncSession, job: FailedJob) -> bool:
        """执行单个任务。返回是否成功（成功由调用方置 done）。"""
        from app.services.ai_filter import run_filter
        from app.services.collector import fetch_category, ingest_papers
        from app.services.extractor import run_extraction

        if job.job_type == "rss_fetch":
            client = self._http_client()
            rows = await fetch_category(client, job.target)
            await ingest_papers(session, rows)
        elif job.job_type == "ai_fine_filter":
            arxiv_ids = json.loads(job.target)
            papers = (
                await session.execute(
                    select(Paper.id).where(Paper.arxiv_id.in_(arxiv_ids))
                )
            ).scalars().all()
            await run_filter(session, self._glm_client(), paper_ids=list(papers))
        elif job.job_type == "glm_extract":
            try:
                arxiv_ids = json.loads(job.target)
                if not isinstance(arxiv_ids, list):
                    arxiv_ids = [job.target]
            except json.JSONDecodeError:
                arxiv_ids = [job.target]
            papers = (
                await session.execute(
                    select(Paper.id).where(Paper.arxiv_id.in_(arxiv_ids))
                )
            ).scalars().all()
            report = await run_extraction(
                session, self._glm_client(), paper_ids=list(papers),
                http=self._http_client(),
            )
            if report.failed:
                raise RuntimeError(f"重试仍失败（{report.failed} 篇）")
        else:
            raise ValueError(f"未知 job_type: {job.job_type}")
        return True


async def _process_job(session: AsyncSession, executor: RetryExecutor, job: FailedJob) -> None:
    # rollback 会 expire 实例，之后再同步访问属性会触发隐式刷新（asyncio 下报
    # MissingGreenlet），先快照标识字段
    job_type, target = job.job_type, job.target
    try:
        ok = await executor.execute(session, job)
    except Exception as e:  # noqa: BLE001 — 重试执行器要兜住一切异常
        await session.rollback()
        await schedule_retry(session, job_type, target, f"{type(e).__name__}: {e}")
        await session.commit()
        return
    if ok:
        job.status = "done"
        job.next_retry_at = None
        await session.commit()


async def scan_and_retry(
    session: AsyncSession, executor: RetryExecutor | None = None
) -> dict[str, int]:
    """扫描到期的 retrying 任务并执行。返回统计。"""
    executor = executor or RetryExecutor()
    now = dt.datetime.now(dt.timezone.utc)
    jobs = (
        await session.execute(
            select(FailedJob).where(
                FailedJob.status == "retrying",
                FailedJob.next_retry_at <= now,
            )
        )
    ).scalars().all()

    stats = {"scanned": len(jobs), "done": 0, "rescheduled": 0, "dead": 0}
    for job in jobs:
        before = job.attempt
        await _process_job(session, executor, job)
        await session.refresh(job)
        if job.status == "done":
            stats["done"] += 1
        elif job.status == "dead":
            stats["dead"] += 1
        else:
            stats["rescheduled"] += 1
            assert job.attempt == before + 1
    return stats


async def rerun_dead(
    session: AsyncSession, job_id: int | None = None, executor: RetryExecutor | None = None
) -> dict[str, int]:
    """死信手动重跑（CLI：scripts/retry_failed.py）。job_id 缺省重跑全部 dead。"""
    executor = executor or RetryExecutor()
    stmt = select(FailedJob).where(FailedJob.status == "dead")
    if job_id is not None:
        stmt = stmt.where(FailedJob.id == job_id)
    jobs = (await session.execute(stmt)).scalars().all()

    stats = {"rerun": len(jobs), "done": 0, "still_dead": 0}
    for job in jobs:
        job.status = "retrying"  # 临时置回，走统一失败路径
        await _process_job(session, executor, job)
        await session.refresh(job)
        if job.status == "done":
            stats["done"] += 1
        else:
            stats["still_dead"] += 1
    return stats
