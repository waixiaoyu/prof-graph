"""T5 采集器单测：解析 / 入库去重 / 单分类失败不中断（FR-1.1/1.6/1.7）。"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx
from sqlalchemy import select

from app.models import FailedJob, Paper
from app.services.collector import collect_all, ingest_papers, parse_rss

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>cs.AI recent papers</title>
    <item>
      <title>Self-Healing Networks with  LLM Agents</title>
      <link>http://arxiv.org/abs/2608.12345v1</link>
      <description>&lt;p&gt;We study closed-loop autonomy&lt;/p&gt;.</description>
      <dc:creator>Wei Zhang (Peking University), Li Wang (Tsinghua University)</dc:creator>
      <dc:date>2026-08-23T18:00:00Z</dc:date>
      <category>cs.AI</category>
      <category>cs.LG</category>
    </item>
    <item>
      <title>GPU Cluster Scheduling
   via Reinforcement Learning</title>
      <link>http://arxiv.org/abs/2608.67890v2</link>
      <description>Abstract text.</description>
      <dc:creator>Solo Author</dc:creator>
      <dc:date>2026-08-22T10:30:00Z</dc:date>
    </item>
    <item>
      <title>no id here</title>
      <link>http://example.com/other</link>
      <description>should be skipped</description>
    </item>
  </channel>
</rss>
"""


def test_parse_rss_fields() -> None:
    rows = parse_rss(RSS_XML, "cs.AI")
    assert len(rows) == 2  # 无 arxiv_id 的条目跳过

    first = rows[0]
    assert first["arxiv_id"] == "2608.12345v1"
    assert first["title"] == "Self-Healing Networks with LLM Agents"  # 折叠空白
    assert first["abstract"] == "We study closed-loop autonomy."  # 去 HTML 标签
    assert first["authors_raw"] == [
        "Wei Zhang (Peking University)",
        "Li Wang (Tsinghua University)",
    ]
    assert first["categories"] == ["cs.AI", "cs.LG"]  # 订阅分类 + 交叉列表标签
    assert first["published_at"] == dt.datetime(
        2026, 8, 23, 18, 0, tzinfo=dt.timezone.utc
    )

    second = rows[1]
    assert second["arxiv_id"] == "2608.67890v2"
    assert second["authors_raw"] == ["Solo Author"]
    assert second["categories"] == ["cs.AI"]


async def test_ingest_and_skip_duplicates(db_session) -> None:
    rows = parse_rss(RSS_XML, "cs.AI")
    added, skipped = await ingest_papers(db_session, rows)
    assert (added, skipped) == (2, 0)

    # 同批再入库：arxiv_id 唯一约束，全部跳过
    added2, skipped2 = await ingest_papers(db_session, rows)
    assert (added2, skipped2) == (0, 2)
    await db_session.commit()

    papers = (await db_session.execute(select(Paper))).scalars().all()
    assert len(papers) == 2


@respx.mock
async def test_collect_all_single_category_failure_continues(db_session) -> None:
    respx.get("https://export.arxiv.org/rss/cs.AI").mock(
        return_value=httpx.Response(200, text=RSS_XML)
    )
    respx.get("https://export.arxiv.org/rss/cs.LG").mock(
        return_value=httpx.Response(500, text="boom")
    )

    report = await collect_all(
        db_session,
        client=httpx.AsyncClient(),
        categories=("cs.AI", "cs.LG"),
    )

    assert report.categories_ok == ["cs.AI"]
    assert report.categories_failed == ["cs.LG"]
    assert (report.added, report.skipped) == (2, 0)

    papers = (await db_session.execute(select(Paper))).scalars().all()
    assert len(papers) == 2

    failures = (
        await db_session.execute(select(FailedJob))
    ).scalars().all()
    assert len(failures) == 1
    assert failures[0].job_type == "rss_fetch"
    assert failures[0].target == "cs.LG"
    assert "500" in failures[0].error or "boom" in failures[0].error
    assert failures[0].next_retry_at is not None


@respx.mock
async def test_collect_all_dedup_cross_listed(db_session) -> None:
    """同一篇论文出现在两个分类的 RSS 中，只入库一次。"""
    for cat in ("cs.AI", "cs.LG"):
        respx.get(f"https://export.arxiv.org/rss/{cat}").mock(
            return_value=httpx.Response(200, text=RSS_XML)
        )

    report = await collect_all(
        db_session, client=httpx.AsyncClient(), categories=("cs.AI", "cs.LG")
    )
    assert (report.added, report.skipped) == (2, 2)
    papers = (await db_session.execute(select(Paper))).scalars().all()
    assert len(papers) == 2
