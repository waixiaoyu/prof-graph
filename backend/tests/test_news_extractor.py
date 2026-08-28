"""T10 单测：§3.3 schema 校验 / 分档映射 / projects upsert 归并 / 新闻页同步。"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.models import NewsItem, Person, PersonOrg, Project, WebPage
from app.services.glm import GLMClient, TransportResult
from app.services.news_extractor import (
    ACCESS_FULLTEXT,
    ACCESS_SUMMARY,
    NEWS_EXTRACT_SYSTEM,
    NewsExtraction,
    NewsProject,
    RSS_ENTRY_WEBPAGE,
    extract_news_item,
    extract_news_page,
    normalize_project_name,
    parse_project_date,
    resolve_news_person,
    sync_news_page_item,
    upsert_project,
    validate_news_extraction,
)

SIGNAL_JSON = """{
  "no_signal": false,
  "persons": [
    {"name": "张伟", "org": "清华大学", "role": "教授"},
    {"name": "李娜", "org": "北京大学", "role": null}
  ],
  "projects": [
    {"name": "国家重点研发计划「大模型安全」项目", "project_type": "国家重点研发",
     "time_start": "2026-03", "time_end": null}
  ],
  "participations": [
    {"person_name": "张伟", "project_name": "国家重点研发计划「大模型安全」项目",
     "explicitness": "listed_members", "sufficiency": "role_stated"},
    {"person_name": "李娜", "project_name": "国家重点研发计划「大模型安全」项目",
     "explicitness": "stated_participation", "sufficiency": "mentioned"}
  ]
}"""


class FakeTransport:
    def __init__(self, text: str):
        self.text = text

    async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
        return TransportResult(self.text, 800, 400)


# ---------- 校验与纯函数 ----------


def test_validate_full_payload() -> None:
    import json

    ext = validate_news_extraction(json.loads(SIGNAL_JSON))
    assert ext.no_signal is False
    assert [p.name for p in ext.persons] == ["张伟", "李娜"]
    assert ext.projects[0].time_start == dt.date(2026, 3, 1)
    assert ext.participations[0].explicitness == "listed_members"


def test_validate_broken_schema_raises() -> None:
    with pytest.raises(ValueError):
        validate_news_extraction([])
    with pytest.raises(ValueError):
        validate_news_extraction({"no_signal": True})  # 缺数组
    with pytest.raises(ValueError):
        validate_news_extraction({"no_signal": True, "persons": {}, "projects": [], "participations": []})


def test_validate_item_tolerance_and_coercion() -> None:
    data = {
        "no_signal": False,
        "persons": [{"name": "  "}, {"name": "王强"}],
        "projects": [{"name": "X 项目", "time_start": "2026-13-99"}],
        "participations": [
            {"person_name": "王强", "project_name": "X 项目",
             "explicitness": "bogus", "sufficiency": "mentioned"},
            {"person_name": "", "project_name": "X 项目",
             "explicitness": "implied", "sufficiency": "minimal"},
        ],
    }
    ext = validate_news_extraction(data)
    assert len(ext.persons) == 1 and len(ext.projects) == 1
    assert ext.participations == []  # 非法枚举/缺名 全部跳过
    assert ext.warnings and ext.no_signal is True  # 无参与对 → 不足以建关系
    assert ext.projects[0].time_start is None  # 非法日期容错


def test_parse_project_date_formats() -> None:
    assert parse_project_date("2026") == dt.date(2026, 1, 1)
    assert parse_project_date("2026-03") == dt.date(2026, 3, 1)
    assert parse_project_date("2026-03-15") == dt.date(2026, 3, 15)
    assert parse_project_date("  ") is None
    assert parse_project_date("2026年3月") is None


def test_normalize_project_name() -> None:
    assert normalize_project_name("  AI Safety   Project ") == "ai safety project"
    assert normalize_project_name("联合实验室A") == normalize_project_name(" 联合实验室A ")


def test_system_prompt_covers_enums() -> None:
    for token in ("listed_members", "stated_participation", "implied", "vague",
                  "detailed_role", "role_stated", "mentioned", "minimal", "no_signal"):
        assert token in NEWS_EXTRACT_SYSTEM


# ---------- projects upsert ----------


async def test_upsert_project_merges_by_normalized_name(db_session):
    """同名项目（空白/大小写差异）两篇报道归并同一 project id（RD-M2-6）。"""
    a = NewsProject(name="AI Safety Project", project_type="other")
    await upsert_project(db_session, a)
    b = NewsProject(name="  ai safety   project ", project_type="企业合作",
                    time_start=dt.date(2026, 1, 1))
    await upsert_project(db_session, b)
    assert a.project_id == b.project_id
    rows = (await db_session.execute(select(Project))).scalars().all()
    assert len(rows) == 1
    assert rows[0].project_type == "other"  # 先到值不被覆盖
    assert rows[0].time_start == dt.date(2026, 1, 1)  # 空位补齐
    assert rows[0].name_normalized == "ai safety project"


async def test_resolve_news_person_reuses_web_disambiguation(db_session):
    """资讯人员：org 锚定强归并到既有 Person，否则新建 0.9。"""
    from app.utils.names import normalize_person_name

    from app.services.openalex import upsert_organization

    org = await upsert_organization(db_session, "清华大学")
    existing = Person(name="张伟", name_normalized=normalize_person_name("张伟"))
    db_session.add(existing)
    await db_session.flush()
    db_session.add(PersonOrg(person_id=existing.id, org_id=org.id, org_confidence=1.0, source="webpage"))

    from app.services.news_extractor import NewsPerson

    np1 = NewsPerson(name="张伟", org="清华大学")
    await resolve_news_person(db_session, np1)
    np2 = NewsPerson(name="王新", org=None)
    await resolve_news_person(db_session, np2)
    assert np1.person_id is not None and np2.person_id is not None
    assert np2.identity == 0.9  # 新建
    total = len((await db_session.execute(select(Person))).scalars().all())
    assert total == 2  # 张伟归并 + 王新建


# ---------- GLM 抽取来源上下文 ----------


async def test_extract_news_item_summary_source(db_session):
    item = NewsItem(
        source_id="unknown-feed", url="https://news.example.com/x.html",
        title="张伟教授获批项目", summary="报道摘要",
        published_at=dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc),
    )
    db_session.add(item)
    await db_session.flush()
    ext = await extract_news_item(db_session, GLMClient(transport=FakeTransport(SIGNAL_JSON)), item)
    assert ext.accessibility == ACCESS_SUMMARY
    assert ext.src_confidence == 0.6  # 未知源 other 档
    assert "unknown-feed" in ext.source_desc


async def test_extract_news_page_fulltext_source(db_session):
    page = WebPage(
        url="https://news.univ.edu.cn/2026/0812.html", seed_id="univ-news",
        page_type="news", title="新闻公示", content_text="全文……",
        content_hash="h1", status="pending_extraction",
    )
    db_session.add(page)
    await db_session.flush()
    ext = await extract_news_page(db_session, GLMClient(transport=FakeTransport(SIGNAL_JSON)), page)
    assert ext.src_confidence == 1.0  # 高校官网新闻页（RD-M2-11）
    assert ext.accessibility == ACCESS_FULLTEXT


# ---------- 新闻页 ↔ NewsItem 同步 ----------


async def test_sync_news_page_item(db_session):
    page = WebPage(
        url="https://news.univ.edu.cn/2026/0812.html", seed_id="univ-news",
        page_type="news", title="联合实验室签约公示",
        content_text="正文" * 10, content_hash="h1", status="pending_extraction",
        fetched_at=dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc),
    )
    db_session.add(page)
    await db_session.flush()

    item = await sync_news_page_item(db_session, page)
    assert item.rss_entry["source"] == RSS_ENTRY_WEBPAGE
    assert item.source_id == "univ-news" and item.status == "pending_screen"
    assert item.published_at == dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc)

    again = await sync_news_page_item(db_session, page)
    assert again.id == item.id
    total = len((await db_session.execute(select(NewsItem))).scalars().all())
    assert total == 1


async def test_sync_news_page_item_fallback_fields(db_session):
    """公示页缺 title/fetched_at：title 回退 url、published_at 回退当前时间。"""
    page = WebPage(
        url="https://news.univ.edu.cn/2026/0813.html", seed_id="univ-news",
        page_type="news", title=None, content_text="c", content_hash="h1",
        status="pending_extraction", fetched_at=None,
    )
    db_session.add(page)
    await db_session.flush()

    item = await sync_news_page_item(db_session, page)
    assert item.title == page.url
    assert item.published_at is not None and item.published_at.tzinfo is not None


class _RecordingTransport:
    """记录 user prompt 供断言（截断验证）。"""

    def __init__(self, text: str):
        self.text = text
        self.user: str | None = None

    async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
        self.user = user
        return TransportResult(self.text, 800, 400)


async def test_extract_news_page_truncates_long_content(db_session):
    """超长公示页正文截断到 MAX_CHARS，prompt 不随页面长度无限膨胀。"""
    from app.services.page_extractor import MAX_CHARS

    page = WebPage(
        url="https://news.univ.edu.cn/long.html", seed_id="univ-news",
        page_type="news", title="t", content_text="字" * (MAX_CHARS + 5000),
        content_hash="h1", status="pending_extraction",
    )
    db_session.add(page)
    await db_session.flush()

    rec = _RecordingTransport(SIGNAL_JSON)
    await extract_news_page(db_session, GLMClient(transport=rec), page)
    assert rec.user is not None and len(rec.user) == MAX_CHARS


async def test_extract_news_item_known_media_confidence(db_session):
    """sources.yaml 里的 known_media 源（qbitai）→ 数据源可信度 0.8。"""
    item = NewsItem(
        source_id="qbitai", url="https://www.qbitai.com/x.html",
        title="张伟教授获批项目", summary="s",
        published_at=dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc),
    )
    db_session.add(item)
    await db_session.flush()
    ext = await extract_news_item(db_session, GLMClient(transport=FakeTransport(SIGNAL_JSON)), item)
    assert ext.src_confidence == 0.8
    assert ext.accessibility == ACCESS_SUMMARY


async def test_resolve_news_person_attaches_glm_org_anchor(db_session):
    """非强归并（新建/打分）人员补挂 GLM 机构锚（source='glm' 0.6）；
    再次同名同机构强归并命中后不重复挂锚。"""
    from app.models import PersonOrg as PO
    from app.services.news_extractor import NewsPerson

    np = NewsPerson(name="赵六", org="复旦大学")
    await resolve_news_person(db_session, np)
    assert np.identity == 0.9 and np.person_id is not None
    anchors = (
        await db_session.execute(
            select(PO).where(PO.person_id == np.person_id, PO.source == "glm")
        )
    ).scalars().all()
    assert len(anchors) == 1 and float(anchors[0].org_confidence) == 0.6

    # 同名同机构再次出现：强归并到同一 Person，机构锚不重复
    np2 = NewsPerson(name="赵六", org="复旦大学")
    await resolve_news_person(db_session, np2)
    assert np2.person_id == np.person_id
    assert np2.identity == 1.0
    anchors2 = (
        await db_session.execute(
            select(PO).where(PO.person_id == np.person_id, PO.source == "glm")
        )
    ).scalars().all()
    assert len(anchors2) == 1
