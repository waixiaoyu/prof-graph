"""failed_jobs 重试调度（T9 起用，T13 扫描器复用）。

退避序列（plan FR-2.5）：1 / 5 / 25 分钟；同一 (job_type, target) 反复失败
超过 3 次进死信（status=dead，不再自动重试，后台可见）。
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FailedJob

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
