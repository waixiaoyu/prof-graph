"""分级 token 熔断器（T7，NFR-2/NFR-3）。

规则（plan §4，2026-08-24 修订）：
- 双预算：日预算 + 周预算（.env 可配：TOKEN_BUDGET_DAILY / TOKEN_BUDGET_WEEKLY）
- 分级：日 80% 告警（可查，不拦截）→ 日 100% 停细筛（待定论文放行）→ 周触顶停抽取
- 管理员手动放行：任一级触发后可放行，当日有效，次日自动回到自动模式
- 次日 / 次周按自然日、自然周（周一起）窗口自动恢复，无需人工干预

用量来自 token_usage 表（只读聚合）；手动放行是进程内状态，重启即失效
（单进程部署 + 当日有效的语义下可接受）。
"""
from __future__ import annotations

import datetime as dt
import enum
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TokenUsage
from app.settings import settings

log = logging.getLogger("prof-graph.breaker")


class JobClass(str, enum.Enum):
    """GLM 调用的两个用途，熔断分级不同。"""

    fine_filter = "fine_filter"  # 细筛：日 100% 停
    extract = "extract"          # 抽取：周触顶停


class BreakerOpenError(RuntimeError):
    """该用途的 GLM 调用被熔断拦截。

    细筛被拦时调用方应放行待定论文（宁多勿漏，FR-1.2）。
    """

    def __init__(self, level: str, message: str) -> None:
        super().__init__(message)
        self.level = level


@dataclass(frozen=True)
class BreakerStatus:
    level: str  # ok / warn / daily_stop / weekly_stop
    daily_used: int
    daily_budget: int
    weekly_used: int
    weekly_budget: int
    manual_override_until: dt.datetime | None

    @property
    def overridden(self) -> bool:
        return self.manual_override_until is not None


# 进程内手动放行状态（当日 24:00 UTC 到期）
_override_until: dt.datetime | None = None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())  # 周一


def manual_resume(now: dt.datetime | None = None) -> dt.datetime:
    """管理员放行：当日有效（到 UTC 次日零点）。操作记日志。"""
    global _override_until
    n = now or _now()
    tomorrow = (n + dt.timedelta(days=1)).date()
    _override_until = dt.datetime.combine(tomorrow, dt.time.min, tzinfo=dt.timezone.utc)
    log.info("熔断手动放行：管理员决定继续，放行至 %s", _override_until.isoformat())
    return _override_until


def _effective_override(now: dt.datetime) -> dt.datetime | None:
    if _override_until is not None and _override_until > now:
        return _override_until
    return None


async def _sum_tokens(session: AsyncSession, day_from: dt.date, day_to: dt.date) -> int:
    stmt = select(
        func.coalesce(func.sum(TokenUsage.input_tokens + TokenUsage.output_tokens), 0)
    ).where(TokenUsage.day >= day_from, TokenUsage.day <= day_to)
    return int((await session.execute(stmt)).scalar() or 0)


async def get_status(session: AsyncSession, now: dt.datetime | None = None) -> BreakerStatus:
    n = now or _now()
    daily_used = await _sum_tokens(session, n.date(), n.date())
    weekly_used = await _sum_tokens(session, _week_start(n.date()), n.date())

    if weekly_used >= settings.token_budget_weekly:
        level = "weekly_stop"
    elif daily_used >= settings.token_budget_daily:
        level = "daily_stop"
    elif daily_used >= settings.token_budget_daily * 0.8:
        level = "warn"
    else:
        level = "ok"

    return BreakerStatus(
        level=level,
        daily_used=daily_used,
        daily_budget=settings.token_budget_daily,
        weekly_used=weekly_used,
        weekly_budget=settings.token_budget_weekly,
        manual_override_until=_effective_override(n),
    )


async def ensure_allowed(
    session: AsyncSession,
    job_class: JobClass,
    now: dt.datetime | None = None,
) -> BreakerStatus:
    """调用 GLM 前检查；被拦时抛 BreakerOpenError，状态对象供调用方决策。"""
    status = await get_status(session, now)
    if status.overridden:
        return status

    if job_class is JobClass.fine_filter and status.level in ("daily_stop", "weekly_stop"):
        raise BreakerOpenError(
            status.level,
            f"细筛已熔断（日用量 {status.daily_used}/{status.daily_budget}，"
            f"周用量 {status.weekly_used}/{status.weekly_budget}），待定论文放行入库",
        )
    if job_class is JobClass.extract and status.level == "weekly_stop":
        raise BreakerOpenError(
            status.level,
            f"抽取已熔断（周用量 {status.weekly_used}/{status.weekly_budget}），"
            "等待次周自动恢复或管理员放行",
        )
    return status


async def record_usage(
    session: AsyncSession,
    job_type: str,
    input_tokens: int,
    output_tokens: int,
    now: dt.datetime | None = None,
) -> None:
    n = now or _now()
    session.add(
        TokenUsage(
            day=n.date(), job_type=job_type,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
    )
    await session.flush()
