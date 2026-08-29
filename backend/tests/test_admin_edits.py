"""M2.5 手动编辑：服务层 + API 合一（FR-1~FR-6 / NFR-1~4 / AC-1~5）。

覆盖矩阵（plan §6）：
- 每操作 before/after 落库正确 + admin_edits 成对行（NFR-4）
- 改名重算 name_normalized（含中文"张三"）
- orgs 传不存在 id → 404；墓碑 person（合并/删除）拒改 409
- 删关系后 linker 重跑不复活（FR-4.2）；重复删除 409（FR-4.4）
- strength 越界 422（FR-4.3）
- 按人删除级联 + 全可见面消失 + C1-C10 全绿（AC-4）
- 墓碑库上巡检全绿（FR-4 墓碑 + FR-5 级联混合样本，NFR-2）
"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy import select

from app.db import get_session
from app.main import app
from app.models import (
    AdminEdit,
    DisambiguationQueue,
    Organization,
    Paper,
    PaperAuthor,
    Person,
    PersonOrg,
    PersonResearchTag,
    Relationship,
    RelationshipEvidence,
)
from app.services.integrity import check_integrity
from app.services.linker import link_paper
from app.utils.names import normalize_person_name


@pytest.fixture
async def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _mk_person(session, name: str) -> Person:
    p = Person(name=name, name_normalized=normalize_person_name(name))
    session.add(p)
    return p


async def _seed_edit_graph(db_session) -> dict:
    """3 人 2 边 1 论文：A-B（有证据）、B-C（有证据）；A 挂机构 + 标签 + 待审队列。"""
    pa = _mk_person(db_session, "Wei Zhang")
    pb = _mk_person(db_session, "Li Wang")
    pc = _mk_person(db_session, "Anon Chen")
    org = Organization(name="Peking University", name_normalized="peking")
    org2 = Organization(name="Tsinghua University", name_normalized="tsinghua")
    db_session.add_all([org, org2])
    paper = Paper(
        arxiv_id="2608.06001",
        title="LLM agents for networks",
        abstract="abs",
        authors_raw=["Wei Zhang", "Li Wang"],
        categories=["cs.AI"],
        directions=[],
        tracks=[],
        status="extracted",
        has_cn_scholar=True,
        published_at=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
    )
    db_session.add(paper)
    await db_session.flush()
    lo, hi = min(pa.id, pb.id), max(pa.id, pb.id)
    rel_ab = Relationship(
        person_a_id=lo, person_b_id=hi, type="paper_cooperation",
        identity_confidence=1.0, strength=0.9, coop_count=1,
    )
    lo2, hi2 = min(pb.id, pc.id), max(pb.id, pc.id)
    rel_bc = Relationship(
        person_a_id=lo2, person_b_id=hi2, type="paper_cooperation",
        identity_confidence=1.0, strength=0.7, coop_count=1,
    )
    db_session.add_all([rel_ab, rel_bc])
    await db_session.flush()
    db_session.add_all([
        PaperAuthor(paper_id=paper.id, author_seq=0, raw_name="Wei Zhang", person_id=pa.id),
        PaperAuthor(paper_id=paper.id, author_seq=1, raw_name="Li Wang", person_id=pb.id),
        RelationshipEvidence(relationship_id=rel_ab.id, paper_id=paper.id),
        RelationshipEvidence(relationship_id=rel_bc.id, paper_id=paper.id),
        PersonOrg(person_id=pa.id, org_id=org.id, org_confidence=0.6, source="glm"),
        PersonResearchTag(person_id=pa.id, tag="llm agent"),
        DisambiguationQueue(person_a_id=pa.id, person_b_id=pb.id, score=0.6, status="pending"),
    ])
    await db_session.commit()
    return {"pa": pa, "pb": pb, "pc": pc, "org": org, "org2": org2,
            "paper": paper, "rel_ab": rel_ab, "rel_bc": rel_bc}

async def _edits_of(session, entity_type: str, entity_id: int) -> list[AdminEdit]:
    return (
        (
            await session.execute(
                select(AdminEdit)
                .where(AdminEdit.entity_type == entity_type, AdminEdit.entity_id == entity_id)
                .order_by(AdminEdit.id)
            )
        )
        .scalars()
        .all()
    )


# ---------- FR-1 字段编辑 ----------


async def test_update_person_fields_with_chinese_rename(client, db_session):
    seed = await _seed_edit_graph(db_session)
    pid = seed["pa"].id

    resp = await client.patch(f"/api/admin/persons/{pid}", json={
        "name": "张三", "title": "教授", "reason": "改名修正",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "张三"
    assert body["title"] == "教授"

    person = await db_session.get(Person, pid)
    assert person.name == "张三"
    # 改名重算 name_normalized（FR-1.2，中文归一）
    assert person.name_normalized == normalize_person_name("张三")

    # NFR-4：日志成对（before 旧名 / after 新名）
    rows = await _edits_of(db_session, "person", pid)
    assert len(rows) == 1
    assert rows[0].action == "update_person"
    assert rows[0].before["name"] == "Wei Zhang"
    assert rows[0].after["name"] == "张三"
    assert rows[0].reason == "改名修正"
    assert rows[0].created_at is not None


async def test_update_person_404_and_409(client, db_session):
    seed = await _seed_edit_graph(db_session)

    resp = await client.patch("/api/admin/persons/99999", json={"name": "x", "reason": "r"})
    assert resp.status_code == 404

    # 合并墓碑拒改（409）
    merged = seed["pc"]
    merged.merged_into_id = seed["pa"].id
    await db_session.commit()
    resp = await client.patch(f"/api/admin/persons/{merged.id}", json={"name": "x", "reason": "r"})
    assert resp.status_code == 409

    # 合规删除墓碑拒改（409）
    resp = await client.request("DELETE", f"/api/admin/persons/{seed['pb'].id}", json={"reason": "测试删除"})
    assert resp.status_code == 200
    resp = await client.patch(f"/api/admin/persons/{seed['pb'].id}", json={"name": "x", "reason": "r"})
    assert resp.status_code == 409

    # 空字段集 → 422
    resp = await client.patch(f"/api/admin/persons/{seed['pa'].id}", json={"reason": "r"})
    assert resp.status_code == 422


# ---------- FR-2 机构归属 / FR-3 标签 ----------


async def test_set_orgs_and_unknown_org_404(client, db_session):
    seed = await _seed_edit_graph(db_session)
    pid = seed["pa"].id

    resp = await client.put(f"/api/admin/persons/{pid}/orgs", json={
        "org_ids": [seed["org2"].id], "reason": "归属修正",
    })
    assert resp.status_code == 200
    assert [o["name"] for o in resp.json()["orgs"]] == ["Tsinghua University"]

    rows = (
        await db_session.execute(select(PersonOrg).where(PersonOrg.person_id == pid))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].org_id == seed["org2"].id
    assert rows[0].source == "admin"  # FR-2.3
    assert float(rows[0].org_confidence) == 1.0

    # NFR-4：before/after 快照带机构名
    log = (await _edits_of(db_session, "person", pid))[-1]
    assert log.action == "set_orgs"
    assert log.before["orgs"] == ["Peking University"]
    assert log.after["orgs"] == ["Tsinghua University"]

    # 不存在机构 → 404，且不产生半成品写（仍只有 1 行归属）
    resp = await client.put(f"/api/admin/persons/{pid}/orgs", json={
        "org_ids": [99999], "reason": "r",
    })
    assert resp.status_code == 404
    rows = (
        await db_session.execute(select(PersonOrg).where(PersonOrg.person_id == pid))
    ).scalars().all()
    assert len(rows) == 1


async def test_set_research_tags_replace(client, db_session):
    seed = await _seed_edit_graph(db_session)
    pid = seed["pa"].id

    resp = await client.put(f"/api/admin/persons/{pid}/research-tags", json={
        "tags": ["联邦学习", " LLM agent ", ""], "reason": "方向更新",
    })
    assert resp.status_code == 200
    # 整组替换：去空白、去空串（FR-3）
    assert set(resp.json()["tags"]) == {"联邦学习", "LLM agent"}

    tags = (
        await db_session.execute(
            select(PersonResearchTag.tag).where(PersonResearchTag.person_id == pid)
        )
    ).scalars().all()
    assert set(tags) == {"联邦学习", "LLM agent"}

    log = (await _edits_of(db_session, "person", pid))[-1]
    assert log.action == "set_research_tags"
    assert log.before["tags"] == ["llm agent"]


# ---------- FR-4 关系删除 / 强度调整 ----------


async def test_delete_relationship_no_resurrection(client, db_session):
    """FR-4.2：删关系后 linker 重跑不复活；证据保留作审计（RD-2）。"""
    seed = await _seed_edit_graph(db_session)
    rel = seed["rel_ab"]
    paper = seed["paper"]

    resp = await client.request("DELETE", f"/api/admin/relationships/{rel.id}", json={"reason": "误判"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    rel_id, paper_id, a_id, b_id = rel.id, paper.id, rel.person_a_id, rel.person_b_id
    db_session.expire_all()
    rel_db = await db_session.get(Relationship, rel_id)
    assert rel_db.deleted_at is not None
    assert rel_db.deleted_reason == "误判"
    # 证据保留（FR-4 与 FR-5 的不对称是有意的）
    n_ev = len(
        (
            await db_session.execute(
                select(RelationshipEvidence).where(RelationshipEvidence.relationship_id == rel_id)
            )
        ).scalars().all()
    )
    assert n_ev == 1

    # NFR-1 幂等入口的等价面：重跑论文建链，不复活、不新建重复行
    paper = await db_session.get(Paper, paper_id)
    await link_paper(db_session, paper)
    await db_session.commit()
    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(Relationship).where(
                    Relationship.person_a_id == a_id,
                    Relationship.person_b_id == b_id,
                    Relationship.type == "paper_cooperation",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # 没有新建重复行（查询不过滤墓碑的原因）
    assert rows[0].deleted_at is not None  # 墓碑未复活

    # 重复删除 → 409（FR-4.4）
    resp = await client.request("DELETE", f"/api/admin/relationships/{rel_id}", json={"reason": "再删"})
    assert resp.status_code == 409


async def test_adjust_strength_bounds(client, db_session):
    seed = await _seed_edit_graph(db_session)
    rel = seed["rel_ab"]

    for bad in (1.5, -0.1):
        resp = await client.patch(f"/api/admin/relationships/{rel.id}", json={
            "strength": bad, "reason": "r",
        })
        assert resp.status_code == 422

    resp = await client.patch(f"/api/admin/relationships/{rel.id}", json={
        "strength": 0.3, "reason": "可疑关系降权",
    })
    assert resp.status_code == 200
    assert resp.json()["strength"] == pytest.approx(0.3)

    log = (await _edits_of(db_session, "relationship", rel.id))[-1]
    assert log.action == "adjust_strength"
    assert log.before == {"strength": pytest.approx(0.9), "adjusted": False}
    assert log.after == {"strength": pytest.approx(0.3), "adjusted": True}


# ---------- FR-5 合规级联删除 ----------


async def test_delete_person_cascade_and_visibility(client, db_session):
    seed = await _seed_edit_graph(db_session)
    pa = seed["pa"]
    pa_id, rel_ab_id, paper_id = pa.id, seed["rel_ab"].id, seed["paper"].id

    resp = await client.request("DELETE", f"/api/admin/persons/{pa_id}", json={"reason": "当事人要求删除"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["cascaded"]["relations"] == 1  # rel_ab（B-C 与他无关）

    db_session.expire_all()
    # 人墓碑
    person = await db_session.get(Person, pa_id)
    assert person.deleted_at is not None
    # 其关系墓碑 + 证据物理删除（合规抹除）
    rel_ab = await db_session.get(Relationship, rel_ab_id)
    assert rel_ab.deleted_at is not None
    n_ev = len(
        (
            await db_session.execute(
                select(RelationshipEvidence).where(
                    RelationshipEvidence.relationship_id == rel_ab_id
                )
            )
        ).scalars().all()
    )
    assert n_ev == 0
    # 归属/标签清空；论文挂接置 NULL 但行保留
    assert len((await db_session.execute(
        select(PersonOrg).where(PersonOrg.person_id == pa_id))).scalars().all()) == 0
    assert len((await db_session.execute(
        select(PersonResearchTag).where(PersonResearchTag.person_id == pa_id))).scalars().all()) == 0
    pa_rows = (await db_session.execute(
        select(PaperAuthor).where(PaperAuthor.paper_id == paper_id))).scalars().all()
    assert len(pa_rows) == 2  # 行保留，作者名单不缺位
    assert all(r.person_id != pa_id for r in pa_rows)
    # 论文实体不动（RD-4）
    assert await db_session.get(Paper, paper_id) is not None
    # 待审合并取消
    q = (await db_session.execute(select(DisambiguationQueue))).scalars().one()
    assert q.status == "cancelled"

    # AC-4 全可见面消失：图谱 / 搜索 / 详情
    g = (await client.get("/api/graph")).json()
    assert pa_id not in {n["id"] for n in g["nodes"]}
    s = (await client.get(f"/api/persons/search?q=Wei&type=name")).json()
    assert pa_id not in {r["id"] for r in s["items"]}
    assert (await client.get(f"/api/persons/{pa_id}")).status_code == 404

    # 剩余活关系（B-C）带证据 → C1-C10 全绿（AC-4 / NFR-2）
    report = await check_integrity(db_session)
    assert report["ok"], [c for c in report["checks"] if c["violations"]]


async def test_integrity_green_on_mixed_tombstone_db(client, db_session):
    """NFR-2：FR-4 墓碑（证据保留）与 FR-5 级联（证据清除）混存时巡检全绿。"""
    seed = await _seed_edit_graph(db_session)
    # FR-4：删 B-C（证据保留在墓碑行上）
    resp = await client.request("DELETE", f"/api/admin/relationships/{seed['rel_bc'].id}", json={"reason": "误判"})
    assert resp.status_code == 200
    # FR-5：删 A（级联清证据）
    resp = await client.request("DELETE", f"/api/admin/persons/{seed['pa'].id}", json={"reason": "合规"})
    assert resp.status_code == 200

    report = await check_integrity(db_session)
    assert report["ok"], [c for c in report["checks"] if c["violations"]]


async def test_deleted_person_not_relinked(client, db_session):
    """FR-5.3：已删除人不再进新关系（linker 拦截）。"""
    seed = await _seed_edit_graph(db_session)
    pa, pb = seed["pa"], seed["pb"]
    assert (await client.request("DELETE", f"/api/admin/persons/{pa.id}", json={"reason": "r"})).status_code == 200

    await link_paper(db_session, seed["paper"])
    await db_session.commit()
    # rel_ab 已是墓碑（级联），重跑不得复活/新建
    rows = (
        (
            await db_session.execute(
                select(Relationship).where(
                    (Relationship.person_a_id == pa.id) | (Relationship.person_b_id == pa.id)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].deleted_at is not None


# ---------- FR-6 操作日志 / 编辑视图 / 机构候选 ----------


async def test_list_edits_filter_and_pagination(client, db_session):
    seed = await _seed_edit_graph(db_session)
    pid = seed["pa"].id
    await client.patch(f"/api/admin/persons/{pid}", json={"title": "教授", "reason": "a"})
    await client.put(f"/api/admin/persons/{pid}/orgs", json={"org_ids": [], "reason": "b"})

    resp = await client.get(f"/api/admin/edits?entity_type=person&entity_id={pid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    actions = [e["action"] for e in body["items"]]
    assert set(actions) == {"update_person", "set_orgs"}
    # 倒序（最新在前）
    assert body["items"][0]["action"] == "set_orgs"
    # 分页
    page1 = (await client.get(f"/api/admin/edits?entity_type=person&entity_id={pid}&limit=1")).json()
    assert len(page1["items"]) == 1
    page2 = (
        await client.get(f"/api/admin/edits?entity_type=person&entity_id={pid}&limit=1&offset=1")
    ).json()
    assert page2["items"][0]["id"] != page1["items"][0]["id"]


async def test_edit_view_and_org_search(client, db_session):
    seed = await _seed_edit_graph(db_session)
    pa = seed["pa"]
    pa.email = "wei@example.edu"
    await db_session.commit()

    resp = await client.get(f"/api/admin/persons/{pa.id}/edit-view")
    assert resp.status_code == 200
    body = resp.json()
    # 管理面可见 email（图谱端 API 不出 email 的口径由 test_graph_api 锁定）
    assert body["email"] == "wei@example.edu"
    assert [o["id"] for o in body["orgs"]] == [seed["org"].id]
    assert body["deleted"] is False and body["merged"] is False

    assert (await client.get("/api/admin/persons/99999/edit-view")).status_code == 404

    # 机构搜索：关键字过滤（FR-2 只能选已有机构）
    resp = await client.get("/api/admin/orgs?q=tsinghua")
    assert resp.status_code == 200
    names = [o["name"] for o in resp.json()["orgs"]]
    assert names == ["Tsinghua University"]
