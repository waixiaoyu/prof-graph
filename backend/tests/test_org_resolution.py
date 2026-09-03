"""机构消解测试（M4 第一刀，2026-08-31）。

键规则（零风险等价类）+ 合并执行（PersonOrg 迁移/置信度取大/审计日志/
防复活）+ 防撞键守卫与校区不误并。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AdminEdit, Organization, Person, PersonOrg
from app.services import openalex
from app.services.org_resolution import (
    apply_org_merges,
    org_key,
    plan_org_merges,
)


# ---------- 键规则 ----------

def test_key_punctuation_variants_unite() -> None:
    """分号/逗号/无标点变体同键（复核队列 181 条的形态）。"""
    a = org_key("Independent Researcher; Raleigh, NC, USA")
    b = org_key("Independent Researcher, Raleigh, NC, USA")
    c = org_key("Independent Researcher Raleigh NC USA")
    assert a == b == c


def test_key_fullwidth_parens_unite() -> None:
    """全角/半角括号同键（IPADS/NISL 双行）。"""
    assert org_key("并行与分布式系统研究所（IPADS）") == org_key(
        "并行与分布式系统研究所 (IPADS)"
    )


def test_key_country_suffix_stripped_with_guard() -> None:
    """尾部国家词剥离；防撞键守卫：剩余名称无显著词则不剥。"""
    assert org_key("Google DeepMind, USA") == org_key("Google DeepMind")
    assert org_key("KAIST, Korea") == org_key("KAIST")
    # 防撞键守卫：剩余只剩 bank of，再剥就与 Bank of England 撞成同一个键 → 不剥
    assert org_key("Bank of China") != org_key("Bank of England")


def test_key_campus_tokens_never_stripped() -> None:
    """城市/校区词是机构身份：哈工大≠哈工大深圳，CUHK≠CUHK深圳。"""
    assert org_key("Harbin Institute of Technology") != org_key(
        "Harbin Institute of Technology, Shenzhen, China"
    )
    assert org_key("The Chinese University of Hong Kong") != org_key(
        "The Chinese University of Hong Kong, Shenzhen"
    )
    # 省份/国家尾缀是信封信息：同校区变体（都带 Shenzhen）同键
    assert org_key("Harbin Institute of Technology, Shenzhen, China") == org_key(
        "Harbin Institute of Technology, Shenzhen, Guangdong, China"
    )


def test_normalize_org_delegates() -> None:
    """写入侧与消解同键（防复活）。"""
    assert openalex.normalize_org("Kuaishou Technology, China") == org_key(
        "Kuaishou Technology"
    )


# ---------- 计划与执行 ----------

async def test_plan_and_apply_merges(db_session) -> None:
    org_a = Organization(name="Kuaishou Technology",
                         name_normalized=org_key("Kuaishou Technology"))
    org_b = Organization(name="Kuaishou Technology; China",
                         name_normalized="kuaishou technology; china")
    org_c = Organization(name="Alibaba Group",
                         name_normalized=org_key("Alibaba Group"))
    db_session.add_all([org_a, org_b, org_c])
    p1, p2 = Person(name="A", name_normalized="a"), Person(name="B", name_normalized="b")
    db_session.add_all([p1, p2])
    await db_session.flush()
    db_session.add_all([
        PersonOrg(person_id=p1.id, org_id=org_a.id, org_confidence=0.9,
                  source="openalex"),
        PersonOrg(person_id=p2.id, org_id=org_b.id, org_confidence=0.8,
                  source="openalex"),
    ])
    await db_session.flush()

    plans = await plan_org_merges(db_session)
    assert len(plans) == 1
    assert plans[0]["keep"]["id"] == org_a.id  # 引用多者为代表
    assert [d["id"] for d in plans[0]["drops"]] == [org_b.id]

    stats = await apply_org_merges(db_session, plans, reason="M4 机构消解")
    assert stats["merged_rows"] == 1 and stats["clusters"] == 1

    # 被并行删除；署名迁移到代表；键重写；审计日志成对
    assert await db_session.get(Organization, org_b.id) is None
    po = (await db_session.execute(
        select(PersonOrg).where(PersonOrg.person_id == p2.id)
    )).scalar_one()
    assert po.org_id == org_a.id
    logs = (await db_session.execute(
        select(AdminEdit).where(AdminEdit.action == "merge_orgs")
    )).scalars().all()
    assert len(logs) == 1 and logs[0].entity_type == "organization"
    assert logs[0].before["drops"][0]["id"] == org_b.id


async def test_apply_keeps_max_confidence(db_session) -> None:
    keep = Organization(name="National University of Singapore",
                        name_normalized=org_key("National University of Singapore"))
    drop = Organization(name="National University of Singapore, Singapore, Singapore",
                        name_normalized="national of singapore singapore")
    db_session.add_all([keep, drop])
    p = Person(name="A", name_normalized="a")
    db_session.add(p)
    await db_session.flush()
    db_session.add_all([
        PersonOrg(person_id=p.id, org_id=keep.id, org_confidence=0.5,
                  source="openalex"),
        PersonOrg(person_id=p.id, org_id=drop.id, org_confidence=0.95,
                  source="openalex"),
    ])
    await db_session.flush()
    plans = await plan_org_merges(db_session)
    await apply_org_merges(db_session, plans, reason="测试")
    po = (await db_session.execute(select(PersonOrg))).scalar_one()
    assert po.org_id == keep.id and float(po.org_confidence) == 0.95


async def test_upsert_no_resurrection_after_merge(db_session) -> None:
    """合并后再 upsert 变体串：命中代表行，不另起新行。"""
    keep = Organization(name="Kuaishou Technology",
                        name_normalized=org_key("Kuaishou Technology"))
    db_session.add(keep)
    await db_session.flush()
    org = await openalex.upsert_organization(db_session, "Kuaishou Technology, China")
    assert org.id == keep.id
    count = len((await db_session.execute(select(Organization))).scalars().all())
    assert count == 1


def test_key_never_empty() -> None:
    assert org_key("   ") != ""
    assert org_key("...")
