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
    process_author,
    run_disambiguation,
    score_name,
    score_network,
    score_org,
    score_research,
    score_time,
)
from app.services.openalex import upsert_organization
from app.utils.names import normalize_name

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
    # 缩写变体：2 字符差 / 8 字符长 → 比值 0.75，仍落在 0.2 档（阈值 ≥0.85 才 0.7）
    assert score_name("W. Zhang", "Wei Zhang") == 0.2
    assert score_name("Aaa Bbbccc", "Zzz") == 0.2


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

    from app.services.disambiguator import _enqueue
    from app.services.disambiguator import ScoreDetail
    detail = ScoreDetail(name=1.0, org=0.4, research=0.5, time=0.5, network=0.2)
    await _enqueue(db_session, a.id, b.id, detail)  # 应被忽略
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

    assert stats == {"linked_existing": 0, "created": 2, "queued": 0}
    persons = (await db_session.execute(select(Person))).scalars().all()
    assert len(persons) == 2
    # 有机构的作者经 sync_person_org 挂上 org
    orgs = (await db_session.execute(select(Organization))).scalars().all()
    assert [o.name for o in orgs] == ["Peking University"]
