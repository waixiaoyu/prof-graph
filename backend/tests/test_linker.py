"""T12 单测：3 篇只 1 条关系 / coop_count 与 strength 阶梯 / 反向不重复（FR-4.1~4.5）。"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.models import Paper, PaperAuthor, Person, PersonOrg, Relationship, RelationshipEvidence
from app.services.linker import REL_TYPE, link_paper, run_linker, tier
from app.services.openalex import upsert_organization

D = dt.datetime


def test_tier_ladder() -> None:
    assert tier(1) == 0.85
    assert tier(2) == 0.90
    assert tier(3) == 0.95
    assert tier(4) == 0.95
    assert tier(5) == 1.00
    assert tier(9) == 1.00


async def _mk_person(db_session, name: str, org: str | None = None) -> Person:
    p = Person(name=name, name_normalized=name.lower().replace(" ", ""))
    db_session.add(p)
    await db_session.flush()
    if org:
        o = await upsert_organization(db_session, org)
        db_session.add(PersonOrg(person_id=p.id, org_id=o.id, org_confidence=1.0, source="glm"))
    return p


async def _mk_paper_with_authors(db_session, arxiv_id: str, persons: list[Person],
                                 date: dt.datetime | None) -> Paper:
    paper = Paper(
        arxiv_id=arxiv_id, title=f"T-{arxiv_id}", abstract="",
        authors_raw=[p.name for p in persons], categories=["cs.AI"],
        status="extracted", has_cn_scholar=True, published_at=date,
    )
    db_session.add(paper)
    await db_session.flush()
    for i, p in enumerate(persons):
        db_session.add(PaperAuthor(paper_id=paper.id, author_seq=i,
                                   raw_name=p.name, person_id=p.id))
    await db_session.flush()
    return paper


async def test_three_papers_one_relationship(db_session) -> None:
    """同对作者 3 篇论文：1 条关系，coop_count=3，strength=0.95×identity，3 条证据，摘要正确。"""
    a = await _mk_person(db_session, "Wei Zhang", "Peking University")
    b = await _mk_person(db_session, "Li Wang", "Tsinghua University")
    dates = [D(2024, 3, 1), D(2025, 6, 1), D(2026, 1, 15)]
    for i, d in enumerate(dates):
        paper = await _mk_paper_with_authors(db_session, f"2608.4{i}", [a, b], d)
        await link_paper(db_session, paper)
    await db_session.commit()

    rels = (await db_session.execute(select(Relationship))).scalars().all()
    assert len(rels) == 1
    rel = rels[0]
    assert rel.type == REL_TYPE
    assert (rel.person_a_id, rel.person_b_id) == (min(a.id, b.id), max(a.id, b.id))
    assert rel.coop_count == 3
    # 双方 org_confidence=1.0 → identity = 0.4×1.0 + 0.6×1.0 = 1.0
    assert float(rel.identity_confidence) == 1.0
    assert float(rel.strength) == 0.95  # tier(3)
    assert rel.time_start == dates[0].date() and rel.time_end == dates[2].date()
    assert rel.evidence_summary == "基于 3 篇合作论文，最近合作于 2026 年"

    evs = (
        await db_session.execute(select(RelationshipEvidence))
    ).scalars().all()
    assert len(evs) == 3


async def test_reprocessing_same_paper_idempotent(db_session) -> None:
    """幂等回归（2026-08-26 修复）：run_linker 每轮处理全部已抽取论文，
    同一篇论文重复处理不得抬升 coop_count/强度，证据只有一行。"""
    a = await _mk_person(db_session, "Idem One", "Fudan University")
    b = await _mk_person(db_session, "Idem Two", "Zhejiang University")
    paper = await _mk_paper_with_authors(db_session, "2608.60", [a, b], D(2026, 2, 1))
    await link_paper(db_session, paper)
    await db_session.commit()

    # 模拟管线下一轮：同样的论文全部重跑（历史缺陷：计数每轮 +1）
    await link_paper(db_session, paper)
    await run_linker(db_session)

    rel = (await db_session.execute(select(Relationship))).scalars().one()
    evs = (
        await db_session.execute(select(RelationshipEvidence))
    ).scalars().all()
    assert rel.coop_count == 1
    assert rel.evidence_summary == "基于 1 篇合作论文，最近合作于 2026 年"
    assert float(rel.strength) == 0.85  # identity=1.0 × tier(1)，未重复抬升
    assert len(evs) == 1


async def test_reversed_author_order_no_duplicate(db_session) -> None:
    """反向顺序（B,A）不产生第二条关系。"""
    a = await _mk_person(db_session, "Ann Lee")
    b = await _mk_person(db_session, "Bob Chen")
    await _mk_paper_with_authors(db_session, "2608.50", [a, b], D(2025, 1, 1))
    await _mk_paper_with_authors(db_session, "2608.51", [b, a], D(2025, 2, 1))  # 顺序颠倒

    report = await run_linker(db_session)
    assert report["relationships_created"] == 1
    rels = (await db_session.execute(select(Relationship))).scalars().all()
    assert len(rels) == 1 and rels[0].coop_count == 2
    # identity = 0.4×1.0 + 0.6×0.4 = 0.64；strength = 0.64 × tier(2)=0.90 = 0.576 → 0.58
    assert float(rels[0].strength) == 0.58


async def test_no_org_identity_04_floor(db_session) -> None:
    """无机构双方：org_confidence 0.4 兜底 → identity = 0.4+0.24 = 0.64。"""
    a = await _mk_person(db_session, "C D")
    b = await _mk_person(db_session, "E F")
    paper = await _mk_paper_with_authors(db_session, "2608.52", [a, b], D(2025, 5, 1))
    await link_paper(db_session, paper)
    await db_session.commit()

    rel = (await db_session.execute(select(Relationship))).scalars().one()
    assert float(rel.identity_confidence) == 0.64
    assert float(rel.strength) == round(0.64 * 0.85, 2)


async def test_linker_skips_unlinked_authors(db_session) -> None:
    """尚无 person_id 的作者行（未消歧）不参与建关系。"""
    a = await _mk_person(db_session, "G H")
    paper = Paper(
        arxiv_id="2608.53", title="T", abstract="", authors_raw=["G H", "I J"],
        categories=["cs.AI"], status="extracted", published_at=D(2025, 1, 1),
    )
    db_session.add(paper)
    await db_session.flush()
    db_session.add(PaperAuthor(paper_id=paper.id, author_seq=0, raw_name="G H", person_id=a.id))
    db_session.add(PaperAuthor(paper_id=paper.id, author_seq=1, raw_name="I J"))  # 无 person
    await db_session.flush()

    created = await link_paper(db_session, paper)
    assert created == 0
    assert (await db_session.execute(select(Relationship))).scalars().first() is None
