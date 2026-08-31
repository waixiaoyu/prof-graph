"""M3 单测：common_network / research_similarity 计算（FR-2）与重算编排（FR-4）。"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    Organization,
    Person,
    PersonOrg,
    PersonResearchTag,
    PotentialRelationship,
    Relationship,
)
from app.services import potential as pot
from app.services.potential import (
    compute_common_network,
    compute_research_similarity,
    load_network,
    recompute_potential,
)


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


async def _tags(db_session, pid: int, tags: list[str]) -> None:
    for t in tags:
        db_session.add(PersonResearchTag(person_id=pid, tag=t))
    await db_session.flush()


async def _rs_rows(db_session):
    net = await load_network(db_session)
    return {(r.a, r.b): r for r in compute_research_similarity(net)}


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


# ---------- research_similarity（FR-2.2 / RD-11） ----------


async def test_rs_basic_output(db_session) -> None:
    """Jaccard 0.5 的唯一合格对 → 产出，signals/reason/置信度齐备。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    await _tags(db_session, a.id, ["ml", "ai", "cv"])
    await _tags(db_session, b.id, ["ml", "ai", "nlp"])

    row = (await _rs_rows(db_session))[(a.id, b.id)]
    assert row.method == "research_similarity"
    assert row.signals["overlap_tags"] == ["ai", "ml"]
    assert row.signals["jaccard"] == 0.5
    assert row.signals["tags_a"] == 3 and row.signals["tags_b"] == 3
    assert row.reason == "研究方向相似（0.50）：ai、ml"
    # 0.7×0.5 + 0.3×(2/5) = 0.47
    assert row.confidence == 0.47


async def test_rs_below_threshold_excluded(db_session) -> None:
    """重叠 1 / 并集 7 → Jaccard≈0.14 < 0.3 → 不产出。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    await _tags(db_session, a.id, ["a", "b", "c", "d"])
    await _tags(db_session, b.id, ["a", "e", "f", "g"])

    assert await _rs_rows(db_session) == {}


async def test_rs_not_mutually_selected_excluded(db_session) -> None:
    """7 人标签全同：第 7 人不入任何人的 top-5 → 与其全部 6 对被剔，余 15 对保留。"""
    group = [await _person(db_session, f"P{i}") for i in range(7)]
    for p in group:
        await _tags(db_session, p.id, ["ml"])

    rows = await _rs_rows(db_session)
    p6 = group[6]  # id 最大：无人互认
    assert all(p6.id not in pair for pair in rows)
    assert (group[0].id, group[1].id) in rows
    assert len(rows) == 15  # C(6,2)
    # Jaccard=1.0 + 重叠 1：0.7 + 0.3×(1/5) = 0.76 → clamp 0.70
    assert rows[(group[0].id, group[1].id)].confidence == 0.7


async def test_rs_no_tags_excluded(db_session) -> None:
    """无标签的人不参与 RS（无共享标签即无候选对）。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    await _tags(db_session, b.id, ["ml"])

    assert await _rs_rows(db_session) == {}


async def test_rs_case_insensitive_and_clamped(db_session) -> None:
    """标签大小写归一后同一标签；Jaccard=1.0 clamp 到 0.70。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    await _tags(db_session, a.id, ["Machine Learning"])
    await _tags(db_session, b.id, ["machine learning"])

    row = (await _rs_rows(db_session))[(a.id, b.id)]
    assert row.signals["overlap_tags"] == ["machine learning"]
    assert row.confidence == 0.7


async def test_rs_existing_direct_excluded(db_session) -> None:
    """已有活跃直接关系（FR-3.1/RD-3 一律排除）→ 不产出。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    await _tags(db_session, a.id, ["ml", "ai"])
    await _tags(db_session, b.id, ["ml", "ai"])
    await _rel(db_session, a.id, b.id)

    assert await _rs_rows(db_session) == {}


