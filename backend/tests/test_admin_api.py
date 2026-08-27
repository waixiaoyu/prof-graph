"""T17 单测：管理端 API（FR-1.4，NFR-3）。"""
from __future__ import annotations

import asyncio
import datetime as dt

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import get_session
from app.main import app
from app.models import FailedJob, TokenUsage
from app.services import breaker, pipeline

EMPTY_RSS = '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'


@pytest.fixture
async def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_breaker_override():
    breaker._override_until = None
    pipeline._trigger_guard = False
    yield
    breaker._override_until = None
    pipeline._trigger_guard = False


async def test_trigger_update_conflict_when_running(client, db_session):
    async with pipeline.PIPELINE_LOCK:  # 模拟批次执行中
        resp = await client.post("/api/admin/trigger-update")
    assert resp.status_code == 409


async def test_trigger_update_returns_batch_id(client, db_session, monkeypatch):
    monkeypatch.setattr("app.api.admin.trigger_pipeline", lambda scope=None: "abc12345")
    resp = await client.post("/api/admin/trigger-update")
    assert resp.status_code == 200
    assert resp.json() == {"batch_id": "abc12345", "scope": None}

    resp = await client.post("/api/admin/trigger-update?scope=crawl")
    assert resp.status_code == 200
    assert resp.json() == {"batch_id": "abc12345", "scope": "crawl"}

    resp = await client.post("/api/admin/trigger-update?scope=bogus")
    assert resp.status_code == 400


async def test_update_status_unknown_404(client, db_session):
    resp = await client.get("/api/admin/update-status/nonexist")
    assert resp.status_code == 404


@respx.mock
async def test_trigger_pipeline_runs_to_done(db_session):
    """trigger_pipeline（注入测试库会话工厂）：后台跑通空批次到 done，状态可查。"""
    respx.get(host="export.arxiv.org").mock(
        return_value=httpx.Response(200, text=EMPTY_RSS)
    )
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    batch_id = pipeline.trigger_pipeline(session_factory=factory)
    assert batch_id is not None

    for _ in range(400):  # 轮询至批次结束（M2 后含 crawl 阶段，留足余量）
        batch = pipeline.get_batch(batch_id)
        if batch is not None and not batch.running:
            break
        await asyncio.sleep(0.05)

    batch = pipeline.get_batch(batch_id)
    assert batch is not None and batch.stage == "done", batch.error if batch else "?"
    assert batch.counts["collect"]["added"] == 0
    assert pipeline._trigger_guard is False

    # 触发期间再触发被拒（护栏）
    assert pipeline.trigger_pipeline(session_factory=factory) is not None


async def test_metrics_token_usage_and_failed_jobs(client, db_session):
    today = dt.date.today()
    db_session.add_all([
        TokenUsage(day=today, job_type="ai_fine_filter", input_tokens=300_000, output_tokens=100_000),
        TokenUsage(day=today - dt.timedelta(days=1), job_type="glm_extract", input_tokens=900_000, output_tokens=100_000),
        FailedJob(job_type="rss_fetch", target="cs.NI", attempt=2, status="retrying",
                  next_retry_at=dt.datetime.now(dt.timezone.utc), error="timeout"),
        FailedJob(job_type="glm_extract", target="2608.00001", attempt=3, status="dead", error="parse"),
        FailedJob(job_type="rss_fetch", target="cs.AI", attempt=1, status="done", error=None),
    ])
    await db_session.commit()

    resp = await client.get("/api/admin/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["token_usage"]["daily_used"] == 400_000
    # 昨日行是否计入本周取决于今天是否周一（周界=周一）
    from app.services.breaker import _week_start
    expected_weekly = 1_400_000 if _week_start(today) < today else 400_000
    assert data["token_usage"]["weekly_used"] == expected_weekly
    assert data["breaker"]["level"] == "ok"  # 日 40 万 < 80% 阈值 96 万
    assert len(data["failed_jobs"]) == 2  # done 不出现
    assert {j["status"] for j in data["failed_jobs"]} == {"retrying", "dead"}
    # M2-T9：RSS 源状态（OQ-2 停用报警），默认配置无停用
    assert data["rss_sources"]["disabled"] == []
    assert all("consecutive_failures" in s for s in data["rss_sources"]["sources"])


async def test_breaker_resume_sets_daily_override(client, db_session):
    resp = await client.post("/api/admin/breaker/resume")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resumed"] is True
    until = dt.datetime.fromisoformat(body["override_until"])
    now = dt.datetime.now(dt.timezone.utc)
    assert now < until <= now + dt.timedelta(days=1)

    status = await breaker.get_status(db_session)
    assert status.overridden

    metrics = (await client.get("/api/admin/metrics")).json()
    assert metrics["breaker"]["manual_override_until"] == body["override_until"]
