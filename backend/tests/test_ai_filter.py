"""T6 单测：粗筛三路判定 / GLM 细筛落库 / 熔断放行 / 失败重试（FR-1.2，AC-8）。"""
from __future__ import annotations

import json
from sqlalchemy import select

from app.models import FailedJob, Paper, TokenUsage
from app.services import breaker
from app.services.ai_filter import classify_paper, run_filter
from app.services.breaker import JobClass
from app.services.glm import GLMClient, GLMParseError, TransportResult
from app.settings import settings

CORE = frozenset({"cs.AI", "cs.LG", "stat.ML"})
KEYWORDS = ("machine learning", "deep learning", "reinforcement learning", "LLM")


def _paper(arxiv_id: str, categories: list[str], title: str = "", abstract: str = "") -> Paper:
    return Paper(
        arxiv_id=arxiv_id,
        title=title or f"Paper {arxiv_id}",
        abstract=abstract,
        authors_raw=["Author A"],
        categories=categories,
    )


# ---------- 第一段：规则粗筛（纯函数） ----------

def test_classify_keep_core_category() -> None:
    """cs.LG 论文直接保留（含交叉列表）。"""
    assert classify_paper(_paper("1", ["cs.NI", "cs.LG"]), CORE, KEYWORDS) == "keep"
    assert classify_paper(_paper("2", ["stat.ML"]), CORE, KEYWORDS) == "keep"


def test_classify_drop_pure_math() -> None:
    """纯数学论文：无 cs.*/stat.ML 交叉、无关键词 → 剔除。"""
    p = _paper("3", ["math.OC", "math.PR"], title="Optimal transport bounds",
               abstract="We prove new bounds for stochastic optimization.")
    assert classify_paper(p, CORE, KEYWORDS) == "drop"


def test_classify_pending() -> None:
    """有 cs 交叉但非核心类 → 待定；无交叉但命中关键词 → 也待定。"""
    p1 = _paper("4", ["cs.NI"], abstract="routing protocol analysis")
    assert classify_paper(p1, CORE, KEYWORDS) == "pending"
    p2 = _paper("5", ["eess.SP"], abstract="we use reinforcement learning to...")
    assert classify_paper(p2, CORE, KEYWORDS) == "pending"


# ---------- 第二段：GLM 细筛（fake transport） ----------

class FakeTransport:
    def __init__(self, text: str | Exception = '{"papers": []}', in_tok: int = 500, out_tok: int = 200):
        self.text, self.in_tok, self.out_tok = text, in_tok, out_tok
        self.prompts: list[str] = []

    async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
        if isinstance(self.text, Exception):
            raise self.text
        self.prompts.append(user)
        return TransportResult(self.text, self.in_tok, self.out_tok)


async def _add_papers(db_session, *papers: Paper) -> list[Paper]:
    for p in papers:
        db_session.add(p)
    await db_session.flush()
    return list(papers)


async def test_run_filter_full_flow(db_session) -> None:
    """三路论文同批：规则保留/规则剔除/GLM 细筛各归其位。"""
    kept = _paper("2608.1", ["cs.LG"])
    dropped = _paper("2608.2", ["math.OC"], title="Bounds", abstract="pure analysis")
    glm_ai = _paper("2608.3", ["cs.NI"], abstract="network control")
    glm_non_ai = _paper("2608.4", ["eess.SY"], abstract="control system design with neural network")
    await _add_papers(db_session, kept, dropped, glm_ai, glm_non_ai)

    resp = json.dumps({"papers": [
        {"arxiv_id": "2608.3", "is_ai": True, "reason": "用了 RL"},
        {"arxiv_id": "2608.4", "is_ai": False, "reason": "传统控制方法"},
    ]})
    glm = GLMClient(transport=FakeTransport(resp))

    report = await run_filter(db_session, glm)

    assert (report.kept_by_rule, report.dropped_by_rule, report.ai_by_glm, report.dropped_by_glm) == (1, 1, 1, 1)
    assert not report.failed_ids and report.passed_by_breaker == 0

    by_id = {p.arxiv_id: p for p in (await db_session.execute(select(Paper))).scalars()}
    assert by_id["2608.1"].status == "pending_extraction"  # 保留，待抽取
    assert by_id["2608.2"].status == "filtered_out" and by_id["2608.2"].ai_relevant is False
    assert by_id["2608.3"].status == "pending_extraction"  # GLM 判 AI
    assert by_id["2608.4"].status == "filtered_out" and by_id["2608.4"].ai_relevant is False

    # 只有待定项走了 GLM（1 次请求），token 已记账
    transport: FakeTransport = glm._transport  # noqa: SLF001
    assert len(transport.prompts) == 1
    usage = (await db_session.execute(select(TokenUsage))).scalars().all()
    assert len(usage) == 1 and usage[0].job_type == "ai_fine_filter"


async def test_run_filter_breaker_passes_pending(db_session) -> None:
    """日预算触顶：待定论文全部放行（ai_relevant=True，状态不变），零 GLM 调用。"""
    import datetime as dt

    db_session.add(TokenUsage(day=dt.datetime.now(dt.timezone.utc).date(),
                              job_type="x", input_tokens=settings.token_budget_daily, output_tokens=0))
    p1 = _paper("2608.5", ["cs.NI"])
    p2 = _paper("2608.6", ["eess.SP"], abstract="deep learning beamformer")
    await _add_papers(db_session, p1, p2)

    glm = GLMClient(transport=FakeTransport())
    report = await run_filter(db_session, glm)

    assert report.passed_by_breaker == 2
    assert glm._transport.prompts == []  # noqa: SLF001 — 一次都没调用
    for p in (p1, p2):
        await db_session.refresh(p)
        assert p.status == "pending_extraction" and p.ai_relevant is True


async def test_run_filter_glm_failure_writes_failed_jobs(db_session) -> None:
    """GLM 整体解析失败：待定论文保持原状，failed_jobs 记录待重试。"""
    p = _paper("2608.7", ["cs.DC"])
    await _add_papers(db_session, p)
    glm = GLMClient(transport=FakeTransport(GLMParseError("bad json")))

    report = await run_filter(db_session, glm)

    assert report.failed_ids == [p.id]
    await db_session.refresh(p)
    assert p.status == "pending_extraction" and p.ai_relevant is True
    jobs = (await db_session.execute(select(FailedJob))).scalars().all()
    assert len(jobs) == 1 and jobs[0].job_type == "ai_fine_filter"


async def test_run_filter_second_round_skips_filtered(db_session) -> None:
    """D2 重筛优化：细筛判 AI 的论文仍在等抽取时，下一轮不再送 GLM。"""
    p = _paper("2608.8", ["cs.NI"], abstract="network control")
    await _add_papers(db_session, p)
    resp = json.dumps({"papers": [{"arxiv_id": "2608.8", "is_ai": True, "reason": "用了 RL"}]})
    glm = GLMClient(transport=FakeTransport(resp))

    report1 = await run_filter(db_session, glm)
    assert report1.ai_by_glm == 1
    await db_session.refresh(p)
    assert p.last_filtered_at is not None  # 拿到判定即打标
    transport: FakeTransport = glm._transport  # noqa: SLF001
    assert len(transport.prompts) == 1

    # 第二轮：论文还是 pending_extraction（等抽取），但不重复细筛
    report2 = await run_filter(db_session, glm)
    assert report2.skipped_filtered == 1
    assert report2.ai_by_glm == 0
    assert len(transport.prompts) == 1  # 零新增 GLM 调用
    await db_session.refresh(p)
    assert p.status == "pending_extraction" and p.ai_relevant is True
