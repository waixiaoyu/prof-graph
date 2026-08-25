"""M1 范围约束单测（2026-08-31）：中国学者判定 + linker 范围过滤。"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.models import Paper, PaperAuthor, Person, Relationship
from app.services.cn_scope import (
    classify_paper,
    flag_papers,
    is_cn_affiliation,
    is_cn_name,
)
from app.services.linker import run_linker
from app.utils.names import normalize_name


# ---------- 启发式判定 ----------

def test_is_cn_affiliation() -> None:
    assert is_cn_affiliation("Tsinghua University")
    assert is_cn_affiliation("Shanghai Jiao Tong University")
    assert is_cn_affiliation("中国科学院自动化研究所")  # CJK 直判
    assert is_cn_affiliation("Shanghai AI Laboratory")
    assert not is_cn_affiliation("MIT CSAIL")
    assert not is_cn_affiliation("University of Toronto")
    assert not is_cn_affiliation(None)
    assert not is_cn_affiliation("Imperial College London")


def test_is_cn_name() -> None:
    assert is_cn_name("Wei Zhang")           # 姓在后
    assert is_cn_name("Zhang, Wei")          # 姓在前（逗号序）
    assert is_cn_name("ZHANG Wei")
    assert not is_cn_name("John Smith")
    assert not is_cn_name("Satoshi Nakamoto")
    assert not is_cn_name("Jisoo Kim")       # 韩式罗马化
    assert not is_cn_name(None)


def test_classify_paper_mixed_authors() -> None:
    """任一作者命中即整篇范围内；两信号独立有效。"""
    by_affil = PaperAuthor(paper_id=1, author_seq=0, raw_name="John Smith",
                           affiliation="Zhejiang University")
    by_name = PaperAuthor(paper_id=1, author_seq=1, raw_name="Wei Zhang",
                          affiliation="MIT")  # 中国学者在外国机构
    foreign = PaperAuthor(paper_id=1, author_seq=2, raw_name="Anna Müller",
                          affiliation="ETH Zürich")
    assert classify_paper([foreign, by_affil])
    assert classify_paper([foreign, by_name])
    assert not classify_paper([foreign])


# ---------- flag_papers 幂等回填 ----------

async def _mk_paper(db_session, arxiv_id: str, affil: str, *, cn: bool) -> tuple[Paper, Person]:
    person = Person(name=f"P {arxiv_id}", name_normalized=normalize_name(f"P {arxiv_id}"))
    db_session.add(person)
    await db_session.flush()
    paper = Paper(
        arxiv_id=arxiv_id, title=f"T-{arxiv_id}", abstract="",
        authors_raw=[], categories=["cs.AI"], status="extracted",
        has_cn_scholar=cn,
        published_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    db_session.add(paper)
    await db_session.flush()
    db_session.add(PaperAuthor(
        paper_id=paper.id, author_seq=0, raw_name=person.name,
        person_id=person.id, affiliation=affil,
    ))
    await db_session.commit()
    return paper, person


async def test_flag_papers_backfills_and_linker_scopes(db_session) -> None:
    """未标记的论文按启发式补标；linker 只为范围内论文建关系。"""
    # 落库时未标记（False），但作者机构是中国 → flag_papers 应翻成 True
    cn_paper, cn_author = await _mk_paper(db_session, "2609.00001", "Peking University", cn=False)
    foreign_paper, _ = await _mk_paper(db_session, "2609.00002", "MIT", cn=False)

    stats = await flag_papers(db_session)
    assert stats["flagged"] == 1  # 只有 2609.00001 翻正

    report = await run_linker(db_session)
    assert report["papers"] == 1  # 只处理范围论文
    rels = (await db_session.execute(select(Relationship))).scalars().all()
    assert rels == []  # 单作者论文不产生两两关系，但 foreign_paper 不应被处理

    # 再给范围论文加一位合作者 → 建出 1 条关系
    partner = Person(name="Li Wang", name_normalized="liwang")
    db_session.add(partner)
    await db_session.flush()
    db_session.add(PaperAuthor(paper_id=cn_paper.id, author_seq=1,
                               raw_name="Li Wang", person_id=partner.id))
    await db_session.commit()
    report = await run_linker(db_session)
    assert report["papers"] == 1 and report["relationships_created"] == 1
