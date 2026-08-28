"""M2-T5：网页抽取单测——schema 校验 / 消歧入库 / 机构三级 / 字段回填 / 幂等（FR-3.2/3.6/3.7）。"""
from __future__ import annotations

import json

from sqlalchemy import select

from app.models import Organization, Person, PersonOrg, WebPage
from app.services.page_extractor import (
    MAX_CHARS,
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


class _RecordingTransport:
    def __init__(self, text: str):
        self.text = text
        self.user: str | None = None

    async def __call__(self, system: str, user: str, max_tokens: int):
        from app.services.glm import TransportResult

        self.user = user
        return TransportResult(self.text, 1500, 1000)


async def test_extract_page_truncates_long_content(db_session) -> None:
    """超长正文截断到 MAX_CHARS（24k chars ≈ 12k tokens）。"""
    page = await _page(db_session, content="字" * (MAX_CHARS + 8000))
    rec = _RecordingTransport(json.dumps({"members": [], "page_context": "unclear"}))
    await extract_page(db_session, GLMClient(transport=rec), page)
    assert rec.user is not None and len(rec.user) == MAX_CHARS


def test_validate_non_dict_member_skipped() -> None:
    """members 混入非对象条目：记 warning 跳过，不炸整批。"""
    ext = validate_page_extraction({"members": ["张三", {"name": "李四"}]})
    assert [m.name for m in ext.members] == ["李四"]
    assert any("非对象" in w for w in ext.warnings)


async def test_same_name_different_org_queues_not_merged(db_session) -> None:
    """同名不同校：页面场景打分上限 0.775 < AUTO_MERGE_THRESHOLD(0.8)——
    锁定该设计边界：不自动并档，走复核队列（人审裁决）。"""
    from app.models import DisambiguationQueue
    from app.utils.names import normalize_person_name

    existing = Person(name="张伟", name_normalized=normalize_person_name("张伟"))
    db_session.add(existing)
    await db_session.flush()
    from app.services.openalex import upsert_organization

    org = await upsert_organization(db_session, "北京大学")
    db_session.add(PersonOrg(person_id=existing.id, org_id=org.id,
                             org_confidence=1.0, source="webpage"))

    payload = json.dumps({
        "lab_name": "智能实验室", "org_school": "清华大学", "org_department": "计算机系",
        "page_context": "official_lab",
        "members": [{"name": "张伟", "role": "professor"}],
    }, ensure_ascii=False)
    page = await _page(db_session)
    ext = await extract_page(db_session, GLMClient(transport=FakeTransport(payload)), page)
    await persist_page_result(db_session, page, ext)
    await db_session.commit()

    zhangs = (await db_session.execute(select(Person).where(Person.name == "张伟"))).scalars().all()
    assert len(zhangs) == 2  # 未自动并档
    queue = (await db_session.execute(select(DisambiguationQueue))).scalars().all()
    assert len(queue) == 1  # 与既有同名者入复核队列
    assert queue[0].status == "pending"
    pair = {queue[0].person_a_id, queue[0].person_b_id}
    assert existing.id in pair


async def test_school_lab_same_name_single_anchor(db_session) -> None:
    """lab_name 与 org_school 同名：upsert 出同一机构，成员只挂一条锚。"""
    payload = json.dumps({
        "lab_name": "交叉信息研究院", "org_school": "交叉信息研究院",
        "page_context": "official_lab",
        "members": [{"name": "钱七", "role": "professor"}],
    }, ensure_ascii=False)
    page = await _page(db_session)
    ext = await extract_page(db_session, GLMClient(transport=FakeTransport(payload)), page)
    await persist_page_result(db_session, page, ext)
    await db_session.commit()

    qian = (await db_session.execute(select(Person).where(Person.name == "钱七"))).scalar_one()
    po = (await db_session.execute(
        select(PersonOrg).where(PersonOrg.person_id == qian.id)
    )).scalars().all()
    assert len(po) == 1  # 同名机构去重，无 PK 冲突


async def test_org_confidence_takes_page_context_tier(db_session) -> None:
    """grad_list（0.8）与 official_lab（1.0）的挂靠置信度分档。"""
    for i, (ctx, expected) in enumerate((("grad_list", 0.8), ("official_lab", 1.0)), start=10):
        payload = json.dumps({
            "org_school": "某大学", "page_context": ctx,
            "members": [{"name": f"成员{ctx}", "role": "alumni" if ctx == "grad_list" else "professor"}],
        }, ensure_ascii=False)
        page = await _page(db_session, i=i)
        ext = await extract_page(db_session, GLMClient(transport=FakeTransport(payload)), page)
        await persist_page_result(db_session, page, ext)
        await db_session.commit()

        person = (await db_session.execute(
            select(Person).where(Person.name == f"成员{ctx}")
        )).scalar_one()
        anchors = (await db_session.execute(
            select(PersonOrg).where(PersonOrg.person_id == person.id)
        )).scalars().all()
        assert len(anchors) == 1
        assert float(anchors[0].org_confidence) == expected, ctx
