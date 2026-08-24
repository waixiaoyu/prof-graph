"""采集管线编排（T14 起用）+ 批次状态跟踪（T17 update-status 用）。

全链：采集 → AI 过滤 → 方向打标 → GLM 抽取 → OpenAlex 补全 → 消歧 → 建关系。
每个阶段对熔断/失败自愈（细筛熔断放行、抽取熔断跳过、单点失败走 failed_jobs），
管线本身不中断。glm transport / http client 可注入以便集成测试。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from app.services.ai_filter import run_filter
from app.services.collector import collect_all
from app.services.disambiguator import run_disambiguation
from app.services.extractor import run_extraction
from app.services.linker import run_linker
from app.services.openalex import enrich_papers
from app.services.tagger import run_tagger

log = logging.getLogger("prof-graph.pipeline")

STAGES = ["collect", "filter", "tag", "extract", "openalex", "disambiguate", "link"]


@dataclass
class BatchStatus:
    batch_id: str
    started_at: datetime
    stage: str = "init"
    running: bool = True
    error: str | None = None
    counts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "started_at": self.started_at.isoformat(),
            "stage": self.stage,
            "running": self.running,
            "error": self.error,
            "counts": self.counts,
        }


# 进程内批次表（单进程部署；T17 查询用）
_batches: dict[str, BatchStatus] = {}
PIPELINE_LOCK = asyncio.Lock()


def get_batch(batch_id: str) -> BatchStatus | None:
    return _batches.get(batch_id)


async def run_pipeline(
    session: AsyncSession,
    glm=None,
    http=None,
    categories: tuple[str, ...] | None = None,
    batch_id: str | None = None,
) -> BatchStatus:
    """执行全链管线。同一时刻只允许一个批次（T17 并发触发 409 语义）。"""
    async with PIPELINE_LOCK:
        batch = BatchStatus(
            batch_id=batch_id or uuid.uuid4().hex[:8],
            started_at=datetime.now(timezone.utc),
        )
        _batches[batch.batch_id] = batch
        try:
            for stage in STAGES:
                batch.stage = stage
                if stage == "collect":
                    report = await collect_all(session, client=http, categories=categories)
                    batch.counts["collect"] = {
                        "added": report.added,
                        "skipped": report.skipped,
                        "failed_categories": report.categories_failed,
                    }
                elif stage == "filter":
                    if glm is None:
                        from app.services.glm import GLMClient

                        glm = GLMClient()
                    report = await run_filter(session, glm)
                    batch.counts["filter"] = {
                        "kept_by_rule": report.kept_by_rule,
                        "dropped_by_rule": report.dropped_by_rule,
                        "ai_by_glm": report.ai_by_glm,
                        "dropped_by_glm": report.dropped_by_glm,
                        "passed_by_breaker": report.passed_by_breaker,
                    }
                elif stage == "tag":
                    report = await run_tagger(session)
                    batch.counts["tag"] = {"tagged": report.tagged}
                elif stage == "extract":
                    report = await run_extraction(session, glm, http=http)
                    batch.counts["extract"] = {
                        "extracted": report.extracted,
                        "failed": report.failed,
                        "breaker_skipped": report.breaker_skipped,
                    }
                elif stage == "openalex":
                    enriched = await enrich_papers(session, http=http)
                    batch.counts["openalex"] = {"enriched": enriched}
                elif stage == "disambiguate":
                    stats = await run_disambiguation(session)
                    batch.counts["disambiguate"] = stats
                elif stage == "link":
                    report = await run_linker(session)
                    batch.counts["link"] = report
            batch.stage = "done"
        except Exception as e:  # noqa: BLE001 — 管线级兜底，错误进批次状态
            batch.error = f"{type(e).__name__}: {e}"
            batch.stage = "error"
            log.exception("管线批次 %s 失败于 %s", batch.batch_id, batch.stage)
        finally:
            batch.running = False
        return batch


def is_pipeline_running() -> bool:
    """是否有批次正在执行（T17 触发 409 判断；锁非阻塞探测）。"""
    return PIPELINE_LOCK.locked()


# 触发瞬间到锁获取之间的原子护栏（asyncio 单线程，无 await 竞态窗口）
_trigger_guard = False


def trigger_pipeline(session_factory=None) -> str | None:
    """非阻塞启动管线（T17）。返回 batch_id；已在执行返回 None。

    session_factory 可注入（测试用测试库会话工厂）。
    """
    global _trigger_guard
    if PIPELINE_LOCK.locked() or _trigger_guard:
        return None
    _trigger_guard = True

    from app.db import SessionLocal

    factory = session_factory or SessionLocal
    batch_id = uuid.uuid4().hex[:8]

    async def _run() -> None:
        global _trigger_guard
        try:
            async with factory() as session:
                await run_pipeline(session, batch_id=batch_id)
        finally:
            _trigger_guard = False

    asyncio.create_task(_run())
    return batch_id
