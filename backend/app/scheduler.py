"""APScheduler 任务注册（T14，FR-1.1）。

① 采集管线：cron 03:00 + jitter 1200s（本地时区 Asia/Shanghai）
② failed_jobs 重试扫描：interval 60s
③ 死信巡检：cron 08:00（记日志，死信本身经 scripts/retry_failed.py 手动重跑）

 constitution 锁定 APScheduler（应用内调度，禁用 Celery / 系统 cron）。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

log = logging.getLogger("prof-graph.scheduler")

TZ = "Asia/Shanghai"


async def pipeline_job() -> None:
    """定时全链采集（调度器无请求上下文，自开 session）。"""
    from app.db import SessionLocal
    from app.services.integrity import check_integrity
    from app.services.pipeline import run_pipeline

    async with SessionLocal() as session:
        batch = await run_pipeline(session)
        report = await check_integrity(session)
    if batch.error:
        log.error("定时管线批次 %s 失败：%s", batch.batch_id, batch.error)
    else:
        log.info("定时管线批次 %s 完成：%s", batch.batch_id, batch.counts.get("collect"))
    # 管线后不变量巡检：数据脏了要当天暴露，不能等界面上看出来（linker 膨胀教训）
    if report["ok"]:
        log.info("数据不变量巡检通过（C1-C10）")
    else:
        for c in report["checks"]:
            if c["violations"]:
                log.warning(
                    "数据不变量违例 %s：%d 处，样本 %s（GET /api/admin/integrity 查看全量）",
                    c["check"], c["violations"], c["sample"],
                )


async def retry_scan_job() -> None:
    from app.db import SessionLocal
    from app.services.failed_jobs import RetryExecutor, scan_and_retry

    async with SessionLocal() as session:
        stats = await scan_and_retry(session, RetryExecutor())
    if stats["scanned"]:
        log.info("重试扫描：%s", stats)


async def dead_letter_patrol_job() -> None:
    """死信巡检：只记日志提醒（重跑走 CLI / 后台）。"""
    from app.db import SessionLocal
    from app.models import FailedJob

    async with SessionLocal() as session:
        dead = (
            await session.execute(
                select(FailedJob.id, FailedJob.job_type, FailedJob.target).where(
                    FailedJob.status == "dead"
                )
            )
        ).all()
    if dead:
        log.warning(
            "当前死信 %d 条：%s（可用 scripts/retry_failed.py 重跑）",
            len(dead),
            [(j.job_type, j.target) for j in dead[:5]],
        )


async def backup_job() -> None:
    """每日全库备份（防护网）：管线之前留一份跑前快照，失败记 ERROR。"""
    from app.services.backup import run_backup

    try:
        dest = await run_backup()
        log.info("每日备份完成：%s", dest)
    except Exception:  # noqa: BLE001 — 备份失败必须显式报警，不影响其他任务
        log.exception("每日备份失败")


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=TZ)
    # 02:00 备份（早于 03:00±20min 的管线，库小秒级完成，不与管线重叠）
    scheduler.add_job(
        backup_job,
        CronTrigger(hour=2, minute=0, jitter=300, timezone=TZ),
        id="daily_backup",
        name="每日全库备份（02:00 ± 5min，pg_dump 滚动保留 7 份）",
        replace_existing=True,
    )
    scheduler.add_job(
        pipeline_job,
        CronTrigger(hour=3, minute=0, jitter=1200, timezone=TZ),
        id="daily_pipeline",
        name="每日采集管线（03:00 ± 20min）",
        replace_existing=True,
    )
    scheduler.add_job(
        retry_scan_job,
        IntervalTrigger(seconds=60),
        id="failed_retry_scan",
        name="failed_jobs 重试扫描（60s）",
        replace_existing=True,
    )
    scheduler.add_job(
        dead_letter_patrol_job,
        CronTrigger(hour=8, minute=0, timezone=TZ),
        id="dead_letter_patrol",
        name="死信巡检（08:00）",
        replace_existing=True,
    )
    return scheduler
