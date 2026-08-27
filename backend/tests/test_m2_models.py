"""M2-T1：subtype 唯一键改造与新增表约束单测。"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import (
    NewsItem,
    Person,
    Project,
    Relationship,
    WebPage,
)


async def _two_persons(db_session):
    a = Person(name="张三", name_normalized="zhangsan")
    b = Person(name="李四", name_normalized="lisi")
    db_session.add_all([a, b])
    await db_session.flush()
    lo, hi = sorted([a.id, b.id])
    return lo, hi


async def test_duplicate_pair_type_subtype_rejected(db_session):
    lo, hi = await _two_persons(db_session)
    db_session.add(Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                                subtype="same_lab", identity_confidence=0.9, strength=0.8))
    await db_session.flush()
    db_session.add(Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                                subtype="same_lab", identity_confidence=0.9, strength=0.8))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_same_pair_different_subtype_coexist(db_session):
    """RD-M2-2：同一对人可同时存在 mentor_student 与 same_lab（subtype 不同行）。"""
    lo, hi = await _two_persons(db_session)
    db_session.add_all([
        Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                     subtype="mentor_student", identity_confidence=0.9, strength=0.9),
        Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                     subtype="same_lab", identity_confidence=0.9, strength=0.8),
    ])
    await db_session.flush()
    count = (await db_session.execute(select(func.count()).select_from(Relationship))).scalar_one()
    assert count == 2


async def test_legacy_subtype_defaults_empty(db_session):
    """存量 paper_cooperation 语义：不传 subtype 落 ''（零迁移）。"""
    lo, hi = await _two_persons(db_session)
    rel = Relationship(person_a_id=lo, person_b_id=hi, type="paper_cooperation",
                       identity_confidence=0.9, strength=0.85)
    db_session.add(rel)
    await db_session.flush()
    assert rel.subtype == ""


async def test_new_tables_unique_keys(db_session):
    db_session.add(WebPage(url="https://x.edu/p", seed_id="s1", page_type="lab_members"))
    await db_session.flush()
    db_session.add(WebPage(url="https://x.edu/p", seed_id="s1", page_type="lab_members"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    db_session.add(NewsItem(source_id="qbitai", url="https://n.example/1", title="t"))
    await db_session.flush()
    db_session.add(NewsItem(source_id="qbitai", url="https://n.example/1", title="t2"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    db_session.add(Project(name="重点项目", name_normalized="zhongdian"))
    await db_session.flush()
    db_session.add(Project(name="重点项目（变体）", name_normalized="zhongdian"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
