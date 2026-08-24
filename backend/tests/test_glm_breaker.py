"""T7 单测：分级熔断各级阈值 / 手动放行 / 跨天自动恢复 / 用量记账（NFR-2/NFR-3）。

时间基准用真实当前日期（熔断窗口按自然日/自然周聚合，测试种数据用
今天/昨天即可，不依赖具体时刻）。
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.models import TokenUsage
from app.services import breaker
from app.services.breaker import BreakerOpenError, JobClass
from app.services.glm import GLMClient, GLMParseError, TransportResult, parse_json_response
from app.settings import settings

TODAY = dt.datetime.now(dt.timezone.utc)
YESTERDAY = TODAY - dt.timedelta(days=1)


class FakeTransport:
    def __init__(self, text: str = '{"ok": true}', in_tok: int = 100, out_tok: int = 50):
        self.text, self.in_tok, self.out_tok = text, in_tok, out_tok
        self.calls = 0

    async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
        self.calls += 1
        return TransportResult(self.text, self.in_tok, self.out_tok)


async def _seed(session, rows: list[tuple[dt.date, str, int, int]]) -> None:
    for day, job_type, in_tok, out_tok in rows:
        session.add(
            TokenUsage(day=day, job_type=job_type, input_tokens=in_tok, output_tokens=out_tok)
        )
    await session.flush()


@pytest.fixture(autouse=True)
def _reset_override():
    breaker._override_until = None
    yield
    breaker._override_until = None


async def test_level_ok_and_warn(db_session) -> None:
    """<80% 正常；80%-100% 告警但不拦截任何用途。"""
    status = await breaker.get_status(db_session)
    assert status.level == "ok"

    await _seed(db_session, [(TODAY.date(), "fine_filter", int(settings.token_budget_daily * 0.8), 0)])
    status = await breaker.get_status(db_session)
    assert status.level == "warn"
    await breaker.ensure_allowed(db_session, JobClass.fine_filter)
    await breaker.ensure_allowed(db_session, JobClass.extract)


async def test_daily_stop_blocks_fine_filter_only(db_session) -> None:
    """日预算触顶：细筛被拦，抽取仍放行。"""
    await _seed(db_session, [(TODAY.date(), "fine_filter", settings.token_budget_daily, 0)])
    status = await breaker.get_status(db_session)
    assert status.level == "daily_stop"

    with pytest.raises(BreakerOpenError) as exc:
        await breaker.ensure_allowed(db_session, JobClass.fine_filter)
    assert exc.value.level == "daily_stop"

    await breaker.ensure_allowed(db_session, JobClass.extract)


async def test_weekly_stop_blocks_extract(db_session) -> None:
    """周预算触顶：抽取被拦；细筛同样被拦（周预算是硬顶，两用途都停）。"""
    await _seed(db_session, [(TODAY.date(), "extract", settings.token_budget_weekly, 0)])
    status = await breaker.get_status(db_session)
    assert status.level == "weekly_stop"

    with pytest.raises(BreakerOpenError) as exc:
        await breaker.ensure_allowed(db_session, JobClass.extract)
    assert exc.value.level == "weekly_stop"

    with pytest.raises(BreakerOpenError):
        await breaker.ensure_allowed(db_session, JobClass.fine_filter)


async def test_manual_resume_restores_calls(db_session) -> None:
    """熔断后管理员放行：两类调用恢复，放行到次日零点。"""
    await _seed(db_session, [(TODAY.date(), "fine_filter", settings.token_budget_daily, 0)])
    with pytest.raises(BreakerOpenError):
        await breaker.ensure_allowed(db_session, JobClass.fine_filter)

    until = breaker.manual_resume()
    assert until == dt.datetime.combine(
        (TODAY + dt.timedelta(days=1)).date(), dt.time.min, tzinfo=dt.timezone.utc
    )

    status = await breaker.ensure_allowed(db_session, JobClass.fine_filter)
    assert status.overridden
    await breaker.ensure_allowed(db_session, JobClass.extract)


async def test_next_day_auto_recovery(db_session) -> None:
    """昨日触顶 + 昨日放行过：今日窗口重置，自动恢复自动模式。"""
    await _seed(db_session, [(YESTERDAY.date(), "fine_filter", settings.token_budget_daily, 0)])
    breaker.manual_resume(now=YESTERDAY)

    status = await breaker.get_status(db_session)
    assert status.level == "ok"
    assert not status.overridden
    await breaker.ensure_allowed(db_session, JobClass.fine_filter)
    await breaker.ensure_allowed(db_session, JobClass.extract)


async def test_complete_json_records_usage(db_session) -> None:
    """每次调用写 token_usage（job_type + in/out tokens），并返回解析结果。"""
    fake = FakeTransport(text='```json\n{"is_ai": true}\n```', in_tok=800, out_tok=120)
    client = GLMClient(transport=fake)

    result = await client.complete_json(
        db_session, system="s", user="u",
        job_type="fine_filter", job_class=JobClass.fine_filter,
    )
    assert result == {"is_ai": True}
    assert fake.calls == 1

    rows = (await db_session.execute(select(TokenUsage))).scalars().all()
    assert len(rows) == 1
    assert rows[0].job_type == "fine_filter"
    assert rows[0].input_tokens == 800
    assert rows[0].output_tokens == 120
    assert rows[0].day == TODAY.date()


async def test_complete_json_blocked_by_breaker(db_session) -> None:
    """熔断中的用途：complete_json 抛 BreakerOpenError，不产生任何 GLM 调用。"""
    await _seed(db_session, [(TODAY.date(), "fine_filter", settings.token_budget_daily, 0)])
    fake = FakeTransport()
    client = GLMClient(transport=fake)

    with pytest.raises(BreakerOpenError):
        await client.complete_json(
            db_session, system="s", user="u",
            job_type="fine_filter", job_class=JobClass.fine_filter,
        )
    assert fake.calls == 0


def test_parse_json_response_tolerances() -> None:
    assert parse_json_response('{"a": 1}') == {"a": 1}
    assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_response('前置说明 {"a": 1} 后置说明') == {"a": 1}
    with pytest.raises(GLMParseError):
        parse_json_response("完全不是 JSON")
