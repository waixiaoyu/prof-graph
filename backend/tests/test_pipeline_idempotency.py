"""全管线双跑幂等测试（M1 做实，2026-08-26；M2-T15 扩展新链路）。

管线设计为"每轮全量重跑"（run_linker / run_disambiguation 每批无差别
重扫全部已抽取论文），任何一个环节非幂等，数据都会随每轮管线悄悄
膨胀——linker 合作次数事故（修复 1d46d1f）就是这么发生的。本测试用
faked RSS / GLM / OpenAlex 把八阶段管线完整跑两遍，断言所有表计数
纹丝不动、不变量巡检全过，给"重跑安全"整体上锁。

M2-T15：crawl（网页快照→传承关系）与 news（资讯→项目关系）两条新
子链同样各双跑一遍——去重/内容指纹/(rel,page)/(rel,news) 证据幂等
任何一环失守，关系数或证据数都会翻倍。
"""
from __future__ import annotations

import datetime as dt
import json

import httpx
from sqlalchemy import func, select

from app.models import (
    NewsItem,
    Paper,
    PaperAuthor,
    Person,
    Project,
    Relationship,
    RelationshipEvidence,
    RelationshipEvidenceNews,
    RelationshipEvidencePage,
    WebPage,
)
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


# ---------- M2-T15：crawl 子链双跑（网页快照 → 传承关系） ----------

SEED_URL = "https://netsec.ccert.edu.cn/chs/people/"
ROBOTS_URL = "https://netsec.ccert.edu.cn/chs/robots.txt"

SEED_HTML = """<html><head><title>NISL 成员</title></head><body>
<nav>导航</nav>
<h1>网络与信息安全实验室 成员</h1>
<p>段海鑫 教授</p>
<p>张三 博士生（导师：段海鑫）</p>
<footer>页脚</footer>
</body></html>"""

CRAWL_PAGE_JSON = json.dumps({
    "lab_name": "NISL 实验室", "org_school": "清华大学", "org_department": "网络研究院",
    "page_context": "official_lab",
    "members": [
        {"name": "段海鑫", "role": "professor"},
        {"name": "张三", "role": "phd", "advisor": "段海鑫"},
    ],
}, ensure_ascii=False)


def _crawl_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == ROBOTS_URL:
        return httpx.Response(200, text="User-agent: *\nAllow: /")
    if url == SEED_URL:
        return httpx.Response(200, text=SEED_HTML)
    return httpx.Response(404)


async def _crawl_counts(session) -> dict:
    async def n(stmt) -> int:
        return (await session.execute(stmt)).scalar()

    rels = (
        await session.execute(
            select(Relationship).where(Relationship.type == "academic_mentorship")
        )
    ).scalars().all()
    return {
        "web_pages": await n(select(func.count()).select_from(WebPage)),
        "persons_live": await n(
            select(func.count()).select_from(Person).where(Person.merged_into_id.is_(None))
        ),
        "mentor_rels": len(rels),
        "strengths": sorted((r.type, r.subtype, float(r.strength)) for r in rels),
        "evidence_pages": await n(select(func.count()).select_from(RelationshipEvidencePage)),
    }


async def test_crawl_chain_twice_idempotent(db_session):
    """爬取链双跑：内容指纹不变不重抽，关系数/强度/证据数纹丝不动。"""
    glm = GLMClient(transport=_FakeTransport(CRAWL_PAGE_JSON))
    http = httpx.AsyncClient(transport=httpx.MockTransport(_crawl_handler), follow_redirects=True)
    try:
        batch1 = await run_pipeline(db_session, glm=glm, http=http, scope="crawl")
        assert batch1.error is None, batch1.error
        assert batch1.counts["crawl"]["pages_new"] == 1
        assert batch1.counts["mentor_link"]["pages_extracted"] == 1

        snapshot = await _crawl_counts(db_session)
        assert snapshot["web_pages"] == 1
        assert snapshot["mentor_rels"] >= 2  # mentor_student + same_lab
        assert snapshot["evidence_pages"] >= 1

        batch2 = await run_pipeline(db_session, glm=glm, http=http, scope="crawl")
        assert batch2.error is None, batch2.error
        assert batch2.counts["crawl"]["pages_new"] == 0  # URL 去重
        assert batch2.counts["mentor_link"]["pages_extracted"] == 0  # 指纹未变不重抽

        assert await _crawl_counts(db_session) == snapshot
        assert (await check_integrity(db_session))["ok"] is True
    finally:
        await http.aclose()


