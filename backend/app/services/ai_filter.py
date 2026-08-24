"""两段式 AI 相关性过滤（T6，FR-1.2，AC-8）。

第一段 · 规则粗筛（零成本，plan §4）：
- 直接保留：分类含 cs.AI / cs.LG / stat.ML 任一（宁多勿漏，交叉列表也算）
- 直接剔除：无任何 cs.* / stat.ML 分类，且标题+摘要不命中 ai_keywords
- 其余待定 → 第二段

第二段 · GLM 批量细筛：10 篇/请求，标题+摘要，返回 is_ai+理由；
非 AI → status=filtered_out。细筛被熔断拦截时待定论文放行（宁多勿漏）；
GLM 失败的批次写 failed_jobs（job_type=ai_fine_filter）待重试。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import load_directions
from app.models import FailedJob, Paper
from app.services.breaker import BreakerOpenError, JobClass
from app.services.glm import GLMClient, GLMError, GLMParseError, GLMTransientError

log = logging.getLogger("prof-graph.ai_filter")

BATCH_SIZE = 10

FINE_FILTER_SYSTEM = (
    "你是论文相关性判定助手。给定一批论文的标题和摘要，判断每篇是否为 AI 相关论文。"
    "判定标准：论文使用了 AI/ML 方法（而非仅提及 AI 词汇）才算 AI 相关。"
    '只输出 JSON：{"papers": [{"arxiv_id": "原文编号", "is_ai": true, '
    '"reason": "一句话理由"}]}，不要输出其他内容。'
)


@dataclass
class FilterReport:
    kept_by_rule: int = 0
    dropped_by_rule: int = 0
    ai_by_glm: int = 0
    dropped_by_glm: int = 0
    passed_by_breaker: int = 0  # 熔断放行的待定论文（宁多勿漏）
    failed_ids: list[int] = field(default_factory=list)  # GLM 失败待重试


def classify_paper(paper: Paper, core_categories: frozenset[str], keywords: tuple[str, ...]) -> str:
    """返回 keep / drop / pending。"""
    cats = set(paper.categories or [])
    if cats & core_categories:
        return "keep"
    has_cs_cross = any(c.startswith("cs.") or c == "stat.ML" for c in cats)
    text = f"{paper.title} {paper.abstract or ''}".lower()
    hit_keyword = any(k in text for k in keywords)
    if not has_cs_cross and not hit_keyword:
        return "drop"
    return "pending"


def _apply_drop(paper: Paper) -> None:
    paper.ai_relevant = False
    paper.status = "filtered_out"


def _build_fine_filter_payload(papers: list[Paper]) -> str:
    items = [
        {"arxiv_id": p.arxiv_id, "title": p.title, "abstract": (p.abstract or "")[:1000]}
        for p in papers
    ]
    return json.dumps({"papers": items}, ensure_ascii=False)


def _parse_verdicts(data: dict) -> dict[str, bool]:
    """响应 → {arxiv_id: is_ai}。字段非法的条目跳过（由调用方按缺失重试）。"""
    verdicts: dict[str, bool] = {}
    for item in data.get("papers", []):
        arxiv_id = item.get("arxiv_id")
        is_ai = item.get("is_ai")
        if isinstance(arxiv_id, str) and isinstance(is_ai, bool):
            verdicts[arxiv_id] = is_ai
    return verdicts


async def run_filter(
    session: AsyncSession,
    glm: GLMClient,
    paper_ids: list[int] | None = None,
) -> FilterReport:
    """对给定论文（默认所有未过滤的 pending_extraction）执行两段式过滤。"""
    report = FilterReport()
    config = load_directions()

    stmt = select(Paper).where(Paper.status == "pending_extraction", Paper.ai_relevant.is_(True))
    if paper_ids is not None:
        stmt = stmt.where(Paper.id.in_(paper_ids))
    papers = (await session.execute(stmt)).scalars().all()

    pending: list[Paper] = []
    for paper in papers:
        verdict = classify_paper(paper, config.ai_core_categories, config.ai_keywords)
        if verdict == "keep":
            report.kept_by_rule += 1
        elif verdict == "drop":
            _apply_drop(paper)
            report.dropped_by_rule += 1
        else:
            pending.append(paper)

    if pending:
        await _fine_filter(session, glm, pending, report)

    await session.commit()
    return report


async def _fine_filter(
    session: AsyncSession, glm: GLMClient, pending: list[Paper], report: FilterReport
) -> None:
    failed: list[Paper] = []
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start : start + BATCH_SIZE]
        try:
            data = await glm.complete_json(
                session,
                system=FINE_FILTER_SYSTEM,
                user=_build_fine_filter_payload(batch),
                job_type="ai_fine_filter",
                job_class=JobClass.fine_filter,
                max_tokens=2000,
            )
        except BreakerOpenError:
            # 熔断：待定论文放行入库（ai_relevant 保持 True），宁多勿漏
            report.passed_by_breaker += len(batch)
            log.warning("细筛熔断，%d 篇待定论文放行", len(batch))
            continue
        except (GLMTransientError, GLMParseError, GLMError) as e:
            log.warning("细筛批次失败（%s）：%s", type(e).__name__, e)
            failed.extend(batch)
            continue

        verdicts = _parse_verdicts(data)
        for paper in batch:
            if paper.arxiv_id not in verdicts:
                failed.append(paper)  # 响应缺该篇 → 整批语义缺失，按失败重试
            elif verdicts[paper.arxiv_id]:
                report.ai_by_glm += 1  # AI 相关，保持 pending_extraction
            else:
                _apply_drop(paper)
                report.dropped_by_glm += 1

    if failed:
        report.failed_ids = [p.id for p in failed]
        session.add(
            FailedJob(
                job_type="ai_fine_filter",
                target=json.dumps([p.arxiv_id for p in failed]),
                attempt=1,
                next_retry_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5),
                error=f"细筛失败，共 {len(failed)} 篇待重试",
            )
        )
