"""T9 单测：正常入库 / 部分容错 / 整篇失败重试 / 熔断跳过（FR-2.1~2.3，RD-11）。"""
from __future__ import annotations

import datetime as dt
import json

import respx

from sqlalchemy import select

from app.models import FailedJob, Paper, PaperAuthor, TokenUsage
from app.services.extractor import (
    EXTRACT_SYSTEM,
    FULLTEXT_MAX_CHARS,
    build_input,
    extract_paper,
    run_extraction,
    validate_extraction,
    validate_mentorship_signals,
)
from app.services.glm import GLMClient, GLMParseError, TransportResult
from app.settings import settings


class FakeTransport:
    def __init__(self, text: str | Exception = "{}", in_tok: int = 1500, out_tok: int = 1000):
        self.text, self.in_tok, self.out_tok = text, in_tok, out_tok
        self.prompts: list[str] = []

    async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
        if isinstance(self.text, Exception):
            raise self.text
        self.prompts.append(user)
        return TransportResult(self.text, self.in_tok, self.out_tok)


async def _add_paper(db_session, **overrides) -> Paper:
    paper = Paper(
        arxiv_id=overrides.get("arxiv_id", "2608.100"),
        title=overrides.get("title", "A Paper on LLM Agents"),
        abstract=overrides.get("abstract", "We study LLM agents."),
        authors_raw=overrides.get("authors_raw", ["A", "B"]),
        categories=overrides.get("categories", ["cs.AI"]),
    )
    db_session.add(paper)
    await db_session.flush()
    return paper


FIVE_AUTHORS_ONE_BROKEN = json.dumps({
    "authors": [
        {"name": "Wei Zhang", "seq": 0, "affiliation": "Peking University", "is_corresponding": True},
        {"name": "Li Wang", "seq": 1, "affiliation": None, "is_corresponding": False},
        {"name": "", "seq": 2, "affiliation": "Nowhere", "is_corresponding": False},  # 缺 name → 跳过
        {"name": "No Seq", "seq": None, "affiliation": "X", "is_corresponding": False},  # 缺 seq → 跳过
        {"name": "Anna Lee", "seq": 3, "affiliation": "  ", "is_corresponding": False},  # 空白机构 → null
    ],
    "research_tags": ["llm agent", "planning", " "],
})


async def test_extract_partial_tolerance(db_session) -> None:
    """5 作者中 2 个字段缺失：只跳过 2 个，其余 3 个入库；标签清洗。"""
    paper = await _add_paper(db_session)
    glm = GLMClient(transport=FakeTransport(FIVE_AUTHORS_ONE_BROKEN))

    report = await run_extraction(db_session, glm)

    assert (report.extracted, report.failed, report.breaker_skipped) == (1, 0, 0)
    await db_session.refresh(paper)
    assert paper.status == "extracted"
    assert paper.research_tags == ["llm agent", "planning"]

    rows = (
        await db_session.execute(
            select(PaperAuthor).order_by(PaperAuthor.author_seq)
        )
    ).scalars().all()
    assert [r.raw_name for r in rows] == ["Wei Zhang", "Li Wang", "Anna Lee"]
    assert rows[0].affiliation == "Peking University"
    assert rows[1].affiliation is None
    assert rows[2].affiliation is None
    # 用量按抽取记账
    usage = (await db_session.execute(select(TokenUsage))).scalars().all()
    assert len(usage) == 1 and usage[0].job_type == "glm_extract"


async def test_extract_whole_json_failure_retries(db_session) -> None:
    """非法 JSON：论文转 extraction_failed，failed_jobs 安排 1 分钟后重试。"""
    paper = await _add_paper(db_session, arxiv_id="2608.101")
    glm = GLMClient(transport=FakeTransport(GLMParseError("not json")))

    report = await run_extraction(db_session, glm)

    assert (report.extracted, report.failed) == (0, 1)
    await db_session.refresh(paper)
    assert paper.status == "extraction_failed"
    jobs = (await db_session.execute(select(FailedJob))).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].job_type == "glm_extract"
    assert jobs[0].target == "2608.101"
    assert jobs[0].attempt == 1 and jobs[0].status == "retrying"
    assert jobs[0].next_retry_at is not None


