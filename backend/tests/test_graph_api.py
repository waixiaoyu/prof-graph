"""T15 单测：图谱与查询 API（FR-5.1~5.5）。"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.db import get_session
from app.main import app
from app.models import (
    NewsItem,
    Organization,
    Paper,
    PaperAuthor,
    Person,
    PersonOrg,
    PersonResearchTag,
    Relationship,
    RelationshipEvidence,
    RelationshipEvidenceNews,
    RelationshipEvidencePage,
    WebPage,
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


async def test_graph_coop_min_filters_single_cooperation(client, db_session):
    """coop_min=2：只留合作 ≥2 次的关系（单次合作隐藏，节点随之收窄）。"""
    await _seed_graph(db_session)
    data = (await client.get("/api/graph", params={"coop_min": 2})).json()
    assert [e["coop_count"] for e in data["edges"]] == [2]
    assert {n["name"] for n in data["nodes"]} == {"Wei Zhang", "Li Wang"}


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
    # 合作伙伴列表（按强度降序）：供详情面板直达证据链，不用瞄准细边点击
    partners = data["partners"]
    assert [p["name"] for p in partners] == ["Wei Zhang", "Anon Chen"]
    assert partners[0]["relationship_id"] == seed["rel_ab"].id
    assert partners[0]["person_id"] == seed["pa"].id
    assert partners[0]["coop_count"] == 2
    assert partners[0]["strength"] == 0.90
    assert partners[0]["org"] == "Peking University"


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


# ---------- M2-T13：rel_types 筛选 / 混合证据 / title·homepage（FR-7.1~7.4） ----------


async def _seed_m2_edges(db_session) -> dict:
    """在 _seed_graph 基础上加学术传承边（网页证据）与项目合作边（资讯证据）。"""
    seed = await _seed_graph(db_session)
    pa, pb, pc = seed["pa"], seed["pb"], seed["pc"]
    page = WebPage(
        url="https://lab.example.edu/people", seed_id="thu-nisl-members",
        page_type="lab_members", title="NISL 成员", status="extracted",
        fetched_at=dt.datetime(2026, 8, 27, 5, 0, tzinfo=dt.timezone.utc),
    )
    news = NewsItem(
        source_id="qbitai", url="https://news.example.com/a/1.html",
        title="两校共建联合实验室", status="extracted",
        published_at=dt.datetime(2026, 8, 24, 8, 0, tzinfo=dt.timezone.utc),
    )
    db_session.add_all([page, news])
    await db_session.flush()
    mentor = Relationship(
        person_a_id=min(pa.id, pb.id), person_b_id=max(pa.id, pb.id),
        type="academic_mentorship", subtype="mentor_student",
        identity_confidence=1.0, strength=0.85, coop_count=0,
        evidence_summary="基于实验室官网成员页，导师-学生",
    )
    coop = Relationship(
        person_a_id=min(pb.id, pc.id), person_b_id=max(pb.id, pc.id),
        type="project_cooperation", subtype="",
        identity_confidence=1.0, strength=0.62, coop_count=1,
        evidence_summary="基于高校新闻，共同参与联合实验室",
    )
    db_session.add_all([mentor, coop])
    await db_session.flush()
    db_session.add_all([
        RelationshipEvidencePage(relationship_id=mentor.id, web_page_id=page.id),
        RelationshipEvidenceNews(relationship_id=coop.id, news_item_id=news.id),
    ])
    await db_session.commit()
    return {**seed, "page": page, "news": news, "mentor": mentor, "coop": coop}


async def test_graph_rel_types_filters(client, db_session):
    """默认三类型全开且边带 type/subtype；rel_types 只留指定类型；非法值 400。"""
    seed = await _seed_m2_edges(db_session)
    data = (await client.get("/api/graph")).json()
    by_type = {}
    for e in data["edges"]:
        by_type.setdefault(e["type"], []).append(e)
    assert set(by_type) == {"paper_cooperation", "academic_mentorship", "project_cooperation"}
    mentor_edge = by_type["academic_mentorship"][0]
    assert mentor_edge["subtype"] == "mentor_student"
    assert by_type["project_cooperation"][0]["subtype"] == ""

    only = (await client.get("/api/graph", params={"rel_types": "academic_mentorship"})).json()
    assert len(only["edges"]) == 1 and only["edges"][0]["id"] == seed["mentor"].id
    assert {n["name"] for n in only["nodes"]} == {"Wei Zhang", "Li Wang"}

    two = (
        await client.get("/api/graph", params={"rel_types": "academic_mentorship,project_cooperation"})
    ).json()
    assert {e["type"] for e in two["edges"]} == {"academic_mentorship", "project_cooperation"}

    resp = await client.get("/api/graph", params={"rel_types": "advisor"})
    assert resp.status_code == 400
    resp = await client.get("/api/graph", params={"rel_types": ","})
    assert resp.status_code == 400


async def test_relationship_evidence_mixed(client, db_session):
    """传承边返回网页证据、项目边返回资讯证据、论文边返回论文证据（均含 URL/时间）。"""
    seed = await _seed_m2_edges(db_session)

    mentor_ev = (await client.get(f"/api/relationships/{seed['mentor'].id}/evidence")).json()
    assert mentor_ev["type"] == "academic_mentorship"
    assert mentor_ev["subtype"] == "mentor_student"
    assert mentor_ev["web_pages"] == [{
        "web_page_id": seed["page"].id,
        "title": "NISL 成员",
        "url": "https://lab.example.edu/people",
        "page_type": "lab_members",
        "fetched_at": "2026-08-27T05:00:00+00:00",
    }]
    assert mentor_ev["papers"] == [] and mentor_ev["news_items"] == []

    coop_ev = (await client.get(f"/api/relationships/{seed['coop'].id}/evidence")).json()
    assert coop_ev["web_pages"] == []
    assert coop_ev["news_items"] == [{
        "news_item_id": seed["news"].id,
        "title": "两校共建联合实验室",
        "url": "https://news.example.com/a/1.html",
        "source": "qbitai",
        "published_at": "2026-08-24T08:00:00+00:00",
    }]

    paper_ev = (await client.get(f"/api/relationships/{seed['rel_ab'].id}/evidence")).json()
    assert paper_ev["papers"][0]["url"] == "https://arxiv.org/abs/2608.06001"
    assert paper_ev["papers"][0]["year"] == 2026
    # M1 前端兼容：items 仍为论文简要列表
    assert paper_ev["items"][0]["title"] == "LLM agents for networks"


async def test_person_detail_title_homepage(client, db_session):
    """详情带 title/homepage（FR-7.4）；email 不出现在图谱端 API；partners 带关系类型。"""
    seed = await _seed_m2_edges(db_session)
    seed["pb"].title = "长聘副教授"
    seed["pb"].homepage = "https://liwang.example.edu"
    seed["pb"].email = "liwang@example.edu"
    await db_session.commit()

    data = (await client.get(f"/api/persons/{seed['pb'].id}")).json()
    assert data["title"] == "长聘副教授"
    assert data["homepage"] == "https://liwang.example.edu"
    assert "email" not in data
    types = {p["relationship_id"]: (p["type"], p["subtype"]) for p in data["partners"]}
    assert types[seed["mentor"].id] == ("academic_mentorship", "mentor_student")
    assert types[seed["coop"].id] == ("project_cooperation", "")


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
    # M2-T0：关系类型固定三项（RD-M2-13）
    assert [t["id"] for t in data["relationship_types"]] == [
        "paper_cooperation", "academic_mentorship", "project_cooperation",
    ]


# ---------- 参数边界 / pivot 优先级 / 空证据 ----------


async def test_graph_limit_bounds_validated(client, db_session):
    """limit 域 [1,1000]：0 与 1001 → 422，1000 正常。"""
    await _seed_graph(db_session)
    assert (await client.get("/api/graph", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/graph", params={"limit": 1001})).status_code == 422
    assert (await client.get("/api/graph", params={"limit": 1000})).status_code == 200


async def test_graph_filter_param_ranges_validated(client, db_session):
    """strength_min 域 [0,1]、coop_min 域 [0,20]：越界 422（防恶意大值全表扫）。"""
    await _seed_graph(db_session)
    assert (await client.get("/api/graph", params={"strength_min": 1.5})).status_code == 422
    assert (await client.get("/api/graph", params={"strength_min": -0.1})).status_code == 422
    assert (await client.get("/api/graph", params={"coop_min": 21})).status_code == 422


async def test_graph_person_overrides_org(client, db_session):
    """person 与 org 同给：person 切入优先（elif 语义），org 不参与过滤。"""
    seed = await _seed_graph(db_session)
    pc = seed["pc"]
    # pc 不在 Peking org：若 org 条件被叠加（错误实现），B-C 边会被过滤掉
    data = (
        await client.get("/api/graph", params={"person": pc.id, "org": "Peking University"})
    ).json()
    pairs = {tuple(sorted((e["source"], e["target"]))) for e in data["edges"]}
    assert len(pairs) == 3  # 1-hop（A-C/B-C）+ 邻居关联（A-B）全在，org 未参与


async def test_graph_person_pivot_with_rel_types(client, db_session):
    """person 切入与 rel_types 叠加：只看该人指定类型的关系。"""
    seed = await _seed_m2_edges(db_session)
    pb = seed["pb"]
    only_coop = (
        await client.get(
            "/api/graph",
            params={"person": pb.id, "rel_types": "paper_cooperation,project_cooperation"},
        )
    ).json()
    types = {e["type"] for e in only_coop["edges"]}
    assert types == {"paper_cooperation", "project_cooperation"}  # 传承边被筛掉

    only_mentor = (
        await client.get(
            "/api/graph", params={"person": pb.id, "rel_types": "academic_mentorship"}
        )
    ).json()
    assert {e["type"] for e in only_mentor["edges"]} == {"academic_mentorship"}


async def test_relationship_evidence_empty_lists(client, db_session):
    """关系存在但无任何证据行：200 + 三段空数组（前端渲染空态）。"""
    seed = await _seed_graph(db_session)
    rel = Relationship(
        person_a_id=min(seed["pa"].id, seed["pc"].id),
        person_b_id=max(seed["pa"].id, seed["pc"].id),
        type="academic_mentorship", subtype="same_lab",
        identity_confidence=0.9, strength=0.5, coop_count=0,
    )
    db_session.add(rel)
    await db_session.commit()

    resp = await client.get(f"/api/relationships/{rel.id}/evidence")
    assert resp.status_code == 200
    data = resp.json()
    assert data["papers"] == [] and data["web_pages"] == [] and data["news_items"] == []
    assert data["items"] == []  # M1 兼容段同样为空
