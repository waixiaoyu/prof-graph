"""T11 单测：同人不同写归并 / 重名新建 / 中间分入队（FR-3.1~3.3）。"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.models import (
    DisambiguationQueue,
    Organization,
    Paper,
    PaperAuthor,
    Person,
    PersonOrg,
)
from app.services.disambiguator import (
    find_candidates,
    process_author,
    run_disambiguation,
    score_name,
    score_network,
    score_org,
    score_research,
    score_time,
)
from app.services.openalex import upsert_organization
from app.utils.names import normalize_name, normalize_person_name

D = dt.datetime


async def _mk_paper(db_session, *, arxiv_id: str, title="T", tags=None, date=None,
                    status="extracted") -> Paper:
    paper = Paper(
        arxiv_id=arxiv_id, title=title, abstract="A",
        authors_raw=[], categories=["cs.AI"], status=status,
        research_tags=tags or [], published_at=date,
    )
    db_session.add(paper)
    await db_session.flush()
    return paper


async def _mk_person_with_history(
    db_session, *, name: str, org: str | None, papers_meta: list[tuple[list[str], dt.date, list[str]]],
    openalex_id: str | None = None,
) -> Person:
    """建 Person + 历史论文（tags, date, coauthor_raw_names）。"""
    person = Person(name=name, name_normalized=normalize_name(name), openalex_id=openalex_id)
    db_session.add(person)
    await db_session.flush()
    if org:
        o = await upsert_organization(db_session, org)
        db_session.add(PersonOrg(person_id=person.id, org_id=o.id, org_confidence=1.0, source="glm"))
    for i, (tags, date, coauthors) in enumerate(papers_meta):
        paper = Paper(
            arxiv_id=f"hist-{person.id}-{i}", title=f"H{i}", abstract="",
            authors_raw=[name] + coauthors, categories=["cs.AI"], status="extracted",
            research_tags=tags, published_at=date,
        )
        db_session.add(paper)
        await db_session.flush()
        db_session.add(PaperAuthor(paper_id=paper.id, author_seq=0, raw_name=name,
                                   person_id=person.id))
        for j, ca in enumerate(coauthors, 1):
            db_session.add(PaperAuthor(paper_id=paper.id, author_seq=j, raw_name=ca))
    await db_session.flush()
    return person


# ---------- 打分纯函数 ----------

def test_score_name_variants() -> None:
    assert score_name("Wei Zhang", "Wei Zhang") == 1.0
    # 姓名序颠倒取较优（归一化前的原始名比较）
    assert score_name("Zhang Wei", "Wei Zhang") == 1.0
    # 缩写变体：归一后不一致 → 0.2（无相似档）
    assert score_name("W. Zhang", "Wei Zhang") == 0.2
    assert score_name("Aaa Bbbccc", "Zzz") == 0.2


def test_score_name_one_letter_off_is_different() -> None:
    """2026-08-29 修订（spec §9-2）：拼音差一字母即不同人，无相似档。

    两例均为生产复核队列真实误报（M1 spec §9-2 实例子）。
    """
    # #4815 Yan Fan vs #5772 杨帆：yanfan vs yangfan 差一个 g
    assert score_name("Yan Fan", "杨帆") == 0.2
    assert score_name("杨帆", "Yan Fan") == 0.2
    # #3239 Bo Zheng vs #5783 卜衡：bozheng vs buheng（姓氏词典修正后仍非同名）
    assert score_name("Bo Zheng", "卜衡") == 0.2
    # 长英文名差一字母同样不同（原 ≥0.95 档对长名容忍 1 字母差）
    assert score_name("Michael Wang", "Micheal Wang") == 0.2
    # 中文↔英文精确同域不变（M2-T4 RD-M2-12）
    assert score_name("张三", "Zhang San") == 1.0


def test_score_factors() -> None:
    assert score_org("Peking University", {"peking"}) == 1.0
    assert score_org(None, {"peking"}) == 0.4
    assert score_org("Peking University", set()) == 0.4
    assert score_research({"a", "b"}, {"b", "c"}) == 1 / 3
    assert score_research(set(), set()) == 0.5
    assert score_time(D(2025, 3, 1).date(), D(2024, 1, 1).date(), D(2026, 1, 1).date()) == 1.0
    assert score_time(D(2026, 6, 1).date(), D(2024, 1, 1).date(), D(2026, 1, 1).date()) == 0.6
    assert score_time(D(2030, 3, 1).date(), D(2024, 1, 1).date(), D(2026, 1, 1).date()) == 0.3
    assert score_network(2) == 1.0 and score_network(1) == 0.6 and score_network(0) == 0.2


# ---------- 主流程 ----------


async def test_find_candidates_exact_only(db_session) -> None:
    """2026-08-29 修订（spec §9-2）：候选=归一精确（含颠倒序）；
    差一字母的名字不再是候选（原编辑距离 ≤2 模糊取消）。
    """
    yang = Person(name="杨帆", name_normalized=normalize_person_name("杨帆"))
    zhang = Person(name="Zhang San", name_normalized=normalize_name("Zhang San"))
    db_session.add_all([yang, zhang])
    await db_session.flush()

    # 颠倒序精确命中
    assert {p.id for p in await find_candidates(db_session, "San Zhang")} == {zhang.id}
    # 中文拼音与英文同域精确命中（M2-T4 不回退）
    assert {p.id for p in await find_candidates(db_session, "张三")} == {zhang.id}
    # Yan Fan vs 杨帆（yanfan/yangfan 差一个 g）→ 无候选
    assert await find_candidates(db_session, "Yan Fan") == []

async def test_same_person_reordered_name_merged(db_session) -> None:
    """同人不同写（姓名序颠倒 + 同机构 + 共享合作者）→ 自动归并 ≥0.8。"""
    await _mk_person_with_history(
        db_session, name="Wei Zhang", org="Peking University",
        papers_meta=[
            (["llm agent", "planning"], D(2024, 6, 1), ["Li Wang", "Anna Lee"]),
            (["llm agent"], D(2025, 9, 1), ["Li Wang"]),
        ],
    )
    paper = await _mk_paper(db_session, arxiv_id="2608.300", tags=["llm agent"],
                            date=D(2025, 10, 1))
    pa = PaperAuthor(paper_id=paper.id, author_seq=0, raw_name="Zhang Wei",
                     affiliation="Peking University")
    db_session.add(pa)
    await db_session.flush()

    result = await process_author(db_session, pa, paper, {"zhangwei", "liwang", "annalee"})
    await db_session.commit()

    assert result == "linked_existing"
    person = (await db_session.execute(select(Person))).scalars().one()
    assert pa.person_id == person.id
    assert person.name == "Wei Zhang"


async def test_same_name_different_person_created(db_session) -> None:
    """重名不同人（机构不同、无交集）→ 总分 <0.5 → 新建 Person。"""
    await _mk_person_with_history(
        db_session, name="Li Wang", org="Tsinghua University",
        papers_meta=[(["wireless"], D(2020, 1, 1), ["Old Coauthor"])],
    )
    paper = await _mk_paper(db_session, arxiv_id="2608.301", tags=["gpu scheduling"],
                            date=D(2026, 8, 1))
    pa = PaperAuthor(paper_id=paper.id, author_seq=0, raw_name="Li Wang",
                     affiliation="Huawei")
    db_session.add(pa)
    await db_session.flush()

    result = await process_author(db_session, pa, paper, {"liwang"})
    assert result == "created"
    persons = (await db_session.execute(select(Person))).scalars().all()
    assert len(persons) == 2  # 原 Person + 新建


async def test_middle_score_queued_with_detail(db_session) -> None:
    """中间分数入队，score_detail 五因素完整。"""
    await _mk_person_with_history(
        db_session, name="Anna Lee", org="MIT",
        papers_meta=[(["networking"], D(2025, 1, 1), ["Bob Chen"])],
    )
    paper = await _mk_paper(db_session, arxiv_id="2608.302", tags=["networking", "llm agent"],
                            date=D(2026, 8, 1))
    # 精确同名（name 1.0）但机构不同（0.4）、部分标签交集、无共享合作者
    pa = PaperAuthor(paper_id=paper.id, author_seq=0, raw_name="Anna Lee",
                     affiliation="Stanford")
    db_session.add(pa)
    await db_session.flush()

    result = await process_author(db_session, pa, paper, {"annalee"})
    await db_session.commit()

    assert result == "queued"
    q = (await db_session.execute(select(DisambiguationQueue))).scalars().one()
    assert q.status == "pending"
    detail = q.score_detail
    assert set(detail) == {"name", "org", "research", "time", "network", "total"}
    assert detail["name"] == 1.0 and detail["org"] == 0.4
    assert 0.5 <= float(q.score) < 0.8


async def test_openalex_strong_match_links(db_session) -> None:
    """openalex_id 命中已有 Person → 直归并（不打分）。"""
    person = await _mk_person_with_history(
        db_session, name="Old Name", org=None, papers_meta=[], openalex_id="A999",
    )
    paper = await _mk_paper(db_session, arxiv_id="2608.303")
    pa = PaperAuthor(paper_id=paper.id, author_seq=0, raw_name="New Name",
                     openalex_id="A999")
    db_session.add(pa)
    await db_session.flush()

    result = await process_author(db_session, pa, paper, set())
    assert result == "linked_existing"
    await db_session.refresh(pa)
    assert pa.person_id == person.id


async def test_rejected_pair_not_requeued(db_session) -> None:
    """已 reject 的组合不再重复入队（uq 兜底）；新相似作者仍可入队。"""
    a = Person(name="A", name_normalized="a")
    b = Person(name="B", name_normalized="b")
    db_session.add_all([a, b])
    await db_session.flush()
    db_session.add(DisambiguationQueue(
        person_a_id=min(a.id, b.id), person_b_id=max(a.id, b.id),
        score=0.65, score_detail={}, status="rejected",
        resolved_at=D(2026, 8, 1, tzinfo=dt.timezone.utc),
    ))
    await db_session.flush()

    from app.services.disambiguator import enqueue_pair
    from app.services.disambiguator import ScoreDetail
    detail = ScoreDetail(name=1.0, org=0.4, research=0.5, time=0.5, network=0.2)
    await enqueue_pair(db_session, a.id, b.id, detail)  # 应被忽略
    await db_session.commit()

    rows = (await db_session.execute(select(DisambiguationQueue))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "rejected"  # 没有新的 pending


async def test_run_disambiguation_stats(db_session) -> None:
    """批量入口：未关联作者全部处理，新 Person 建立并同步 org。"""
    paper = await _mk_paper(db_session, arxiv_id="2608.304", tags=["llm agent"],
                            date=D(2026, 8, 1))
    db_session.add_all([
        PaperAuthor(paper_id=paper.id, author_seq=0, raw_name="Wei Zhang",
                    affiliation="Peking University"),
        PaperAuthor(paper_id=paper.id, author_seq=1, raw_name="Li Wang"),
    ])
    await db_session.flush()

    stats = await run_disambiguation(db_session)

    assert stats == {"linked_existing": 0, "created": 2, "queued": 0, "failed": 0}
    persons = (await db_session.execute(select(Person))).scalars().all()
    assert len(persons) == 2
    # 有机构的作者经 sync_person_org 挂上 org
    orgs = (await db_session.execute(select(Organization))).scalars().all()
    assert [o.name for o in orgs] == ["Peking University"]


async def test_overlong_affiliation_skips_org_no_crash(db_session) -> None:
    """回归（2026-08-31 生产事故）：多机构拼接串超 organizations.name
    VARCHAR(300)——08-25 起夜批消歧连崩 5 晚的根因。超长机构不挂，
    Person 照建，不抛异常。
    """
    poison = "Inst A, Univ B, City C, Country D; " * 20  # 560 字符
    assert len(poison) > 300
    paper = await _mk_paper(db_session, arxiv_id="2608.305")
    db_session.add(PaperAuthor(paper_id=paper.id, author_seq=0,
                               raw_name="Poison Au", affiliation=poison))
    await db_session.flush()

    stats = await run_disambiguation(db_session)  # 修复前：StringDataRightTruncation

    assert stats["created"] == 1 and stats["failed"] == 0
    person = (await db_session.execute(select(Person))).scalars().one()
    assert person.name == "Poison Au"
    org_links = (await db_session.execute(select(PersonOrg))).scalars().all()
    assert org_links == []  # 超长机构跳过，不留垃圾实体


async def test_poison_paper_isolated_not_whole_batch(db_session) -> None:
    """单篇失败只回滚该篇：其余论文照常消歧，失败篇进 failed_jobs 可重试。"""
    from app.models import FailedJob

    good = await _mk_paper(db_session, arxiv_id="2608.306")
    bad = await _mk_paper(db_session, arxiv_id="2608.307")
    db_session.add_all([
        PaperAuthor(paper_id=bad.id, author_seq=0, raw_name="Bad Au"),
        PaperAuthor(paper_id=good.id, author_seq=0, raw_name="Good Au"),
    ])
    await db_session.flush()

    import app.services.disambiguator as dis_mod
    real = dis_mod.process_author
    calls = {"n": 0}

    async def flaky(session, pa, paper, norms):
        calls["n"] += 1
        if pa.raw_name == "Bad Au":
            raise RuntimeError("毒论文模拟失败")
        return await real(session, pa, paper, norms)

    dis_mod.process_author = flaky
    try:
        stats = await run_disambiguation(db_session)
    finally:
        dis_mod.process_author = real

    assert stats["created"] == 1 and stats["failed"] == 1
    persons = (await db_session.execute(select(Person))).scalars().all()
    assert [p.name for p in persons] == ["Good Au"]  # 毒篇整篇回滚
    jobs = (await db_session.execute(select(FailedJob))).scalars().all()
    assert len(jobs) == 1 and jobs[0].job_type == "disambiguate"
    assert jobs[0].target == "2608.307" and jobs[0].status == "retrying"
