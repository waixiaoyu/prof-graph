"""T15 单测：图谱与查询 API（FR-5.1~5.5）。"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.db import get_session
from app.main import app
from app.models import (
    Organization,
    Paper,
    PaperAuthor,
    Person,
    PersonOrg,
    PersonResearchTag,
    Relationship,
    RelationshipEvidence,
)
from app.utils.names import normalize_name


@pytest.fixture
async def client(db_session):
    """ASGI 测试客户端（不触发 lifespan，调度器不启动）。"""
    app.dependency_overrides[get_session] = lambda: db_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _mk_person(session, name: str) -> Person:
    p = Person(name=name, name_normalized=normalize_name(name))
    session.add(p)
    return p


def _mk_paper(session, arxiv_id: str, title: str, year: int,
              directions: list[str], tracks: list[str]) -> Paper:
    paper = Paper(
        arxiv_id=arxiv_id,
        title=title,
        abstract="abs",
        authors_raw=["Wei Zhang", "Li Wang"],
        categories=["cs.AI"],
        directions=directions,
        tracks=tracks,
        status="extracted", has_cn_scholar=True,
        published_at=dt.datetime(year, 6, 1, tzinfo=dt.timezone.utc),
    )
    session.add(paper)
    return paper


async def _seed_graph(db_session) -> dict:
    """3 人 3 边：A-B 0.90、B-C 0.70、A-C 0.50；B 打了 network_autonomy+ADN 标签。"""
    pa, pb, pc = _mk_person(db_session, "Wei Zhang"), _mk_person(db_session, "Li Wang"), _mk_person(db_session, "Anon Chen")
    # 与生产写入侧 upsert_organization 一致：name_normalized 由 normalize_org 生成
    org = Organization(name="Peking University", name_normalized="peking")
    db_session.add(org)

    p1 = _mk_paper(db_session, "2608.06001", "LLM agents for networks", 2026,
                   ["network_autonomy"], ["ADN"])
    p2 = _mk_paper(db_session, "2607.07002", "Traffic prediction", 2025, [], [])
    await db_session.flush()
    db_session.add_all([
        PaperAuthor(paper_id=p1.id, author_seq=0, raw_name="Wei Zhang", person_id=pa.id),
        PaperAuthor(paper_id=p1.id, author_seq=1, raw_name="Li Wang", person_id=pb.id),
        PaperAuthor(paper_id=p2.id, author_seq=0, raw_name="Anon Chen", person_id=pc.id),
        PersonOrg(person_id=pa.id, org_id=org.id, org_confidence=1.0, source="glm", paper_id=p1.id),
        PersonResearchTag(person_id=pb.id, tag="llm agent"),
    ])
    rel_ab = Relationship(person_a_id=min(pa.id, pb.id), person_b_id=max(pa.id, pb.id),
                          type="paper_cooperation", identity_confidence=1.0, strength=0.90,
                          coop_count=2, evidence_summary="基于 2 篇合作论文")
    rel_bc = Relationship(person_a_id=min(pb.id, pc.id), person_b_id=max(pb.id, pc.id),
                          type="paper_cooperation", identity_confidence=1.0, strength=0.70,
                          coop_count=1)
    rel_ac = Relationship(person_a_id=min(pa.id, pc.id), person_b_id=max(pa.id, pc.id),
                          type="paper_cooperation", identity_confidence=1.0, strength=0.50,
                          coop_count=1)
    db_session.add_all([rel_ab, rel_bc, rel_ac])
    await db_session.flush()
    db_session.add(RelationshipEvidence(relationship_id=rel_ab.id, paper_id=p1.id))
    await db_session.commit()
    return {"pa": pa, "pb": pb, "pc": pc, "p1": p1, "rel_ab": rel_ab}


# ---------- GET /api/graph ----------

async def test_graph_returns_all_edges_sorted(client, db_session):
    await _seed_graph(db_session)
    resp = await client.get("/api/graph")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["nodes"]) == 3 and len(data["edges"]) == 3
    strengths = [e["strength"] for e in data["edges"]]
    assert strengths == sorted(strengths, reverse=True)
    node = next(n for n in data["nodes"] if n["name"] == "Wei Zhang")
    assert node["orgs"][0]["name"] == "Peking University"
    assert node["paper_count"] == 1


async def test_graph_strength_min_filters_weak_edges(client, db_session):
    await _seed_graph(db_session)
    data = (await client.get("/api/graph", params={"strength_min": 0.6})).json()
    assert [e["strength"] for e in data["edges"]] == [0.90, 0.70]
    assert {n["name"] for n in data["nodes"]} == {"Wei Zhang", "Li Wang", "Anon Chen"}


async def test_graph_direction_filter_keeps_tagged_subnetwork(client, db_session):
    """direction=network_autonomy：只留与带标论文关联的端点（A、B）。"""
    await _seed_graph(db_session)
    data = (await client.get("/api/graph", params={"direction": "network_autonomy"})).json()
    assert {n["name"] for n in data["nodes"]} == {"Wei Zhang", "Li Wang"}
    assert {e["strength"] for e in data["edges"]} == {0.90}


async def test_graph_track_filter_and_limit(client, db_session):
    await _seed_graph(db_session)
    data = (await client.get("/api/graph", params={"track": "ADN"})).json()
    assert {n["name"] for n in data["nodes"]} == {"Wei Zhang", "Li Wang"}
    limited = (await client.get("/api/graph", params={"limit": 1})).json()
    assert len(limited["edges"]) == 1 and limited["edges"][0]["strength"] == 0.90


# ---------- GET /api/persons/search ----------

async def test_persons_search_by_name_like(client, db_session):
    await _seed_graph(db_session)
    data = (await client.get("/api/persons/search", params={"q": "wei zh"})).json()
    assert [i["name"] for i in data["items"]] == ["Wei Zhang"]
    assert data["items"][0]["org"] == "Peking University"


async def test_persons_search_by_org(client, db_session):
    await _seed_graph(db_session)
    data = (await client.get("/api/persons/search", params={"q": "peking", "type": "org"})).json()
    assert [i["name"] for i in data["items"]] == ["Wei Zhang"]
    none = (await client.get("/api/persons/search", params={"q": "tsinghua", "type": "org"})).json()
    assert none["items"] == []


async def test_persons_search_rejects_bad_type(client, db_session):
    resp = await client.get("/api/persons/search", params={"q": "x", "type": "email"})
    assert resp.status_code == 422


# ---------- GET /api/persons/{id} ----------

async def test_person_detail(client, db_session):
    seed = await _seed_graph(db_session)
    resp = await client.get(f"/api/persons/{seed['pb'].id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Li Wang"
    assert data["research_tags"] == ["llm agent"]
    assert [p["arxiv_id"] for p in data["papers"]] == ["2608.06001"]
    assert data["papers"][0]["year"] == 2026
    assert data["papers"][0]["tracks"] == ["ADN"]


async def test_person_detail_404(client, db_session):
    resp = await client.get("/api/persons/9999")
    assert resp.status_code == 404


# ---------- GET /api/relationships/{id}/evidence ----------

async def test_relationship_evidence(client, db_session):
    seed = await _seed_graph(db_session)
    resp = await client.get(f"/api/relationships/{seed['rel_ab'].id}/evidence")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["title"] == "LLM agents for networks"
    assert items[0]["year"] == 2026


async def test_relationship_evidence_404(client, db_session):
    resp = await client.get("/api/relationships/9999/evidence")
    assert resp.status_code == 404


# ---------- 机构切入 / 老师切入 / M1 范围（2026-08-31） ----------

async def test_graph_org_pivot(client, db_session):
    """org=Peking University：只留任一端为该机构成员的关系（A-B、A-C）。"""
    seed = await _seed_graph(db_session)
    data = (await client.get("/api/graph", params={"org": "Peking University"})).json()
    assert {e["strength"] for e in data["edges"]} == {0.90, 0.50}
    assert {n["name"] for n in data["nodes"]} == {"Wei Zhang", "Li Wang", "Anon Chen"}
    # 不存在的机构 → 空
    empty = (await client.get("/api/graph", params={"org": "MIT"})).json()
    assert empty["edges"] == [] and empty["nodes"] == []
    # 名称变体（URL 手输/分享场景）走 normalize_org 归一化匹配：Univ. ≡ University
    variant = (await client.get("/api/graph", params={"org": "Peking Univ."})).json()
    assert {e["strength"] for e in variant["edges"]} == {0.90, 0.50}


async def test_graph_person_pivot_ego_network(client, db_session):
    """person=B：以 B 为中心的 1-hop 关系，limit 内补邻居间关联。"""
    seed = await _seed_graph(db_session)
    b = seed["pb"]
    # limit=2：取 B 的两条边（0.90、0.70），无余额补 A-C
    data = (await client.get("/api/graph", params={"person": b.id, "limit": 2})).json()
    assert {e["strength"] for e in data["edges"]} == {0.90, 0.70}
    # limit 充足：补上邻居 A-C 之间的关联
    full = (await client.get("/api/graph", params={"person": b.id})).json()
    assert {e["strength"] for e in full["edges"]} == {0.90, 0.70, 0.50}


async def test_persons_search_excludes_out_of_scope(client, db_session):
    """范围外的人（只出现在纯外国论文上）不出现在搜索。"""
    seed = await _seed_graph(db_session)
    out = Person(name="Anna Mueller", name_normalized="annamueller")
    db_session.add(out)
    paper = Paper(
        arxiv_id="2608.09003", title="Foreign only", abstract="",
        authors_raw=["Anna Mueller"], categories=["cs.AI"],
        status="extracted", has_cn_scholar=False,
        published_at=dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
    )
    db_session.add(paper)
    await db_session.flush()
    db_session.add(PaperAuthor(paper_id=paper.id, author_seq=0,
                               raw_name="Anna Mueller", person_id=out.id))
    await db_session.commit()

    data = (await client.get("/api/persons/search", params={"q": "anna"})).json()
    assert data["items"] == []
    # 范围内的人不受影响
    kept = (await client.get("/api/persons/search", params={"q": "wei zh"})).json()
    assert [i["name"] for i in kept["items"]] == ["Wei Zhang"]


async def test_filters_options_include_orgs(client, db_session):
    await _seed_graph(db_session)
    data = (await client.get("/api/filters/options")).json()
    assert data["orgs"] == ["Peking University"]
