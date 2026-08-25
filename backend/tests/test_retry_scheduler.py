"""T13/T14 单测：退避序列 / 死信 / CLI 重跑 / 调度注册 / 管线全链（FR-2.5，FR-1.1）。"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys

import httpx
import pytest
import respx
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.models import FailedJob, Paper, Person, Relationship
from app.services.failed_jobs import (
    BACKOFF_MINUTES,
    RetryExecutor,
    rerun_dead,
    schedule_retry,
    scan_and_retry,
)

NOW = dt.datetime.now(dt.timezone.utc)
RSS_XML = """<?xml version="1.0"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel><title>t</title>
    <item>
      <title>LLM Agent Paper</title>
      <link>http://arxiv.org/abs/2608.06001v1</link>
      <description>We study AI agents.</description>
      <dc:creator>Wei Zhang (PKU)</dc:creator>
      <dc:date>2026-08-20T00:00:00Z</dc:date>
      <category>cs.AI</category>
    </item>
  </channel>
</rss>
"""


# ---------- T13：退避与死信 ----------

async def test_backoff_sequence_1_5_25_then_dead(db_session) -> None:
    """失败间隔 1/5/25 分钟；3 次后 dead。"""
    job = None
    deltas = []
    for i in range(4):
        t0 = dt.datetime.now(dt.timezone.utc)
        job = await schedule_retry(db_session, "rss_fetch", "cs.NI", "err")
        if i < 3:
            assert job.status == "retrying"
            assert job.next_retry_at is not None
            deltas.append((job.next_retry_at - t0).total_seconds() / 60)
        else:
            assert job.status == "dead"
    assert job.next_retry_at is None
    assert [round(d) for d in deltas] == list(BACKOFF_MINUTES)


async def test_schedule_retry_heals_duplicate_rows(db_session) -> None:
    """同 (job_type,target) 存在多行脏数据时不再抛 MultipleResultsFound：
    保留最老一行并累加 attempt，其余重复行删除。"""
    now = dt.datetime.now(dt.timezone.utc)
    for attempt in (1, 1, 1):
        db_session.add(
            FailedJob(
                job_type="rss_fetch",
                target="eess.IT",
                attempt=attempt,
                next_retry_at=now,
                error="历史脏数据",
            )
        )
    await db_session.commit()

    job = await schedule_retry(db_session, "rss_fetch", "eess.IT", "HTTPStatusError: 400")
    await db_session.commit()

    rows = (
        await db_session.execute(
            select(FailedJob).where(
                FailedJob.job_type == "rss_fetch", FailedJob.target == "eess.IT"
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].attempt == 2
    assert "400" in rows[0].error


@respx.mock
async def test_scan_and_retry_rss_fetch_success(db_session) -> None:
    """到期 rss_fetch 任务重试成功 → done，论文入库。"""
    respx.get("https://export.arxiv.org/rss/cs.NI").mock(
        return_value=httpx.Response(200, text=RSS_XML)
    )
    db_session.add(FailedJob(job_type="rss_fetch", target="cs.NI", attempt=1,
                             next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    stats = await scan_and_retry(db_session, RetryExecutor(http=httpx.AsyncClient()))

    assert stats == {"scanned": 1, "done": 1, "rescheduled": 0, "dead": 0}
    job = (await db_session.execute(select(FailedJob))).scalars().one()
    assert job.status == "done" and job.next_retry_at is None
    papers = (await db_session.execute(select(Paper))).scalars().all()
    assert len(papers) == 1 and papers[0].arxiv_id == "2608.06001v1"


@respx.mock
async def test_scan_and_retry_failure_reschedules(db_session) -> None:
    """重试仍失败 → attempt+1、下次退避 5 分钟（第 2 次）。"""
    respx.get("https://export.arxiv.org/rss/cs.NI").mock(
        return_value=httpx.Response(500)
    )
    db_session.add(FailedJob(job_type="rss_fetch", target="cs.NI", attempt=1,
                             next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    stats = await scan_and_retry(db_session, RetryExecutor(http=httpx.AsyncClient()))

    assert stats == {"scanned": 1, "done": 0, "rescheduled": 1, "dead": 0}
    job = (await db_session.execute(select(FailedJob))).scalars().one()
    assert job.attempt == 2 and job.status == "retrying"
    minutes = (job.next_retry_at - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60
    assert 4 < minutes <= 5


@respx.mock
async def test_rerun_dead_success(db_session) -> None:
    """死信经 rerun_dead（CLI scripts/retry_failed.py 调用的服务函数）重跑 → done。"""
    respx.get("https://export.arxiv.org/rss/cs.NI").mock(
        return_value=httpx.Response(200, text=RSS_XML)
    )
    db_session.add(FailedJob(job_type="rss_fetch", target="cs.NI", attempt=4,
                             status="dead", error="e"))
    await db_session.commit()

    stats = await rerun_dead(db_session, executor=RetryExecutor(http=httpx.AsyncClient()))

    assert stats == {"rerun": 1, "done": 1, "still_dead": 0}
    job = (await db_session.execute(select(FailedJob))).scalars().one()
    assert job.status == "done"


def test_retry_failed_cli_help() -> None:
    """CLI 入口可执行（--help 退出码 0，验证 import/argparse 接线）。"""
    proc = subprocess.run(
        [sys.executable, "scripts/retry_failed.py", "--help"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--job-id" in proc.stdout


# ---------- T14：调度注册 ----------

def test_build_scheduler_registers_three_jobs() -> None:
    from app.scheduler import build_scheduler

    scheduler = build_scheduler()
    jobs = {j.id: j.trigger for j in scheduler.get_jobs()}
    assert set(jobs) == {"daily_pipeline", "failed_retry_scan", "dead_letter_patrol"}

    pipeline = jobs["daily_pipeline"]
    assert isinstance(pipeline, CronTrigger)
    assert "hour='3'" in str(pipeline) and "minute='0'" in str(pipeline)
    assert pipeline.jitter == 1200

    assert isinstance(jobs["failed_retry_scan"], IntervalTrigger)
    assert jobs["failed_retry_scan"].interval == dt.timedelta(seconds=60)

    patrol = jobs["dead_letter_patrol"]
    assert isinstance(patrol, CronTrigger) and "hour='8'" in str(patrol)


# ---------- T14：管线全链集成（fixture + mock） ----------

@respx.mock
async def test_pipeline_full_chain(db_session) -> None:
    """mock RSS + mock GLM + mock OpenAlex：全链跑通出关系（T14 集成验证）。"""
    from app.services.glm import GLMClient, TransportResult
    from app.services.pipeline import run_pipeline

    respx.get("https://export.arxiv.org/rss/cs.AI").mock(
        return_value=httpx.Response(200, text=RSS_XML)
    )
    respx.get("https://arxiv.org/html/2608.06001v1").mock(
        return_value=httpx.Response(404)  # 无 HTML 全文 → 回退标题+摘要
    )
    respx.get(
        "https://api.openalex.org/works/https://doi.org/10.48550/arxiv.2608.06001"
    ).mock(return_value=httpx.Response(404))
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, json={"results": []})  # 标题搜索也无记录
    )

    # cs.AI 分类 → 规则直留，细筛不调 GLM；本 transport 只服务抽取
    extract_response = json.dumps({
        "authors": [
            {"name": "Wei Zhang", "seq": 0, "affiliation": "Peking University"},
            {"name": "Li Wang", "seq": 1, "affiliation": None},
        ],
        "research_tags": ["llm agent"],
    })

    async def transport(system: str, user: str, max_tokens: int) -> TransportResult:
        return TransportResult(extract_response, 500, 800)

    glm = GLMClient(transport=transport)
    http = httpx.AsyncClient()
    batch = await run_pipeline(db_session, glm=glm, http=http, categories=("cs.AI",))
    await http.aclose()

    assert batch.stage == "done", batch.error
    assert batch.counts["collect"]["added"] == 1
    assert batch.counts["extract"]["extracted"] == 1

    papers = (await db_session.execute(select(Paper))).scalars().all()
    assert papers[0].status == "extracted"
    assert papers[0].research_tags == ["llm agent"]

    persons = (await db_session.execute(select(Person))).scalars().all()
    assert len(persons) == 2

    rels = (await db_session.execute(select(Relationship))).scalars().all()
    assert len(rels) == 1 and rels[0].type == "paper_cooperation"
    assert rels[0].coop_count == 1
