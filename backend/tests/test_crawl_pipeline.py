"""T8/T12 单测：scope=crawl|news 子链编排 / 调度任务 / page_extract 重试分支。"""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from sqlalchemy import select

from app.models import FailedJob, NewsItem, Relationship, WebPage
from app.scheduler import build_scheduler
from app.services.failed_jobs import RetryExecutor, scan_and_retry
from app.services.glm import GLMClient, TransportResult
from app.services.pipeline import run_pipeline

SEED_URL = "https://netsec.ccert.edu.cn/chs/people/"
ROBOTS_URL = "https://netsec.ccert.edu.cn/robots.txt"

SEED_HTML = """<html><head><title>NISL 成员</title></head><body>
<nav>导航</nav>
<h1>网络与信息安全实验室 成员</h1>
<p>段海鑫 教授</p>
<p>张三 博士生（导师：段海鑫）</p>
<footer>页脚</footer>
</body></html>"""

PAGE_JSON = json.dumps({
    "lab_name": "NISL 实验室", "org_school": "清华大学", "org_department": "网络研究院",
    "page_context": "official_lab",
    "members": [
        {"name": "段海鑫", "role": "professor"},
        {"name": "张三", "role": "phd", "advisor": "段海鑫"},
    ],
}, ensure_ascii=False)


class _FakeTransport:
    def __init__(self, text: str = PAGE_JSON):
        self.text = text

    async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
        return TransportResult(self.text, 1500, 1000)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(_handler), follow_redirects=True)


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == ROBOTS_URL:
        return httpx.Response(200, text="User-agent: *\nAllow: /")
    if url == SEED_URL:
        return httpx.Response(200, text=SEED_HTML)
    return httpx.Response(404)


async def test_scope_crawl_runs_only_crawl_chain(db_session) -> None:
    """scope=crawl：只跑 crawl → mentor_link，产出页面快照 + 传承关系。"""
    glm = GLMClient(transport=_FakeTransport())
    http = _client()
    try:
        batch = await run_pipeline(db_session, glm=glm, http=http, scope="crawl")
    finally:
        await http.aclose()

    assert batch.error is None and batch.stage == "done"
    assert set(batch.counts) == {"crawl", "mentor_link"}  # 论文链路未跑
    assert batch.counts["crawl"]["pages_new"] == 1
    assert batch.counts["mentor_link"]["pages_extracted"] == 1
    assert batch.counts["mentor_link"]["pairs_created"] >= 2  # mentor_student + same_lab

    page = (await db_session.execute(select(WebPage))).scalar_one()
    assert page.status == "extracted" and page.content_hash
    rels = (
        await db_session.execute(
            select(Relationship).where(Relationship.type == "academic_mentorship")
        )
    ).scalars().all()
    assert {r.subtype for r in rels} == {"mentor_student", "same_lab"}


async def test_scope_invalid_rejected(db_session) -> None:
    with pytest.raises(ValueError, match="未知 scope"):
        await run_pipeline(db_session, scope="bogus")


def test_scheduler_has_crawl_job() -> None:
    scheduler = build_scheduler()
    job = scheduler.get_job("mentorship_crawl")
    assert job is not None
    assert "hour='5'" in str(job.trigger) and "minute='0'" in str(job.trigger)


async def test_retry_executor_page_extract(db_session) -> None:
    """page_extract 失败任务：重试执行器按 url 重跑单页（不限状态）。"""
    page = WebPage(
        url=SEED_URL, seed_id="thu-nisl-members", page_type="lab_members",
        title="NISL 成员", content_text=SEED_HTML, status="extraction_failed",
    )
    db_session.add(page)
    await db_session.flush()
    job = FailedJob(
        job_type="page_extract", target=SEED_URL, attempt=1,
        next_retry_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1),
    )
    db_session.add(job)
    await db_session.commit()

    glm = GLMClient(transport=_FakeTransport())
    stats = await scan_and_retry(db_session, RetryExecutor(glm=glm))

    assert stats["done"] == 1
    await db_session.refresh(job)
    assert job.status == "done"
    await db_session.refresh(page)
    assert page.status == "extracted"


# ---------- T12 资讯链路 ----------

FEED_URL = "https://news.example.com/feed.xml"
NEWS_JSON = json.dumps({
    "no_signal": False,
    "persons": [{"name": "张伟", "org": "清华大学", "role": "教授"},
                {"name": "李娜", "org": "北京大学", "role": None}],
    "projects": [{"name": "联合实验室L", "project_type": "联合实验室",
                  "time_start": "2026-03", "time_end": None}],
    "participations": [
        {"person_name": "张伟", "project_name": "联合实验室L",
         "explicitness": "listed_members", "sufficiency": "role_stated"},
        {"person_name": "李娜", "project_name": "联合实验室L",
         "explicitness": "stated_participation", "sufficiency": "mentioned"},
    ],
}, ensure_ascii=False)

FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>AI 资讯</title>
<item><title>张伟教授团队联合实验室签约</title>
<link>https://news.example.com/a/1.html</link>
<description>两校共建联合实验室</description>
<pubDate>Mon, 24 Aug 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""


def _news_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == FEED_URL:
        return httpx.Response(200, text=FEED_XML)
    return httpx.Response(404)


async def test_scope_news_runs_only_news_chain(db_session) -> None:
    """scope=news：只跑 news_collect → news_link，产出资讯条目 + 项目关系（AC-3 自动验证）。"""
    # pipeline 阶段内局部导入 collect_news，替换模块属性即可生效；
    # 注入单源配置绕过进程级 load_sources 缓存
    import app.services.news_collector as nc
    from app.sources_config import RssSource, SourcesConfig

    test_sources = SourcesConfig(
        rss=(RssSource(id="t-feed", url=FEED_URL, tier="known_media"),), seeds=()
    )
    orig_collect = nc.collect_news

    async def _collect(session, http=None, sources=None):
        return await orig_collect(session, http=http, sources=test_sources)

    glm = GLMClient(transport=_FakeTransport(NEWS_JSON))
    http = httpx.AsyncClient(transport=httpx.MockTransport(_news_handler), follow_redirects=True)
    nc.collect_news = _collect
    try:
        batch = await run_pipeline(db_session, glm=glm, http=http, scope="news")
    finally:
        nc.collect_news = orig_collect
        await http.aclose()

    assert batch.error is None and batch.stage == "done"
    assert set(batch.counts) == {"news_collect", "news_link"}  # 论文/爬虫链路未跑
    assert batch.counts["news_collect"]["added"] == 1
    assert batch.counts["news_link"]["items_extracted"] == 1
    assert batch.counts["news_link"]["pairs_created"] == 1

    item = (await db_session.execute(select(NewsItem))).scalar_one()
    assert item.status == "extracted"
    rels = (
        await db_session.execute(
            select(Relationship).where(Relationship.type == "project_cooperation")
        )
    ).scalars().all()
    assert len(rels) == 1 and rels[0].coop_count == 1


def test_scheduler_has_news_job() -> None:
    scheduler = build_scheduler()
    job = scheduler.get_job("news_collect")
    assert job is not None
    assert "hour='4'" in str(job.trigger) and "minute='0'" in str(job.trigger)
