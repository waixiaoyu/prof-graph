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
from app.services.cn_scope import flag_papers
from app.services.disambiguator import run_disambiguation
from app.services.extractor import run_extraction
from app.services.linker import run_linker
from app.services.mentor_linker import run_mentor_link
from app.services.openalex import enrich_papers
from app.services.tagger import run_tagger

log = logging.getLogger("prof-graph.pipeline")

# cn_scope 在 openalex 之后：GLM+OpenAlex 机构信号齐了再判定（M1 范围约束）
# crawl/mentor_link 排论文链路后（M2-T8）：爬取的成员消歧可命中论文管线产出的人
STAGES = [
    "collect", "filter", "tag", "extract", "openalex", "cn_scope", "disambiguate", "link",
    "crawl", "mentor_link", "news_collect", "news_link",
]
# trigger-update 的 scope 子集（M2-T8/T12）：crawl = 学术传承链；news = 资讯项目链。
# plan 的 news_extract/project_link 合并为单阶段 news_link：抽取结果无中间
# 存储，逐条抽取即建链，(rel,news) 证据幂等保证重跑安全。
SCOPES = {"crawl": ["crawl", "mentor_link"], "news": ["news_collect", "news_link"]}


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
    scope: str | None = None,
) -> BatchStatus:
    """执行管线（scope=None 全链；scope="crawl" 只跑学术传承链）。

    同一时刻只允许一个批次（T17 并发触发 409 语义）。
    """
    if scope is not None and scope not in SCOPES:
        raise ValueError(f"未知 scope: {scope}（可用：{', '.join(SCOPES)}）")
    stages = SCOPES.get(scope, STAGES)
    async with PIPELINE_LOCK:
        batch = BatchStatus(
            batch_id=batch_id or uuid.uuid4().hex[:8],
            started_at=datetime.now(timezone.utc),
        )
        _batches[batch.batch_id] = batch
        try:
            if glm is None and any(s in ("filter", "extract", "mentor_link", "news_link") for s in stages):
                from app.services.glm import GLMClient

                glm = GLMClient()
            for stage in stages:
                batch.stage = stage
                if stage == "collect":
                    report = await collect_all(session, client=http, categories=categories)
                    batch.counts["collect"] = {
                        "added": report.added,
                        "skipped": report.skipped,
                        "failed_categories": report.categories_failed,
                    }
                elif stage == "filter":
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
                elif stage == "cn_scope":
                    batch.counts["cn_scope"] = await flag_papers(session)
                elif stage == "disambiguate":
                    stats = await run_disambiguation(session)
                    batch.counts["disambiguate"] = stats
                elif stage == "link":
                    report = await run_linker(session)
                    batch.counts["link"] = report
                elif stage == "crawl":
                    from app.services.crawler import Crawler

                    report = await Crawler(http=http).run(session)
                    batch.counts["crawl"] = {
                        "pages_new": report.pages_new,
                        "pages_changed": report.pages_changed,
                        "pages_unchanged": report.pages_unchanged,
                        "failed": report.failed,
                    }
                elif stage == "news_collect":
                    from app.services.news_collector import collect_news

                    report = await collect_news(session, http=http)
                    batch.counts["news_collect"] = {
                        "sources_ok": report.sources_ok,
                        "sources_failed": report.sources_failed,
                        "sources_skipped_disabled": report.sources_skipped_disabled,
                        "added": report.added,
                        "skipped_dup": report.skipped_dup,
                        "screened_no_signal": report.screened_no_signal,
                    }
                elif stage == "news_link":
                    from app.services.project_linker import run_news_link

                    report = await run_news_link(session, glm)
                    batch.counts["news_link"] = {
                        "items_extracted": report.items_extracted,
                        "items_no_signal": report.items_no_signal,
                        "items_failed": report.items_failed,
                        "breaker_skipped": report.breaker_skipped,
                        "pages_extracted": report.pages_extracted,
                        "pairs_created": report.pairs_created,
                        "pairs_merged": report.pairs_merged,
                        "pairs_dup": report.pairs_dup,
                    }
                elif stage == "mentor_link":
                    report = await run_mentor_link(session, glm)
                    batch.counts["mentor_link"] = {
                        "pages_extracted": report.pages_extracted,
                        "pages_no_signal": report.pages_no_signal,
                        "pages_failed": report.pages_failed,
                        "breaker_skipped": report.breaker_skipped,
                        "pairs_created": report.pairs_created,
                        "pairs_merged": report.pairs_merged,
                        "pairs_dup": report.pairs_dup,
                    }
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


def trigger_pipeline(session_factory=None, scope: str | None = None) -> str | None:
    """非阻塞启动管线（T17）。返回 batch_id；已在执行返回 None。

    session_factory 可注入（测试用测试库会话工厂）；scope 透传子链选择。
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
                await run_pipeline(session, batch_id=batch_id, scope=scope)
        finally:
            _trigger_guard = False

    asyncio.create_task(_run())
    return batch_id
