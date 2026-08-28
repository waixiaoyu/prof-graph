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


# ---------- 2026-08-27 修复：服务层已记账的失败不重复 schedule_retry ----------
# 生产日志现象：page_extract 死信后"复活"无限重试 + scan 断言崩溃。
# 根因：run_mentor_link/extractor 失败路径已逐项 schedule_retry+commit，执行器
# 抛错后 _process_job 再记一次 → 同一失败 attempt+2、死信行被建成新行。


async def test_retry_page_extract_failure_not_double_booked(db_session) -> None:
    """服务层已记账的重试失败：attempt 只 +1（不再 +2），单行。"""
    from app.models import WebPage
    from app.services.glm import GLMClient, GLMParseError, TransportResult

    url = "https://lab.example.edu/members"
    db_session.add(WebPage(url=url, seed_id="s1", page_type="lab_members",
                            title="成员", status="extraction_failed"))
    db_session.add(FailedJob(job_type="page_extract", target=url, attempt=1,
                             next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    class _Boom:
        async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
            raise GLMParseError("bad json")

    stats = await scan_and_retry(db_session, RetryExecutor(glm=GLMClient(transport=_Boom())))

    assert stats == {"scanned": 1, "done": 0, "rescheduled": 1, "dead": 0}
    jobs = (await db_session.execute(select(FailedJob))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].attempt == 2 and jobs[0].status == "retrying"  # 服务层 +1，执行器不再 +1


async def test_retry_page_extract_dead_stays_dead(db_session) -> None:
    """第 3 次重试失败进死信后：不再复活出新 retrying 行。"""
    from app.models import WebPage
    from app.services.glm import GLMClient, GLMParseError, TransportResult

    url = "https://lab.example.edu/members"
    db_session.add(WebPage(url=url, seed_id="s1", page_type="lab_members",
                            title="成员", status="extraction_failed"))
    db_session.add(FailedJob(job_type="page_extract", target=url, attempt=3,
                             next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    class _Boom:
        async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
            raise GLMParseError("bad json")

    stats = await scan_and_retry(db_session, RetryExecutor(glm=GLMClient(transport=_Boom())))

    assert stats == {"scanned": 1, "done": 0, "rescheduled": 0, "dead": 1}
    jobs = (await db_session.execute(select(FailedJob))).scalars().all()
    assert len(jobs) == 1  # 死信行保留，没有复活的新行
    assert jobs[0].status == "dead" and jobs[0].attempt == 4


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


# ---------- M2 新 job_type 重试分支（web_crawl / news_fetch / news_extract） ----------

RETRY_PAGE_URL = "https://lab.example.edu/members"
RETRY_ROBOTS = "https://lab.example.edu/robots.txt"


@respx.mock
async def test_retry_web_crawl_success(db_session) -> None:
    """web_crawl 失败任务：单页重爬成功 → 快照更新 + 状态回 pending_extraction。"""
    from app.models import WebPage

    respx.get(RETRY_ROBOTS).mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    respx.get(RETRY_PAGE_URL).mock(
        return_value=httpx.Response(200, text="<html><body>新内容 李四 教授</body></html>")
    )
    db_session.add(WebPage(url=RETRY_PAGE_URL, seed_id="s1", page_type="lab_members",
                           title="旧标题", content_text="旧内容", status="extraction_failed"))
    db_session.add(FailedJob(job_type="web_crawl", target=RETRY_PAGE_URL, attempt=1,
                             next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    stats = await scan_and_retry(db_session, RetryExecutor(http=httpx.AsyncClient()))

    assert stats == {"scanned": 1, "done": 1, "rescheduled": 0, "dead": 0}
    page = (await db_session.execute(select(WebPage))).scalar_one()
    assert "新内容" in page.content_text
    assert page.status == "pending_extraction"  # 内容变化触发重抽取
    job = (await db_session.execute(select(FailedJob))).scalars().one()
    assert job.status == "done"


@respx.mock
async def test_retry_web_crawl_failure_reschedules(db_session) -> None:
    """web_crawl 重试仍失败（HTTP 500）→ attempt+1、单行记账。"""
    from app.models import WebPage

    respx.get(RETRY_ROBOTS).mock(return_value=httpx.Response(404))  # robots 不可得 → 允许
    respx.get(RETRY_PAGE_URL).mock(return_value=httpx.Response(500))
    db_session.add(WebPage(url=RETRY_PAGE_URL, seed_id="s1", page_type="lab_members",
                           title="t", content_text="c", status="extraction_failed"))
    db_session.add(FailedJob(job_type="web_crawl", target=RETRY_PAGE_URL, attempt=1,
                             next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    stats = await scan_and_retry(db_session, RetryExecutor(http=httpx.AsyncClient()))

    assert stats["rescheduled"] == 1
    jobs = (await db_session.execute(select(FailedJob))).scalars().all()
    assert len(jobs) == 1 and jobs[0].attempt == 2 and jobs[0].status == "retrying"


@respx.mock
async def test_retry_news_fetch_success(db_session) -> None:
    """news_fetch 失败任务：单源重拉成功 → 资讯入库 + 任务 done。"""
    from app.models import NewsItem
    from app.sources_config import load_sources

    src_url = load_sources().enabled_rss()[0].url  # 执行器按真实配置定位源
    respx.get(src_url).mock(return_value=httpx.Response(200, text=_NEWS_FEED_XML))
    db_session.add(FailedJob(job_type="news_fetch", target=src_url, attempt=1,
                             next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    stats = await scan_and_retry(db_session, RetryExecutor(http=httpx.AsyncClient()))

    assert stats["done"] == 1
    items = (await db_session.execute(select(NewsItem))).scalars().all()
    assert len(items) == 1 and items[0].status == "pending_screen"


@respx.mock
async def test_retry_news_fetch_failure_reschedules(db_session) -> None:
    """news_fetch 重试仍失败 → attempt+1 且单行（collect_news 内部记账与执行器
    记账不叠加：未提交的内部记账被 rollback，由 _process_job 统一补记）。"""
    from app.sources_config import load_sources

    src_url = load_sources().enabled_rss()[0].url
    respx.get(src_url).mock(return_value=httpx.Response(500))
    db_session.add(FailedJob(job_type="news_fetch", target=src_url, attempt=1,
                             next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    stats = await scan_and_retry(db_session, RetryExecutor(http=httpx.AsyncClient()))

    assert stats["rescheduled"] == 1
    jobs = (await db_session.execute(select(FailedJob))).scalars().all()
    assert len(jobs) == 1 and jobs[0].attempt == 2 and jobs[0].status == "retrying"


_NEWS_EXTRACT_JSON = json.dumps({
    "no_signal": False,
    "persons": [{"name": "张伟", "org": "清华大学", "role": "教授"}],
    "projects": [{"name": "联合实验室L", "project_type": "联合实验室",
                  "time_start": "2026-03", "time_end": None}],
    "participations": [
        {"person_name": "张伟", "project_name": "联合实验室L",
         "explicitness": "listed_members", "sufficiency": "role_stated"},
    ],
}, ensure_ascii=False)
_NEWS_FEED_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>张伟教授联合实验室签约</title>
<link>https://news.example.com/a/9.html</link>
<description>内容</description></item>
</channel></rss>"""


async def test_retry_news_extract_item_success(db_session) -> None:
    """news_extract 失败任务（RSS 条目）：单条重跑成功 → 条目 extracted。"""
    from app.models import NewsItem
    from app.services.glm import GLMClient, TransportResult

    url = "https://news.example.com/a/9.html"
    db_session.add(NewsItem(source_id="t-feed", url=url, title="张伟教授联合实验室签约",
                            summary="内容", status="extraction_failed",
                            rss_entry={"source": "rss"}))
    db_session.add(FailedJob(job_type="news_extract", target=url, attempt=1,
                             next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    async def transport(system: str, user: str, max_tokens: int) -> TransportResult:
        return TransportResult(_NEWS_EXTRACT_JSON, 500, 800)

    stats = await scan_and_retry(db_session, RetryExecutor(glm=GLMClient(transport=transport)))

    assert stats == {"scanned": 1, "done": 1, "rescheduled": 0, "dead": 0}
    item = (await db_session.execute(select(NewsItem))).scalar_one()
    assert item.status == "extracted"


async def test_retry_news_extract_page_success(db_session) -> None:
    """news_extract 失败任务（新闻公示页）：单页重跑 → 页面 extracted + 同步条目。"""
    from app.models import NewsItem, WebPage
    from app.services.glm import GLMClient, TransportResult

    url = "https://news.example.com/notice/1.html"
    db_session.add(WebPage(url=url, seed_id="s-news", page_type="news",
                           title="公示", content_text="正文", status="extraction_failed"))
    db_session.add(FailedJob(job_type="news_extract", target=url, attempt=1,
                             next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    async def transport(system: str, user: str, max_tokens: int) -> TransportResult:
        return TransportResult(_NEWS_EXTRACT_JSON, 500, 800)

    stats = await scan_and_retry(db_session, RetryExecutor(glm=GLMClient(transport=transport)))

    assert stats["done"] == 1
    page = (await db_session.execute(select(WebPage))).scalar_one()
    assert page.status == "extracted"
    item = (await db_session.execute(select(NewsItem))).scalar_one()  # sync_news_page_item
    assert item.rss_entry.get("source") == "webpage"


async def test_retry_news_extract_missing_target_goes_dead(db_session) -> None:
    """news_extract 目标不存在（已清理）→ 3 次后死信而非静默成功。"""
    db_session.add(FailedJob(job_type="news_extract", target="https://gone.example.com/x",
                             attempt=3, next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    stats = await scan_and_retry(db_session, RetryExecutor())

    assert stats["dead"] == 1
    job = (await db_session.execute(select(FailedJob))).scalars().one()
    assert job.status == "dead" and "不存在" in job.error


async def test_retry_unknown_job_type_goes_dead(db_session) -> None:
    """未知 job_type（配置漂移兜底）：反复失败进死信，不静默吞掉。"""
    db_session.add(FailedJob(job_type="bogus_type", target="x", attempt=3,
                             next_retry_at=NOW - dt.timedelta(minutes=1), error="e"))
    await db_session.commit()

    stats = await scan_and_retry(db_session, RetryExecutor())

    assert stats["dead"] == 1
    job = (await db_session.execute(select(FailedJob))).scalars().one()
    assert job.status == "dead" and "未知 job_type" in job.error


# ---------- T14：调度注册 ----------

def test_build_scheduler_registers_all_jobs() -> None:
    from app.scheduler import build_scheduler

    scheduler = build_scheduler()
    jobs = {j.id: j.trigger for j in scheduler.get_jobs()}
    assert set(jobs) == {
        "daily_backup", "daily_pipeline", "news_collect", "mentorship_crawl",
        "failed_retry_scan", "dead_letter_patrol",
    }

    pipeline = jobs["daily_pipeline"]
    assert isinstance(pipeline, CronTrigger)
    assert "hour='3'" in str(pipeline) and "minute='0'" in str(pipeline)
    assert pipeline.jitter == 1200
    news = jobs["news_collect"]
    assert isinstance(news, CronTrigger)
    assert "hour='4'" in str(news) and news.jitter == 600
    crawl = jobs["mentorship_crawl"]
    assert isinstance(crawl, CronTrigger)
    assert "hour='5'" in str(crawl) and crawl.jitter == 600

    # 备份固定在管线之前（02:00 ± 5min < 03:00 - 20min jitter）
    backup = jobs["daily_backup"]
    assert isinstance(backup, CronTrigger)
    assert "hour='2'" in str(backup) and "minute='0'" in str(backup)

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
