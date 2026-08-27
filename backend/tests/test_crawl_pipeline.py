"""T8 单测：scope=crawl 子链编排 / 调度任务 / page_extract 重试分支。"""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from sqlalchemy import select

from app.models import FailedJob, Relationship, WebPage
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
    async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
        return TransportResult(PAGE_JSON, 1500, 1000)


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