async def test_rs_tombstoned_person_excluded(db_session) -> None:
    """FR-3.2：已删除人的标签不参与——若计入则 Jaccard=1.0 会产出，断言为空。"""
    a = await _person(db_session, "Alice")
    e = await _person(db_session, "Eve", deleted=True)
    await _tags(db_session, a.id, ["ml", "ai"])
    await _tags(db_session, e.id, ["ml", "ai"])

    assert await _rs_rows(db_session) == {}


# ---------- 重算编排 recompute_potential（FR-4.3/4.4） ----------


async def _square_graph(db_session):
    """四人方图：a-c/a-d/b-c/b-d 四条直接边 → common (a,b)+(c,d)；
    c、d 加同款标签 → RS (c,d)。"""
    a = await _person(db_session, "Alice")
    b = await _person(db_session, "Bob")
    c = await _person(db_session, "Carol")
    d = await _person(db_session, "Dave")
    for x in (c, d):
        await _rel(db_session, a.id, x.id)
        await _rel(db_session, b.id, x.id)
    await _tags(db_session, c.id, ["ml"])
    await _tags(db_session, d.id, ["ml"])
    return a, b, c, d


def _snapshot(rows) -> list:
    """行集快照（不含 id/时间戳——幂等比对的就是业务列）。"""
    return sorted(
        (
            r.person_a_id,
            r.person_b_id,
            r.discovery_method,
            float(r.confidence),
            r.reason,
        )
        for r in rows
    )


async def _table(db_session):
    return (await db_session.execute(select(PotentialRelationship))).scalars().all()


async def test_recompute_double_run_idempotent(db_session) -> None:
    """FR-4.4：同输入双跑产出完全相同的行集（M1 双跑幂等惯例）。"""
    a, b, c, d = await _square_graph(db_session)

    report1 = await recompute_potential(db_session)
    snap1 = _snapshot(await _table(db_session))
    report2 = await recompute_potential(db_session)
    snap2 = _snapshot(await _table(db_session))

    assert report1["common_network"] == 2  # (a,b) 与镜像对 (c,d)
    assert report1["research_similarity"] == 1  # (c,d)
    assert report2["common_network"] == 2 and report2["research_similarity"] == 1
    assert snap1 == snap2 and len(snap1) == 3
    assert report1["duration_s"] >= 0


async def test_recompute_clears_stale_rows(db_session) -> None:
    """FR-3.3：全量收敛不是只增不减——预置失效行（新直接关系对）随重算清除；
    端点转墓碑后其全部潜在行消失。"""
    a, b, c, d = await _square_graph(db_session)
    await recompute_potential(db_session)
    assert len(await _table(db_session)) == 3

    # 预置脏行：重算不会再产出的旧残留（a-b 并无标签，不该有 RS 行）
    lo, hi = min(a.id, b.id), max(a.id, b.id)
    db_session.add(
        PotentialRelationship(
            person_a_id=lo,
            person_b_id=hi,
            discovery_method="research_similarity",
            confidence=0.5,
            reason="旧版残留",
        )
    )
    await db_session.flush()
    assert len(await _table(db_session)) == 4  # 脏行确实在库

    # c 转墓碑：其参与的全部潜在行失效；commons(a,b) 也只剩 1 人
    c.deleted_at = dt.datetime.now(dt.timezone.utc)
    report = await recompute_potential(db_session)
    assert report["common_network"] == 0 and report["research_similarity"] == 0
    assert await _table(db_session) == []


async def test_recompute_rollback_keeps_old(db_session, monkeypatch) -> None:
    """FR-4.3：事务中途失败整体回滚——DELETE 已执行的旧全量必须原样保留。"""
    a, b, c, d = await _square_graph(db_session)
    await recompute_potential(db_session)
    old_snap = _snapshot(await _table(db_session))
    assert len(old_snap) == 3

    # 计算结果夹带越界 confidence → flush 撞 ck_potential_confidence
    bad = pot.PotentialRow(
        a=a.id, b=b.id, method="common_network",
        confidence=0.9, reason="越界", signals={},
    )
    monkeypatch.setattr(pot, "compute_common_network", lambda net: [bad])

    with pytest.raises(IntegrityError):
        await recompute_potential(db_session)

    assert _snapshot(await _table(db_session)) == old_snap  # 旧全量未丢
