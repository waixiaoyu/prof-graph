"""T11 单测：§4.2 分档数值 / 同项目两两配对 / 证据幂等 / run_news_link 编排。"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import func, select

from app.models import (
    FailedJob,
    NewsItem,
    Person,
    Project,
    Relationship,
    RelationshipEvidenceNews,
    WebPage,
)
from app.services.breaker import BreakerOpenError
from app.services.glm import GLMClient, TransportResult
from app.services.news_extractor import (
    ACCESS_FULLTEXT,
    ACCESS_SUMMARY,
    NewsExtraction,
    NewsPerson,
    NewsProject,
    Participation,
)
from app.services.project_linker import (
    compute_confidence,
    link_news_relations,
    run_news_link,
    tier_strength,
)

# 分档数值算例（plan §4.2）
TIER_CASES = [(1, 0.90), (2, 0.95), (3, 1.00), (7, 1.00)]


def _ext(**kw) -> NewsExtraction:
    defaults = dict(
        src_confidence=0.8, accessibility=ACCESS_SUMMARY, source_desc="RSS 资讯（t）"
    )
    defaults.update(kw)
    return NewsExtraction(**defaults)


def _pair_news_item(db_session, url="https://news.example.com/n1.html", published=None):
    item = NewsItem(
        source_id="ai-news", url=url, title="联合实验室签约",
        summary="张伟教授与李娜研究员合作签约",
        published_at=published or dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc),
    )
    db_session.add(item)
    return item


async def _mk_item(db_session, **kw) -> NewsItem:
    item = _pair_news_item(db_session, **kw)
    await db_session.flush()
    return item


# ---------- 公式（纯函数） ----------


@pytest.mark.parametrize("coop,expected", TIER_CASES)
def test_tier_strength(coop, expected) -> None:
    assert tier_strength(coop) == expected


def test_compute_confidence_plan_examples() -> None:
    # 0.3×1.0 + 0.3×0.8 + 0.2×0.8 + 0.2×0.5 = 0.8
    assert compute_confidence("listed_members", "role_stated", 0.8, ACCESS_SUMMARY) == 0.8
    # 0.3×0.5 + 0.3×0.5 + 0.2×1.0 + 0.2×0.8 = 0.66（新闻页全文）
    assert compute_confidence("implied", "mentioned", 1.0, ACCESS_FULLTEXT) == 0.66
    # 0.3×0.3 + 0.3×0.3 + 0.2×0.6 + 0.2×0.5 = 0.4（最弱档）
    assert compute_confidence("vague", "minimal", 0.6, ACCESS_SUMMARY) == 0.4


# ---------- 配对与幂等 ----------


async def test_link_news_relations_pairs_and_projects(db_session):
    """同项目两两配对 + projects 入库 + 置信度/强度落库。"""
    item = await _mk_item(db_session)
    ext = _ext(
        persons=[
            NewsPerson(name="张伟", org="清华大学"),
            NewsPerson(name="李娜", org="北京大学"),
            NewsPerson(name="王强", org=None),
        ],
        projects=[NewsProject(name="大模型安全联合实验室", project_type="联合实验室")],
        participations=[
            Participation("张伟", "大模型安全联合实验室", "listed_members", "detailed_role"),
            Participation("李娜", "大模型安全联合实验室", "stated_participation", "role_stated"),
            Participation("王强", "大模型安全联合实验室", "implied", "mentioned"),
        ],
        no_signal=False,
    )
    stats = await link_news_relations(db_session, ext, item)

    assert stats["created"] == 3  # C(3,2) 两两
    rels = (
        await db_session.execute(
            select(Relationship).where(Relationship.type == "project_cooperation")
        )
    ).scalars().all()
    assert len(rels) == 3
    by_pair = {(r.person_a_id, r.person_b_id): r for r in rels}
    assert all(r.subtype == "" and r.coop_count == 1 for r in rels)
    assert all(float(r.strength) == 0.81 for r in rels)  # identity 0.9 × tier(1) 0.90

    # 置信度分档差异（张伟-李娜 listed/role_stated 取两端各自参与记录的一档）
    proj = (await db_session.execute(select(Project))).scalar_one()
    assert proj.name_normalized == "大模型安全联合实验室"
    assert proj.project_type == "联合实验室"
    # 证据锚点：3 对关系各挂 1 条 news 证据
    n_ev = (
        await db_session.execute(select(func.count()).select_from(RelationshipEvidenceNews))
    ).scalar_one()
    assert n_ev == 3
    # 时间范围取资讯 published_at
    assert all(r.time_start == dt.date(2026, 8, 25) for r in rels)


async def test_evidence_idempotent_rerun(db_session):
    """同一资讯重跑：证据已存在 → dup，不重复计。"""
    item = await _mk_item(db_session)
    ext = _ext(
        persons=[NewsPerson(name="张伟", org="清华大学"), NewsPerson(name="李娜", org="北京大学")],
        projects=[NewsProject(name="P 项目")],
        participations=[Participation("张伟", "P 项目", "listed_members", "role_stated"),
                        Participation("李娜", "P 项目", "listed_members", "role_stated")],
        no_signal=False,
    )
    s1 = await link_news_relations(db_session, ext, item)
    s2 = await link_news_relations(db_session, ext, item)
    assert s1["created"] == 1 and s2["dup"] == 1
    n_rel = (
        await db_session.execute(
            select(func.count()).select_from(Relationship).where(
                Relationship.type == "project_cooperation")
        )
    ).scalar_one()
    assert n_rel == 1


async def test_two_news_merge_coop2(db_session):
    """不同项目 A、B 两篇报道：证据合并 coop_count=2，strength 重查 0.95。"""
    item_a = await _mk_item(db_session, url="https://news.example.com/a.html")
    item_b = await _mk_item(
        db_session, url="https://news.example.com/b.html",
        published=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc),
    )
    # org 锚定：第二篇报道的人强归并到第一篇建立的同名 Person（identity 1.0）
    persons = lambda: [NewsPerson(name="张伟", org="清华大学"),
                       NewsPerson(name="李娜", org="北京大学")]  # noqa: E731
    s1 = await link_news_relations(
        db_session,
        _ext(persons=persons(), projects=[NewsProject(name="项目A")],
             participations=[Participation("张伟", "项目A", "listed_members", "role_stated"),
                             Participation("李娜", "项目A", "listed_members", "role_stated")],
             no_signal=False),
        item_a,
    )
    s2 = await link_news_relations(
        db_session,
        _ext(persons=persons(), projects=[NewsProject(name="项目B")],
             participations=[Participation("张伟", "项目B", "stated_participation", "mentioned"),
                             Participation("李娜", "项目B", "stated_participation", "mentioned")],
             no_signal=False),
        item_b,
    )
    assert s1["created"] == 1 and s2["merged"] == 1
    rel = (
        await db_session.execute(
            select(Relationship).where(Relationship.type == "project_cooperation")
        )
    ).scalar_one()
    assert rel.coop_count == 2
    assert float(rel.strength) == 0.95  # 0.9 × tier(2)
    # 时间范围并集
    assert rel.time_start == dt.date(2026, 8, 25) and rel.time_end == dt.date(2026, 9, 1)
    # 两个项目实体独立归并
    n_proj = (await db_session.execute(select(func.count()).select_from(Project))).scalar_one()
    assert n_proj == 2


async def test_same_person_mentions_dedupe(db_session):
    """同页同人多名（消歧后同 person）不建自环。"""
    item = await _mk_item(db_session)
    ext = _ext(
        persons=[NewsPerson(name="张伟"), NewsPerson(name="张伟")],
        projects=[NewsProject(name="P")],
        participations=[Participation("张伟", "P", "listed_members", "role_stated"),
                        Participation("张伟", "P", "implied", "mentioned")],
        no_signal=False,
    )
    stats = await link_news_relations(db_session, ext, item)
    assert stats["created"] == 0  # 两名消歧为同一人 → 无对可建
    n_rel = (
        await db_session.execute(
            select(func.count()).select_from(Relationship).where(
                Relationship.type == "project_cooperation")
        )
    ).scalar_one()
    assert n_rel == 0


async def test_identity_not_degraded_by_weaker_evidence(db_session):
    """identity 历史最好：弱证据合并不降低已确定的身份置信度。"""
    anchored = lambda: [NewsPerson(name="张伟", org="清华大学"),  # noqa: E731
                        NewsPerson(name="李娜", org="北京大学")]
    # 第 1 篇：新建 0.9
    item1 = await _mk_item(db_session, url="https://news.example.com/i1.html")
    await link_news_relations(
        db_session, _ext(persons=anchored(), projects=[NewsProject(name="项目一")],
                         participations=[Participation("张伟", "项目一", "listed_members", "role_stated"),
                                         Participation("李娜", "项目一", "listed_members", "role_stated")],
                         no_signal=False),
        item1,
    )
    rel = (
        await db_session.execute(
            select(Relationship).where(Relationship.type == "project_cooperation")
        )
    ).scalar_one()
    assert float(rel.identity_confidence) == 0.9

    # 第 2 篇：同名同机构强归并 1.0 → identity 升到 1.0
    item2 = await _mk_item(db_session, url="https://news.example.com/i2.html")
    await link_news_relations(
        db_session, _ext(persons=anchored(), projects=[NewsProject(name="项目二")],
                         participations=[Participation("张伟", "项目二", "listed_members", "role_stated"),
                                         Participation("李娜", "项目二", "listed_members", "role_stated")],
                         no_signal=False),
        item2,
    )
    await db_session.refresh(rel)
    assert float(rel.identity_confidence) == 1.0

    # 第 3 篇：弱证据（identity 0.7，同名无机构不并档——消歧保守，直接以
    # 低 identity 参与对调用配对，验证关系层 identity 取历史最好不回退）
    from app.services.project_linker import link_project_pair

    item3 = await _mk_item(db_session, url="https://news.example.com/i3.html")
    weak_a, weak_b = NewsPerson(name="张伟"), NewsPerson(name="李娜")
    weak_a.person_id, weak_a.identity = rel.person_a_id, 0.7
    weak_b.person_id, weak_b.identity = rel.person_b_id, 0.7
    outcome = await link_project_pair(
        db_session, weak_a, weak_b,
        Participation("张伟", "项目三", "vague", "minimal"),
        _ext(src_confidence=0.6), item3,
    )
    assert outcome == "merged"
    await db_session.flush()  # 配对内只改内存不 flush（真实链路由 _process_item commit）
    await db_session.refresh(rel)
    assert float(rel.identity_confidence) == 1.0  # max 语义，不回退
    assert rel.coop_count == 3
    assert float(rel.strength) == 1.0  # 1.0 × tier(3)
    assert "共 3 项合作证据" in rel.evidence_summary


async def test_participation_referencing_unknown_entities_skipped(db_session):
    """参与对引用的人员/项目不在 persons/projects 抽取结果中 → 容错跳过，
    不为悬空名字新建 Person。"""
    item = await _mk_item(db_session)
    ext = _ext(
        persons=[NewsPerson(name="张伟"), NewsPerson(name="李娜")],
        projects=[NewsProject(name="P")],
        participations=[
            Participation("张伟", "P", "listed_members", "role_stated"),
            Participation("路人甲", "P", "listed_members", "role_stated"),   # 人不在 persons
            Participation("李娜", "幽灵项目", "listed_members", "role_stated"),  # 项目不在 projects
        ],
        no_signal=False,
    )
    stats = await link_news_relations(db_session, ext, item)
    assert stats["created"] == 0  # 张伟-路人甲/李娜-幽灵 均无有效配对
    names = {
        p.name for p in (await db_session.execute(select(Person))).scalars()
    }
    assert "路人甲" not in names  # 悬空引用不建实体


# ---------- run_news_link 编排 ----------


class FakeTransport:
    def __init__(self, text: str | Exception):
        self.text = text

    async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
        if isinstance(self.text, Exception):
            raise self.text
        return TransportResult(self.text, 800, 400)


SIGNAL_JSON = json.dumps({
    "no_signal": False,
    "persons": [{"name": "张伟", "org": "清华大学", "role": "教授"},
                {"name": "李娜", "org": None, "role": None}],
    "projects": [{"name": "重点项目X", "project_type": None,
                  "time_start": "2026-03", "time_end": None}],
    "participations": [
        {"person_name": "张伟", "project_name": "重点项目X",
         "explicitness": "listed_members", "sufficiency": "role_stated"},
        {"person_name": "李娜", "project_name": "重点项目X",
         "explicitness": "stated_participation", "sufficiency": "mentioned"},
    ],
}, ensure_ascii=False)

NO_SIGNAL_JSON = json.dumps(
    {"no_signal": True, "persons": [], "projects": [], "participations": []}
)


async def test_run_news_link_rss_flow(db_session):
    item = await _mk_item(db_session, url="https://news.example.com/rss-1.html")
    await db_session.commit()
    report = await run_news_link(db_session, GLMClient(transport=FakeTransport(SIGNAL_JSON)))
    assert report.items_extracted == 1 and report.pairs_created == 1
    await db_session.refresh(item)
    assert item.status == "extracted"
    # 重跑：无待处理条目
    report2 = await run_news_link(db_session, GLMClient(transport=FakeTransport(SIGNAL_JSON)))
    assert report2.items_extracted == 0


async def test_run_news_link_no_signal_short_circuit(db_session):
    item = await _mk_item(db_session, url="https://news.example.com/rss-2.html")
    await db_session.commit()
    report = await run_news_link(db_session, GLMClient(transport=FakeTransport(NO_SIGNAL_JSON)))
    assert report.items_no_signal == 1 and report.pairs_created == 0
    await db_session.refresh(item)
    assert item.status == "no_signal"


async def test_run_news_link_failure_schedules_retry(db_session):
    item = await _mk_item(db_session, url="https://news.example.com/rss-3.html")
    await db_session.commit()
    report = await run_news_link(
        db_session,
        GLMClient(transport=FakeTransport(ValueError("JSON 破损"))),
    )
    assert report.items_failed == 1
    await db_session.refresh(item)
    assert item.status == "extraction_failed"
    job = (
        await db_session.execute(select(FailedJob).where(FailedJob.job_type == "news_extract"))
    ).scalar_one()
    assert job.target == item.url and job.status == "retrying"
    # 修复后重跑（状态回扫）成功
    report2 = await run_news_link(db_session, GLMClient(transport=FakeTransport(SIGNAL_JSON)))
    assert report2.items_extracted == 1


async def test_run_news_link_breaker_skips(db_session):
    await _mk_item(db_session, url="https://news.example.com/rss-4.html")
    await db_session.commit()
    report = await run_news_link(
        db_session, GLMClient(transport=FakeTransport(BreakerOpenError("budget", "日预算耗尽")))
    )
    assert report.breaker_skipped == 1


async def test_run_news_link_news_page_flow(db_session):
    """新闻公示页（RD-M2-11）：同步 NewsItem → 抽取 → 建链，page 状态联动。"""
    page = WebPage(
        url="https://news.univ.edu.cn/2026/lab.html", seed_id="univ-news",
        page_type="news", title="联合实验室签约公示",
        content_text="张伟教授与李娜研究员共建联合实验室……",
        content_hash="hash-1", status="pending_extraction",
        fetched_at=dt.datetime(2026, 8, 12, tzinfo=dt.timezone.utc),
    )
    db_session.add(page)
    await db_session.commit()

    report = await run_news_link(db_session, GLMClient(transport=FakeTransport(SIGNAL_JSON)))
    assert report.pages_extracted == 1 and report.pairs_created == 1
    await db_session.refresh(page)
    assert page.status == "extracted" and page.last_extracted_hash == "hash-1"
    item = (
        await db_session.execute(select(NewsItem).where(NewsItem.url == page.url))
    ).scalar_one()
    assert item.status == "extracted"
    rel = (
        await db_session.execute(
            select(Relationship).where(Relationship.type == "project_cooperation")
        )
    ).scalar_one()
    assert float(rel.strength) == 0.81  # identity 0.9 × tier(1) 0.90
    # 新闻页来源 src=1.0 / 全文 0.8 → 置信度（listed 1.0/role_stated 0.8）：
    # 0.3×1.0+0.3×0.8+0.2×1.0+0.2×0.8 = 0.9
    assert "置信度 0.90" in rel.evidence_summary


async def test_run_news_link_explicit_news_ids_ignores_status(db_session):
    """显式 news_ids（重试执行器路径）：已 extracted 的条目也重跑，证据幂等去重。"""
    item = await _mk_item(db_session, url="https://news.example.com/rss-5.html")
    item.status = "extracted"  # 已处理过（如需重跑修正）
    await db_session.commit()

    report = await run_news_link(
        db_session, GLMClient(transport=FakeTransport(SIGNAL_JSON)),
        news_ids=[item.id], page_ids=[],
    )
    assert report.items_extracted == 1
    assert report.pairs_dup == 1 or report.pairs_created == 1  # 证据幂等
    n_rel = (
        await db_session.execute(
            select(func.count()).select_from(Relationship).where(
                Relationship.type == "project_cooperation")
        )
    ).scalar_one()
    assert n_rel == 1


async def test_run_news_link_skips_webpage_source_items(db_session):
    """rss_entry.source=webpage 的条目在 news_ids 路径跳过（由 page 路径负责）。"""
    item = await _mk_item(db_session, url="https://news.univ.edu.cn/w1.html")
    item.rss_entry = {"source": "webpage"}
    await db_session.commit()

    report = await run_news_link(
        db_session, GLMClient(transport=FakeTransport(SIGNAL_JSON)),
        news_ids=[item.id], page_ids=[],
    )
    assert report.items_extracted == 0
    await db_session.refresh(item)
    assert item.status == "pending_screen"  # 原状


async def test_run_news_link_news_page_no_signal(db_session):
    """新闻公示页无信号：page 与同步条目都置 no_signal，零关系。"""
    page = WebPage(
        url="https://news.univ.edu.cn/2026/none.html", seed_id="univ-news",
        page_type="news", title="无关通知", content_text="放假安排",
        content_hash="hash-2", status="pending_extraction",
        fetched_at=dt.datetime(2026, 8, 13, tzinfo=dt.timezone.utc),
    )
    db_session.add(page)
    await db_session.commit()

    report = await run_news_link(db_session, GLMClient(transport=FakeTransport(NO_SIGNAL_JSON)))
    assert report.pages_extracted == 0 and report.items_no_signal == 1
    await db_session.refresh(page)
    assert page.status == "no_signal"
    item = (
        await db_session.execute(select(NewsItem).where(NewsItem.url == page.url))
    ).scalar_one()
    assert item.status == "no_signal"
    assert (
        await db_session.execute(
            select(func.count()).select_from(Relationship).where(
                Relationship.type == "project_cooperation")
        )
    ).scalar_one() == 0
