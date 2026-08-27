"""M2-T4：中文拼音归一 + 消歧强归并单测（RD-M2-12，FR-4.1/4.2）。"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.models import DisambiguationQueue, Paper, PaperAuthor, Person, PersonOrg
from app.services.disambiguator import process_author, strong_merge_match
from app.services.openalex import upsert_organization
from app.utils.names import normalize_name, normalize_person_name

D = dt.datetime


# ---------- 归一纯函数 ----------


def test_pinyin_norm_same_domain_as_english() -> None:
    assert normalize_person_name("张三") == "zhangsan"
    assert normalize_person_name("张三") == normalize_name("Zhang San")
    assert normalize_person_name("王小明") == "wangxiaoming"
    # 英文行为不变
    assert normalize_person_name("Wei Zhang") == "weizhang"
    assert normalize_person_name("") == ""


def test_polyphonic_surname_takes_default_sound() -> None:
    """多音字（"单"姓）不崩溃，取词典默认音。"""
    norm = normalize_person_name("单伟")
    assert norm and norm.isalpha()
    assert norm == normalize_person_name("单伟")  # 确定性


# ---------- 强归并 ----------


async def _seed_zhang_san(db_session) -> Person:
    """既有英文 Person("Zhang San") + 清华大学。"""
    person = Person(name="Zhang San", name_normalized=normalize_name("Zhang San"))
    db_session.add(person)
    await db_session.flush()
    org = await upsert_organization(db_session, "清华大学")
    db_session.add(PersonOrg(person_id=person.id, org_id=org.id, org_confidence=1.0, source="glm"))
    await db_session.commit()
    return person


async def _mk_paper(db_session, arxiv_id: str) -> Paper:
    paper = Paper(
        arxiv_id=arxiv_id, title="T", abstract="A",
        authors_raw=[], categories=["cs.AI"], status="extracted",
        research_tags=[], published_at=D(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    db_session.add(paper)
    await db_session.flush()
    return paper


async def test_strong_merge_chinese_name_same_org(db_session) -> None:
    """网页"张三"+清华大学 vs Person("Zhang San", 清华) → 强归并同 id。"""
    person = await _seed_zhang_san(db_session)

    hit = await strong_merge_match(db_session, "张三", "清华大学")
    assert hit is not None and hit.id == person.id

    paper = await _mk_paper(db_session, "2609.001")
    pa = PaperAuthor(paper_id=paper.id, author_seq=0, raw_name="张三", affiliation="清华大学")
    db_session.add(pa)
    await db_session.flush()
    result = await process_author(db_session, pa, paper, {"zhangsan"})
    await db_session.commit()

    assert result == "linked_existing"
    persons = (await db_session.execute(select(Person))).scalars().all()
    assert len(persons) == 1  # 未新建
    assert pa.person_id == person.id
    # 不进队列
    assert (await db_session.execute(select(DisambiguationQueue))).scalars().all() == []


async def test_strong_merge_swapped_english_name(db_session) -> None:
    """"San Zhang" 颠倒序 + 同机构 → 强归并。"""
    person = await _seed_zhang_san(db_session)
    hit = await strong_merge_match(db_session, "San Zhang", "清华大学")
    assert hit is not None and hit.id == person.id


async def test_same_name_different_org_goes_scoring(db_session) -> None:
    """同名不同机构：不强归并，走打分路径（新建 + 入队）。"""
    person = await _seed_zhang_san(db_session)

    assert await strong_merge_match(db_session, "张三", "北京大学") is None

    paper = await _mk_paper(db_session, "2609.002")
    pa = PaperAuthor(paper_id=paper.id, author_seq=0, raw_name="张三", affiliation="北京大学")
    db_session.add(pa)
    await db_session.flush()
    result = await process_author(db_session, pa, paper, {"zhangsan"})
    await db_session.commit()

    persons = (await db_session.execute(select(Person))).scalars().all()
    assert len(persons) == 2  # 新建了中文 Person
    new_one = next(p for p in persons if p.id != person.id)
    assert new_one.name == "张三"
    assert new_one.name_normalized == "zhangsan"  # 拼音归一，与英文同域
    assert result in ("queued", "created")
    queue = (await db_session.execute(select(DisambiguationQueue))).scalars().all()
    if result == "queued":
        assert len(queue) == 1  # 新建 vs 既有 Zhang San 入队


async def test_new_chinese_person_pinyin_norm_matches_english_lookup(db_session) -> None:
    """新建中文 Person 后，英文写法能按拼音归一找到它（双向相遇）。"""
    paper = await _mk_paper(db_session, "2609.003")
    pa = PaperAuthor(paper_id=paper.id, author_seq=0, raw_name="李雷", affiliation="某研究所")
    db_session.add(pa)
    await db_session.flush()
    await process_author(db_session, pa, paper, {"lilei"})
    await db_session.commit()

    created = (await db_session.execute(select(Person))).scalars().one()
    assert created.name_normalized == "lilei"
    hit = await strong_merge_match(db_session, "Li Lei", "某研究所")
    assert hit is not None and hit.id == created.id