async def test_extract_breaker_skips_without_failed_jobs(db_session) -> None:
    """周预算触顶：抽取被拦，论文保持 pending_extraction，不写 failed_jobs。"""
    db_session.add(TokenUsage(
        day=dt.datetime.now(dt.timezone.utc).date(),
        job_type="x", input_tokens=settings.token_budget_weekly, output_tokens=0))
    papers = [await _add_paper(db_session, arxiv_id=f"2608.10{i}") for i in (2, 3)]
    glm = GLMClient(transport=FakeTransport())

    report = await run_extraction(db_session, glm)

    assert report.breaker_skipped == 2 and report.extracted == 0
    assert (await db_session.execute(select(FailedJob))).scalars().first() is None
    for p in papers:
        await db_session.refresh(p)
        assert p.status == "pending_extraction"


async def test_extract_schema_violation_all_authors_broken(db_session) -> None:
    """authors 全部无效 → 视为整篇失败走重试。"""
    paper = await _add_paper(db_session, arxiv_id="2608.104")
    glm = GLMClient(transport=FakeTransport('{"authors": [{"name": 1}]}'))

    report = await run_extraction(db_session, glm)
    assert report.failed == 1
    await db_session.refresh(paper)
    assert paper.status == "extraction_failed"


@respx.mock
async def test_input_fallback_to_abstract(db_session) -> None:
    """全文不可得（404）→ 输入退回 标题+摘要+作者列表。"""
    import httpx

    paper = await _add_paper(db_session, arxiv_id="2608.105")
    respx.get("https://arxiv.org/html/2608.105").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http:
        text, used_ft = await build_input(paper, http)
    assert used_ft is False
    assert "A Paper on LLM Agents" in text and "We study LLM agents." in text


@respx.mock
async def test_input_uses_fulltext(db_session) -> None:
    import httpx

    paper = await _add_paper(db_session, arxiv_id="2608.106")
    html = "<html><body>" + "fulltext content. " * 200 + "</body></html>"
    respx.get("https://arxiv.org/html/2608.106").mock(return_value=httpx.Response(200, text=html))
    async with httpx.AsyncClient() as http:
        text, used_ft = await build_input(paper, http)
    assert used_ft is True
    assert "fulltext content" in text


# ---------- 2026-08-30 两刀修复（arXiv HTML 页可能缺作者行）----------


@respx.mock
async def test_input_fulltext_prepends_metadata(db_session) -> None:
    """全文可用时输入头部仍拼 RSS 标题+作者列表——页面作者区块被 LaTeXML
    丢弃时模型依然看得到作者（12 篇必然死信的根因修复）。"""
    import httpx

    paper = await _add_paper(
        db_session, arxiv_id="2608.107", authors_raw=["Alice Chen", "Bob Li"]
    )
    # 页面正文没有任何作者名（≥500 字符以通过长度下限）
    html = "<html><body>" + "Abstract only body without any author names. " * 20 + "</body></html>"
    respx.get("https://arxiv.org/html/2608.107").mock(return_value=httpx.Response(200, text=html))
    async with httpx.AsyncClient() as http:
        text, used_ft = await build_input(paper, http)
    assert used_ft is True
    assert text.startswith("标题：A Paper on LLM Agents\n作者列表：Alice Chen, Bob Li\n")
    assert "Abstract only body" in text


@respx.mock
async def test_input_truncation_keeps_metadata_header(db_session) -> None:
    """超长全文截断时头部元数据保留（截断只吃尾部）。"""
    import httpx

    paper = await _add_paper(db_session, arxiv_id="2608.108", authors_raw=["Alice"])
    html = "<html><body>" + "word " * 20_000 + "</body></html>"
    respx.get("https://arxiv.org/html/2608.108").mock(return_value=httpx.Response(200, text=html))
    async with httpx.AsyncClient() as http:
        text, used_ft = await build_input(paper, http)
    assert used_ft is True
    assert len(text) == FULLTEXT_MAX_CHARS
    assert text.startswith("标题：A Paper on LLM Agents\n作者列表：Alice\n")


