"""全管线双跑幂等测试（M1 做实，2026-08-26）。

管线设计为"每轮全量重跑"（run_linker / run_disambiguation 每批无差别
重扫全部已抽取论文），任何一个环节非幂等，数据都会随每轮管线悄悄
膨胀——linker 合作次数事故（修复 1d46d1f）就是这么发生的。本测试用
faked RSS / GLM / OpenAlex 把八阶段管线完整跑两遍，断言所有表计数
纹丝不动、不变量巡检全过，给"重跑安全"整体上锁。
"""
from __future__ import annotations

import json

import httpx
from sqlalchemy import func, select

from app.models import Paper, PaperAuthor, Person, Relationship, RelationshipEvidence
from app.services.glm import GLMClient, TransportResult
from app.services.integrity import check_integrity
from app.services.pipeline import run_pipeline

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>cs.AI recent papers</title>
    <item>
      <title>Self-Healing Networks with LLM Agents</title>
      <link>http://arxiv.org/abs/2608.12345v1</link>
      <description>&lt;p&gt;We study closed-loop autonomy&lt;/p&gt;.</description>
      <dc:creator>Wei Zhang (Peking University), Li Wang (Tsinghua University)</dc:creator>
      <dc:date>2026-08-23T18:00:00Z</dc:date>
      <category>cs.AI</category>
    </item>
  </channel>
</rss>
"""

EXTRACT_JSON = json.dumps({
    "authors": [
        {"name": "Wei Zhang", "seq": 0, "affiliation": "Peking University", "is_corresponding": True},
        {"name": "Li Wang", "seq": 1, "affiliation": "Tsinghua University", "is_corresponding": False},
    ],
    "research_tags": ["llm agent"],
})


class _FakeTransport:
    def __init__(self, text: str):
        self._text = text

    async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
        return TransportResult(self._text, 1500, 1000)


def _http_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "api.openalex.org" in url:
        if "/works/https" in url:  # DOI 直查未收录 → 404 回退标题搜索
            return httpx.Response(404)
        return httpx.Response(200, json={"results": []})  # 标题搜索无命中
    return httpx.Response(200, text=RSS_XML)  # arXiv RSS


async def _counts(session) -> dict:
    async def n(stmt) -> int:
        return (await session.execute(stmt)).scalar()

    return {
        "papers": await n(select(func.count()).select_from(Paper)),
        "authors": await n(select(func.count()).select_from(PaperAuthor)),
        "persons_live": await n(
            select(func.count()).select_from(Person).where(Person.merged_into_id.is_(None))
        ),
        "relationships": await n(select(func.count()).select_from(Relationship)),
        "evidence": await n(select(func.count()).select_from(RelationshipEvidence)),
    }


async def test_pipeline_twice_leaves_all_counts_unchanged(db_session):
    http = httpx.AsyncClient(transport=httpx.MockTransport(_http_handler))
    try:
        batch1 = await run_pipeline(
            db_session, glm=GLMClient(transport=_FakeTransport(EXTRACT_JSON)),
            http=http, categories=("cs.AI",),
        )
        assert batch1.error is None, batch1.error
        assert batch1.counts["collect"]["added"] == 1

        snapshot = await _counts(db_session)
        assert snapshot == {
            "papers": 1, "authors": 2, "persons_live": 2,
            "relationships": 1, "evidence": 1,
        }

        batch2 = await run_pipeline(
            db_session, glm=GLMClient(transport=_FakeTransport(EXTRACT_JSON)),
            http=http, categories=("cs.AI",),
        )
        assert batch2.error is None, batch2.error
        assert batch2.counts["collect"]["added"] == 0
        assert batch2.counts["collect"]["skipped"] == 1

        assert await _counts(db_session) == snapshot
        assert (await check_integrity(db_session))["ok"] is True
    finally:
        await http.aclose()
