"""M2-T5：网页抽取单测——schema 校验 / 消歧入库 / 机构三级 / 字段回填 / 幂等（FR-3.2/3.6/3.7）。"""
from __future__ import annotations

import json

from sqlalchemy import select

from app.models import Organization, Person, PersonOrg, WebPage
from app.services.page_extractor import (
    extract_page,
    persist_page_result,
    validate_page_extraction,
)
from app.services.glm import GLMClient
from tests.test_extractor import FakeTransport

PAGE_JSON = json.dumps(
    {
        "lab_name": "智能实验室",
        "org_school": "清华大学",
        "org_department": "计算机科学与技术系",
        "members": [
            {
                "name": "张伟", "role": "professor", "advisor": None, "grad_year": None,
                "title": "教授", "homepage": "https://zhangwei.example.cn",
                "email": "zw@tsinghua.example.cn",
            },
            {"name": "李雷", "role": "phd", "advisor": "张伟", "grad_year": None},
            {"name": "", "role": "master"},  # 缺 name → 跳过
            {"name": "韩梅", "role": "master", "grad_year": 2024, "title": "  "},  # 空白字段清洗
        ],
        "page_context": "official_lab",
    },
    ensure_ascii=False,
)


async def _page(db_session, i=1, content="成员页正文") -> WebPage:
    w = WebPage(
        url=f"https://x.edu/p{i}", seed_id="seed-1", page_type="lab_members",
        content_text=content, content_hash=f"h{i}", status="pending_extraction",
    )
    db_session.add(w)
    await db_session.flush()
    return w


# ---------- 校验纯函数 ----------


def test_validate_full_schema() -> None:
    ext = validate_page_extraction(json.loads(PAGE_JSON))
    assert ext.lab_name == "智能实验室"
    assert ext.org_school == "清华大学" and ext.org_department == "计算机科学与技术系"
    assert ext.page_context == "official_lab"
    assert ext.src_confidence == 1.0
    assert [m.name for m in ext.members] == ["张伟", "李雷", "韩梅"]  # 缺 name 跳过
    zhang = ext.members[0]
    assert zhang.title == "教授" and zhang.homepage and zhang.email
    assert ext.members[1].advisor == "张伟"
    assert ext.members[2].grad_year == 2024 and ext.members[2].title is None
    assert len(ext.warnings) == 1


def test_validate_defaults_and_bad_context() -> None:
    ext = validate_page_extraction(
        {"members": [{"name": "X", "role": "boss", "grad_year": True}]}
    )
    assert ext.members[0].role == "unknown"  # 非法 role
    assert ext.members[0].grad_year is None  # 非法年份
    assert ext.page_context == "unclear" and ext.src_confidence == 0.6


def test_validate_broken_schema_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="members"):
        validate_page_extraction({"no_members": 1})


# ---------- GLM 抽取 ----------


async def test_extract_page_no_signal(db_session) -> None:
    glm = GLMClient(transport=FakeTransport(json.dumps({"members": [], "page_context": "unclear"})))
    page = await _page(db_session)
    ext = await extract_page(db_session, glm, page)
    assert ext.members == []


# ---------- 落库 ----------


async def test_persist_persons_orgs_and_fields(db_session) -> None:
    glm = GLMClient(transport=FakeTransport(PAGE_JSON))
    page = await _page(db_session)
    ext = await extract_page(db_session, glm, page)
    await persist_page_result(db_session, page, ext)
    await db_session.commit()

    persons = (await db_session.execute(select(Person))).scalars().all()
    assert {p.name for p in persons} == {"张伟", "李雷", "韩梅"}
    zhang = next(p for p in persons if p.name == "张伟")
    assert zhang.title == "教授" and zhang.homepage == "https://zhangwei.example.cn"
    assert zhang.email == "zw@tsinghua.example.cn"
    assert all(m.identity == 0.9 for m in ext.members)  # 全新中文名：新建基准

    orgs = {
        o.name: o.level
        for o in (await db_session.execute(select(Organization))).scalars().all()
    }
    assert orgs == {
        "清华大学": "university",
        "计算机科学与技术系": "department",
        "智能实验室": "lab",
    }

    po_rows = (
        await db_session.execute(select(PersonOrg).where(PersonOrg.person_id == zhang.id))
    ).scalars().all()
    assert len(po_rows) == 3  # 三级全挂靠
    assert all(r.source == "webpage" and float(r.org_confidence) == 1.0 for r in po_rows)


async def test_persist_idempotent_on_reextraction(db_session) -> None:
    """页面重抽（内容变化场景）：同人强归并、person_org 不重复。"""
    glm = GLMClient(transport=FakeTransport(PAGE_JSON))
    page = await _page(db_session)
    ext = await extract_page(db_session, glm, page)
    await persist_page_result(db_session, page, ext)
    await db_session.commit()

    ext2 = await extract_page(db_session, glm, page)
    await persist_page_result(db_session, page, ext2)
    await db_session.commit()

    persons = (await db_session.execute(select(Person))).scalars().all()
    assert len(persons) == 3  # 强归并（拼音命中 + 同实验室实体）未新建
    assert all(m.identity == 1.0 for m in ext2.members)  # 第二轮全部强归并
    total_po = len((await db_session.execute(select(PersonOrg))).scalars().all())
    assert total_po == 9  # 3 人 × 3 级，无重复


async def test_field_backfill_only_when_null(db_session) -> None:
    """FR-3.6：仅填空不覆盖——重抽带来不同 title 时保留既有值。"""
    glm = GLMClient(transport=FakeTransport(PAGE_JSON))
    page = await _page(db_session)
    ext = await extract_page(db_session, glm, page)
    await persist_page_result(db_session, page, ext)
    await db_session.commit()

    changed = json.loads(PAGE_JSON)
    changed["members"][0]["title"] = "讲席教授"  # 与已入库不同
    glm2 = GLMClient(transport=FakeTransport(json.dumps(changed, ensure_ascii=False)))
    ext2 = await extract_page(db_session, glm2, page)
    await persist_page_result(db_session, page, ext2)
    await db_session.commit()

    zhang = (
        await db_session.execute(select(Person).where(Person.name == "张伟"))
    ).scalar_one()
    assert zhang.title == "教授"  # 既有值不被覆盖
