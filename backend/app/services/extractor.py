"""GLM 抽取器（T9，FR-2.1~2.3，RD-11 部分容错）。

输入策略（FR-2.2）：优先 arXiv HTML 全文（截断约 12k tokens），
不可得则标题+摘要+作者列表。输出按 plan §3 Schema 校验：
- 顶层可解析 → 逐个作者校验，name+seq 完整者入库；缺字段者跳过并记 warning
- 整篇解析失败 / 无有效作者 → failed_jobs 退避重试（1/5/25min ×3 后死信）
- 熔断 → 跳过该篇（不写 failed_jobs，恢复后由调度器重扫）
状态流转：pending_extraction → extracted / extraction_failed。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Paper, PaperAuthor
from app.services.breaker import BreakerOpenError, JobClass
from app.services.failed_jobs import schedule_retry
from app.services.glm import GLMClient, GLMError, GLMParseError, GLMTransientError

log = logging.getLogger("prof-graph.extractor")

# 12k tokens 的字符近似（英文 ~4 chars/token）
FULLTEXT_MAX_CHARS = 48_000
FULLTEXT_MIN_CHARS = 500  # 低于此长度视为全文不可用，回退摘要
HTML_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.DOTALL)
USER_AGENT = "prof-graph/0.1 (academic-network-governance; internal)"

EXTRACT_SYSTEM = (
    "你是学术信息抽取助手。从论文内容中抽取作者列表和研究方向标签。"
    '只输出 JSON：{"authors": [{"name": "姓名", "seq": 0, '
    '"affiliation": "署名机构或 null", "is_corresponding": false}], '
    '"research_tags": ["最多 8 个英文短语"]}。'
    "authors 按署名顺序，seq 从 0 开始；机构取作者署名时所属机构，没有则 null。"
)


@dataclass
class ExtractReport:
    extracted: int = 0
    partial_skipped: int = 0  # 部分容错跳过的作者数
    failed: int = 0
    breaker_skipped: int = 0
    used_fulltext: int = 0


async def fetch_fulltext(http: httpx.AsyncClient, arxiv_id: str) -> str | None:
    """arXiv HTML 版全文（去标签）；不可得返回 None（LaTeX 源 M4 再做）。"""
    try:
        resp = await http.get(
            f"https://arxiv.org/html/{arxiv_id}",
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code != 200:
            return None
        text = HTML_TAG_RE.sub(" ", resp.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text if len(text) >= FULLTEXT_MIN_CHARS else None
    except httpx.HTTPError:
        return None


async def build_input(paper: Paper, http: httpx.AsyncClient) -> tuple[str, bool]:
    """返回 (输入文本, 是否用了全文)。"""
    fulltext = await fetch_fulltext(http, paper.arxiv_id)
    if fulltext:
        return fulltext[:FULLTEXT_MAX_CHARS], True
    authors = ", ".join(paper.authors_raw or [])
    return (
        f"标题：{paper.title}\n摘要：{paper.abstract or '（无）'}\n作者列表：{authors}",
        False,
    )


def validate_extraction(data: dict) -> tuple[list[dict], list[str], list[str]]:
    """顶层 JSON → (有效作者列表, research_tags, 警告)。authors 无一有效则抛 ValueError。"""
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    raw_authors = data.get("authors")
    if not isinstance(raw_authors, list):
        raise ValueError("缺少 authors 数组")

    valid: list[dict] = []
    warnings: list[str] = []
    for i, item in enumerate(raw_authors):
        if not isinstance(item, dict):
            warnings.append(f"authors[{i}] 非对象，跳过")
            continue
        name, seq = item.get("name"), item.get("seq")
        if not isinstance(name, str) or not name.strip():
            warnings.append(f"authors[{i}] 缺 name，跳过")
            continue
        if isinstance(seq, bool) or not isinstance(seq, int):
            warnings.append(f"authors[{i}]（{name.strip()}）缺 seq，跳过")
            continue
        affiliation = item.get("affiliation")
        valid.append(
            {
                "name": name.strip(),
                "seq": seq,
                "affiliation": affiliation
                if isinstance(affiliation, str) and affiliation.strip()
                else None,
                "is_corresponding": bool(item.get("is_corresponding")),
            }
        )
    if not valid:
        raise ValueError(f"无有效作者（{len(warnings)} 条警告）")

    tags_raw = data.get("research_tags") or []
    tags = [t.strip() for t in tags_raw if isinstance(t, str) and t.strip()][:8]
    return valid, tags, warnings


async def extract_paper(
    session: AsyncSession,
    glm: GLMClient,
    http: httpx.AsyncClient,
    paper: Paper,
    input_text: str | None = None,
    used_fulltext: bool = False,
) -> str:
    """单篇抽取。返回 extracted / failed / breaker。"""
    if input_text is None:
        input_text, used_fulltext = await build_input(paper, http)

    try:
        data = await glm.complete_json(
            session,
            system=EXTRACT_SYSTEM,
            user=input_text,
            job_type="glm_extract",
            job_class=JobClass.extract,
            max_tokens=4000,
        )
    except BreakerOpenError:
        return "breaker"  # 恢复后由调度器重扫，不算失败
    except (GLMTransientError, GLMParseError, GLMError) as e:
        await schedule_retry(session, "glm_extract", paper.arxiv_id, f"{type(e).__name__}: {e}")
        paper.status = "extraction_failed"
        return "failed"

    try:
        authors, tags, warnings = validate_extraction(data)
    except ValueError as e:
        await schedule_retry(session, "glm_extract", paper.arxiv_id, f"schema 校验失败: {e}")
        paper.status = "extraction_failed"
        return "failed"

    for w in warnings:
        log.warning("论文 %s 抽取部分容错：%s", paper.arxiv_id, w)

    # 幂等：重试成功时先清旧记录
    await session.execute(delete(PaperAuthor).where(PaperAuthor.paper_id == paper.id))
    for a in authors:
        session.add(
            PaperAuthor(
                paper_id=paper.id,
                author_seq=a["seq"],
                raw_name=a["name"],
                name_confidence=1.0,
                affiliation=a["affiliation"],
            )
        )
    paper.research_tags = tags
    paper.status = "extracted"
    _ = used_fulltext  # 仅供调用方统计
    return "extracted"


async def run_extraction(
    session: AsyncSession,
    glm: GLMClient,
    paper_ids: list[int] | None = None,
    http: httpx.AsyncClient | None = None,
) -> ExtractReport:
    """批量抽取：默认处理全部 pending_extraction 的 AI 相关论文。"""
    report = ExtractReport()

    stmt = select(Paper).where(
        Paper.status == "pending_extraction", Paper.ai_relevant.is_(True)
    )
    if paper_ids is not None:
        # 显式指定（重试执行器）：允许重试 extraction_failed 的论文
        stmt = (
            select(Paper)
            .where(Paper.ai_relevant.is_(True))
            .where(Paper.id.in_(paper_ids))
        )
    papers = (await session.execute(stmt)).scalars().all()

    own_client = http is None
    client = http or httpx.AsyncClient(
        timeout=httpx.Timeout(30.0), headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )
    try:
        for paper in papers:
            input_text, used_ft = await build_input(paper, client)
            report.used_fulltext += 1 if used_ft else 0
            result = await extract_paper(session, glm, client, paper, input_text, used_ft)
            if result == "extracted":
                report.extracted += 1
            elif result == "failed":
                report.failed += 1
            else:
                report.breaker_skipped += 1
            # 一旦熔断就无需继续（后面全部会被跳过）
            if result == "breaker":
                report.breaker_skipped += len(papers) - (report.extracted + report.failed + report.breaker_skipped)
                break
            # 逐篇提交：长时间批次中断/重启时不丢已完成的抽取进度
            await session.commit()
        await session.commit()
    finally:
        if own_client:
            await client.aclose()
    return report