@respx.mock
async def test_input_rejects_failed_conversion_page(db_session) -> None:
    """LaTeXML 转换失败残页（title=Untitled Document）判全文不可用，
    回退摘要路径——残页仅 arXiv 界面文字，长度曾骗过 ≥500 字符检查。"""
    import httpx

    paper = await _add_paper(db_session, arxiv_id="2606.01009", authors_raw=["Carol"])
    chrome = (
        "<html><head><title>Untitled Document</title></head><body>"
        + "Experimental support, please view the build logs. " * 30
        + "</body></html>"
    )
    respx.get("https://arxiv.org/html/2606.01009").mock(
        return_value=httpx.Response(200, text=chrome)
    )
    async with httpx.AsyncClient() as http:
        text, used_ft = await build_input(paper, http)
    assert used_ft is False
    assert "标题：A Paper on LLM Agents" in text
    assert "We study LLM agents." in text and "Carol" in text


def test_validate_truncates_tags_to_8() -> None:
    data = {
        "authors": [{"name": "A", "seq": 0}],
        "research_tags": [f"tag{i}" for i in range(12)],
    }
    _, tags, _ = validate_extraction(data)
    assert len(tags) == 8


# ---------- T7 致谢信号（RD-M2-8） ----------

def test_validate_mentorship_signals_cleaning() -> None:
    data = {
        "mentorship_signals": [
            {"advisor": " 段海鑫 ", "student": None, "lab": "NISL", "hint": "感谢导师"},
            {"advisor": "", "student": "王五"},          # 缺 advisor → 跳过
            {"student": "王五"},                          # 缺 advisor → 跳过
            "not-a-dict",                                 # 非对象 → 跳过
            {"advisor": "李教授", "student": "  ", "lab": "  ", "hint": 123},
        ]
    }
    signals = validate_mentorship_signals(data)
    assert signals == [
        {"advisor": "段海鑫", "student": None, "lab": "NISL", "hint": "感谢导师"},
        {"advisor": "李教授", "student": None, "lab": None, "hint": None},
    ]
    # 老响应无该字段 → []（向后兼容）
    assert validate_mentorship_signals({}) == []
    assert validate_mentorship_signals({"mentorship_signals": "bad"}) == []


def test_extract_system_mentions_ack_schema() -> None:
    assert "mentorship_signals" in EXTRACT_SYSTEM
    assert "致谢" in EXTRACT_SYSTEM


async def test_extract_stores_ack_signals_only_fulltext(db_session) -> None:
    """全文输入 → 信号清洗入库；摘要输入 → 忽略（致谢只在全文中）。"""
    import httpx

    payload = json.dumps({
        "authors": [
            {"name": "Wei Zhang", "seq": 0, "affiliation": "Peking University", "is_corresponding": False},
            {"name": "Li Wang", "seq": 1, "affiliation": None, "is_corresponding": False},
        ],
        "research_tags": ["llm agent"],
        "mentorship_signals": [
            {"advisor": "Li Wang", "student": "Wei Zhang", "lab": None,
             "hint": "We thank our advisor Li Wang for guidance."},
            {"advisor": "", "student": "X"},
        ],
    })
    paper_ft = await _add_paper(db_session, arxiv_id="2608.200")
    paper_abs = await _add_paper(db_session, arxiv_id="2608.201")

    glm = GLMClient(transport=FakeTransport(payload))
    async with httpx.AsyncClient() as http:
        r1 = await extract_paper(db_session, glm, http, paper_ft, input_text="full text with acknowledgements", used_fulltext=True)
        r2 = await extract_paper(db_session, glm, http, paper_abs, input_text="标题+摘要", used_fulltext=False)

    assert (r1, r2) == ("extracted", "extracted")
    assert paper_ft.mentorship_signals == [
        {"advisor": "Li Wang", "student": "Wei Zhang", "lab": None,
         "hint": "We thank our advisor Li Wang for guidance."},
    ]
    assert paper_abs.mentorship_signals is None  # 摘要输入不采