# ---------- M2-T15：news 子链双跑（资讯 → 项目关系） ----------

FEED_URL = "https://news.example.com/feed.xml"

NEWS_JSON = json.dumps({
    "no_signal": False,
    "persons": [{"name": "张伟", "org": "清华大学", "role": "教授"},
                {"name": "李娜", "org": "北京大学", "role": None}],
    "projects": [{"name": "联合实验室L", "project_type": "联合实验室",
                  "time_start": "2026-03", "time_end": None}],
    "participations": [
        {"person_name": "张伟", "project_name": "联合实验室L",
         "explicitness": "listed_members", "sufficiency": "role_stated"},
        {"person_name": "李娜", "project_name": "联合实验室L",
         "explicitness": "stated_participation", "sufficiency": "mentioned"},
    ],
}, ensure_ascii=False)

NEWS_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>AI 资讯</title>
<item><title>张伟教授团队联合实验室签约</title>
<link>https://news.example.com/a/1.html</link>
<description>两校共建联合实验室</description>
<pubDate>Mon, 24 Aug 2026 08:00:00 GMT</pubDate></item>
</channel></rss>"""


def _news_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) == FEED_URL:
        return httpx.Response(200, text=NEWS_FEED_XML)
    return httpx.Response(404)


async def _news_counts(session) -> dict:
    async def n(stmt) -> int:
        return (await session.execute(stmt)).scalar()

    rels = (
        await session.execute(
            select(Relationship).where(Relationship.type == "project_cooperation")
        )
    ).scalars().all()
    return {
        "news_items": await n(select(func.count()).select_from(NewsItem)),
        "projects": await n(select(func.count()).select_from(Project)),
        "persons_live": await n(
            select(func.count()).select_from(Person).where(Person.merged_into_id.is_(None))
        ),
        "coop_rels": len(rels),
        "coop_counts": sorted((float(r.strength), r.coop_count) for r in rels),
        "evidence_news": await n(select(func.count()).select_from(RelationshipEvidenceNews)),
    }


async def test_news_chain_twice_idempotent(db_session):
    """资讯链双跑：URL 去重 + (rel,news) 证据幂等，关系数/强度/证据数纹丝不动。"""
    import app.services.news_collector as nc
    from app.sources_config import RssSource, SourcesConfig

    test_sources = SourcesConfig(
        rss=(RssSource(id="t-feed", url=FEED_URL, tier="known_media"),), seeds=()
    )
    orig_collect = nc.collect_news

    async def _collect(session, http=None, sources=None):
        return await orig_collect(session, http=http, sources=test_sources)

    glm = GLMClient(transport=_FakeTransport(NEWS_JSON))
    http = httpx.AsyncClient(transport=httpx.MockTransport(_news_handler), follow_redirects=True)
    nc.collect_news = _collect
    try:
        batch1 = await run_pipeline(db_session, glm=glm, http=http, scope="news")
        assert batch1.error is None, batch1.error
        assert batch1.counts["news_collect"]["added"] == 1
        assert batch1.counts["news_link"]["pairs_created"] == 1

        snapshot = await _news_counts(db_session)
        assert snapshot["news_items"] == 1 and snapshot["projects"] == 1
        assert snapshot["coop_rels"] == 1 and snapshot["evidence_news"] == 1

        batch2 = await run_pipeline(db_session, glm=glm, http=http, scope="news")
        assert batch2.error is None, batch2.error
        assert batch2.counts["news_collect"]["added"] == 0  # 条目 URL 去重
        assert batch2.counts["news_link"]["items_extracted"] == 0  # 已抽取不再送 GLM

        assert await _news_counts(db_session) == snapshot
        assert (await check_integrity(db_session))["ok"] is True
    finally:
        nc.collect_news = orig_collect
        await http.aclose()


# ---------- M2 收尾补：管线错误传播 + news 页跨链路由 ----------


async def test_mid_stage_error_recorded_and_downstream_skipped(db_session, monkeypatch):
    """链中阶段异常被管线级兜底接住：错误进批次状态，后续阶段全部不再执行。"""
    import app.services.pipeline as pl

    async def _boom(session):
        raise RuntimeError("tag 阶段炸了")

    monkeypatch.setattr(pl, "run_tagger", _boom)
    http = httpx.AsyncClient(transport=httpx.MockTransport(_http_handler))
    try:
        batch = await run_pipeline(
            db_session, glm=GLMClient(transport=_FakeTransport(EXTRACT_JSON)),
            http=http, categories=("cs.AI",),
        )
        assert batch.error is not None
        assert "RuntimeError" in batch.error and "tag 阶段炸了" in batch.error
        assert batch.stage == "error"
        assert batch.running is False
        # 出错前的阶段已记账；出错阶段及其后的阶段没有执行
        assert "collect" in batch.counts and "filter" in batch.counts
        for later in ("tag", "extract", "openalex", "disambiguate", "link", "crawl", "news_link"):
            assert later not in batch.counts
        # 下游零副作用：论文收进来了，但从未抽取/消歧/建链
        assert (await db_session.execute(select(func.count()).select_from(PaperAuthor))).scalar() == 0
        assert (await db_session.execute(select(func.count()).select_from(Person))).scalar() == 0
    finally:
        await http.aclose()


async def test_news_page_routed_to_news_chain_not_crawl(db_session, monkeypatch):
    """page_type=news 公示页归 news 链处理：crawl 链的 mentor_link 显式排除它。"""
    import app.services.news_collector as nc
    from app.sources_config import SourcesConfig

    page = WebPage(
        url="https://news.univ.edu.cn/2026/joint-lab.html", seed_id="univ-news",
        page_type="news", title="联合实验室签约公示",
        content_text="张伟教授与李娜研究员共建联合实验室……",
        content_hash="hash-route", status="pending_extraction",
        fetched_at=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
    )
    db_session.add(page)
    await db_session.commit()

    glm_crawl = GLMClient(transport=_FakeTransport(CRAWL_PAGE_JSON))
    glm_news = GLMClient(transport=_FakeTransport(NEWS_JSON))
    http = httpx.AsyncClient(transport=httpx.MockTransport(_crawl_handler), follow_redirects=True)
    try:
        # crawl 链：成员种子页被 mentor_link 处理；news 页被排除，原样等待
        batch1 = await run_pipeline(db_session, glm=glm_crawl, http=http, scope="crawl")
        assert batch1.error is None, batch1.error
        assert batch1.counts["mentor_link"]["pages_extracted"] == 1  # 只有成员页
        await db_session.refresh(page)
        assert page.status == "pending_extraction"
        names = {p.name for p in (await db_session.execute(select(Person))).scalars()}
        assert names == {"段海鑫", "张三"}  # 张伟/李娜尚未出现
        assert (await db_session.execute(select(func.count()).select_from(NewsItem))).scalar() == 0

        # news 链：collect 置空（不碰真实 RSS 源），news_link 接手该公示页
        orig_collect = nc.collect_news

        async def _collect(session, http=None, sources=None):
            return await orig_collect(session, http=http, sources=SourcesConfig(rss=(), seeds=()))

        monkeypatch.setattr(nc, "collect_news", _collect)
        batch2 = await run_pipeline(db_session, glm=glm_news, http=http, scope="news")
        assert batch2.error is None, batch2.error
        assert batch2.counts["news_link"]["pages_extracted"] == 1
        assert batch2.counts["news_link"]["pairs_created"] == 1
        await db_session.refresh(page)
        assert page.status == "extracted"
        item = (
            await db_session.execute(select(NewsItem).where(NewsItem.url == page.url))
        ).scalar_one()
        assert (item.rss_entry or {}).get("source") == "webpage"  # 公示页同步条目
        names = {p.name for p in (await db_session.execute(select(Person))).scalars()}
        assert {"张伟", "李娜"} <= names
    finally:
        await http.aclose()
