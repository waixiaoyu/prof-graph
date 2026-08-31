"""M3 单测：common_network 计算（FR-2.1 / FR-3 排除分支 / RD-10 门槛）。"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from app.models import Organization, Person, PersonOrg, Relationship
from app.services.potential import compute_common_network, load_network


async def _person(db_session, name: str, *, deleted=False, merged_into=None) -> Person:
    p = Person(
        name=name,
        name_normalized=name.lower(),
        deleted_at=dt.datetime.now(dt.timezone.utc) if deleted else None,
        merged_into_id=merged_into,
    )
    db_session.add(p)
    await db_session.flush()
    return p


async def _rel(
    db_session, a: int, b: int, *, type_: str = "paper_cooperation"
) -> Relationship:
    lo, hi = min(a, b), max(a, b)
    rel = Relationship(
        person_a_id=lo,
        person_b_id=hi,
        type=type_,
        identity_confidence=0.9,
        strength=0.8,
    )
    db_session.add(rel)
    await db_session.flush()
    return rel


async def _rows(db_session):
    net = await load_network(db_session)
    return {(r.a, r.b): r for r in compute_common_network(net)}


async def test_common_network_two_collaborators(db_session) -> None:
    """A、B 与 C、D 都合作过且 A-B 无直接关系 → 产出，signals/reason 可解释。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    c = await _person(db_session, "Carol")
    d = await _person(db_session, "Dave")
    for x in (c, d):
        await _rel(db_session, a.id, x.id)
        await _rel(db_session, b.id, x.id)

    row = (await _rows(db_session))[(a.id, b.id)]
    assert row.method == "common_network"
    assert row.signals["count"] == 2
    assert sorted(row.signals["common_collaborators"]) == sorted([c.id, d.id])
    assert "Carol" in row.reason and "Dave" in row.reason
    # 无共同机构：0.5×(2/5) + 0.3×(1/3) + 0.2×0.5 = 0.40
    assert row.confidence == 0.4

    # 同机构加成：org_sim 0.5→1.0，置信度 +0.10
    org = Organization(name="NISL", name_normalized="nisl")
    db_session.add(org)
    await db_session.flush()
    for pid in (a.id, b.id):
        db_session.add(PersonOrg(person_id=pid, org_id=org.id, source="webpage"))
    await db_session.flush()
    assert (await _rows(db_session))[(a.id, b.id)].confidence == 0.5


async def test_common_network_single_collaborator_excluded(db_session) -> None:
    """仅 1 个共同合作者，低于 RD-10 门槛 2 → 不产出。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    c = await _person(db_session, "Carol")
    await _rel(db_session, a.id, c.id)
    await _rel(db_session, b.id, c.id)

    assert await _rows(db_session) == {}


async def test_common_network_existing_direct_excluded(db_session) -> None:
    """A-B 已有活跃直接关系（FR-3.1）→ 不产出（c-d 也连直接边，避免镜像对干扰断言）。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    c = await _person(db_session, "Carol")
    d = await _person(db_session, "Dave")
    for x in (c, d):
        await _rel(db_session, a.id, x.id)
        await _rel(db_session, b.id, x.id)
    await _rel(db_session, a.id, b.id, type_="academic_mentorship")
    await _rel(db_session, c.id, d.id)

    assert await _rows(db_session) == {}


async def test_common_network_deleted_person_excluded(db_session) -> None:
    """FR-3.2：已删除人不参与——Eve 若计入则共同者凑满 2 人会产出，断言必须为空。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    c = await _person(db_session, "Carol")
    e = await _person(db_session, "Eve", deleted=True)
    for x in (c, e):
        await _rel(db_session, a.id, x.id)
        await _rel(db_session, b.id, x.id)

    assert await _rows(db_session) == {}


async def test_common_network_merged_person_excluded(db_session) -> None:
    """FR-3.2：合并墓碑人不参与（与 deleted 同口径）。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    c = await _person(db_session, "Carol")
    m = await _person(db_session, "Merged", merged_into=a.id)
    for x in (c, m):
        await _rel(db_session, a.id, x.id)
        await _rel(db_session, b.id, x.id)

    assert await _rows(db_session) == {}


async def test_common_network_tombstoned_relationship_excluded(db_session) -> None:
    """FR-3.2：墓碑关系不构成邻接——a-d 关系删除后共同者只剩 c → 不再产出。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    c = await _person(db_session, "Carol")
    d = await _person(db_session, "Dave")
    for x in (c, d):
        await _rel(db_session, a.id, x.id)
        await _rel(db_session, b.id, x.id)
    assert (a.id, b.id) in await _rows(db_session)

    rel_ad = (
        await db_session.execute(
            select(Relationship).where(
                Relationship.person_a_id == min(a.id, d.id),
                Relationship.person_b_id == max(a.id, d.id),
            )
        )
    ).scalar_one()
    rel_ad.deleted_at = dt.datetime.now(dt.timezone.utc)
    assert await _rows(db_session) == {}


async def test_common_network_confidence_clamped(db_session) -> None:
    """6 个共同合作者 + 同机构 → 公式值 0.80，clamp 到 0.70 上限（FR-1.1）。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    helpers = [await _person(db_session, f"H{i}") for i in range(6)]
    for h in helpers:
        await _rel(db_session, a.id, h.id)
        await _rel(db_session, b.id, h.id)
    org = Organization(name="THU", name_normalized="thu")
    db_session.add(org)
    await db_session.flush()
    for pid in (a.id, b.id):
        db_session.add(PersonOrg(person_id=pid, org_id=org.id, source="glm"))
    await db_session.flush()

    assert (await _rows(db_session))[(a.id, b.id)].confidence == 0.7
