"""T16 单测：审核队列 API（FR-3.4，AC-9）。"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy import select

from app.db import get_session
from app.main import app
from app.models import (
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
from app.utils.names import normalize_name


@pytest.fixture
async def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _person(session, name: str) -> Person:
    p = Person(name=name, name_normalized=normalize_name(name))
    session.add(p)
    return p


async def _paper(session, i: int, year: int) -> Paper:
    p = Paper(
        arxiv_id=f"2608.0600{i}",
        title=f"paper {i}",
        abstract="a",
        authors_raw=[],
        categories=["cs.AI"],
        status="extracted", has_cn_scholar=True,
        published_at=dt.datetime(year, 3, 1, tzinfo=dt.timezone.utc),
    )
    session.add(p)
    return p


async def _rel(session, a: int, b: int, papers: list[Paper], strength=0.5) -> Relationship:
    lo, hi = min(a, b), max(a, b)
    r = Relationship(
        person_a_id=lo, person_b_id=hi, type="paper_cooperation",
        identity_confidence=0.64, strength=strength, coop_count=len(papers),
        time_start=min(p.published_at.date() for p in papers),
        time_end=max(p.published_at.date() for p in papers),
    )
    session.add(r)
    await session.flush()
    for p in papers:
        session.add(RelationshipEvidence(relationship_id=r.id, paper_id=p.id))
    return r


async def _seed_merge_scenario(db_session) -> dict:
    """A、B 待合并；C 第三者：A-C 1 篇(2024)，B-C 2 篇(2025/2026)，A-B 直连 1 篇。"""
    a = await _person(db_session, "Wei Zhang")
    b = await _person(db_session, "Wei Zhang")  # 同名不同实体
    c = await _person(db_session, "Li Wang")
    org_pku = Organization(name="Peking University", name_normalized="peking")
    org_thu = Organization(name="Tsinghua University", name_normalized="tsinghua")
    db_session.add_all([org_pku, org_thu])
    await db_session.flush()

    papers = [await _paper(db_session, i, y) for i, y in enumerate([2024, 2025, 2026, 2026])]
    p1, p2, p3, p4 = papers
    await db_session.flush()
    db_session.add_all([
        PaperAuthor(paper_id=p1.id, author_seq=0, raw_name="Wei Zhang", person_id=a.id),
        PaperAuthor(paper_id=p1.id, author_seq=1, raw_name="Li Wang", person_id=c.id),
        PaperAuthor(paper_id=p2.id, author_seq=0, raw_name="Wei Zhang", person_id=b.id),
        PaperAuthor(paper_id=p2.id, author_seq=1, raw_name="Li Wang", person_id=c.id),
        PaperAuthor(paper_id=p3.id, author_seq=0, raw_name="Wei Zhang", person_id=b.id),
        PaperAuthor(paper_id=p3.id, author_seq=1, raw_name="Li Wang", person_id=c.id),
        PaperAuthor(paper_id=p4.id, author_seq=0, raw_name="Wei Zhang", person_id=a.id),
        PaperAuthor(paper_id=p4.id, author_seq=1, raw_name="Wei Zhang", person_id=b.id),
        PersonOrg(person_id=a.id, org_id=org_pku.id, org_confidence=1.0, source="glm", paper_id=p1.id),
        PersonOrg(person_id=b.id, org_id=org_thu.id, org_confidence=0.8, source="openalex", paper_id=p2.id),
        PersonResearchTag(person_id=b.id, tag="llm agent"),
    ])
    await _rel(db_session, a.id, c.id, [p1], strength=0.54)
    await _rel(db_session, b.id, c.id, [p2, p3], strength=0.58)
    await _rel(db_session, a.id, b.id, [p4], strength=0.85)

    q = DisambiguationQueue(
        person_a_id=min(a.id, b.id), person_b_id=max(a.id, b.id),
        score=0.65, score_detail={"name": 1.0, "org": 0.4, "research": 0.5, "time": 0.6, "network": 0.2},
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return {"a": a, "b": b, "c": c, "q": q, "p1": p1, "p2": p2, "p3": p3, "p4": p4,
            "org_pku": org_pku, "org_thu": org_thu}


async def test_list_pending_with_score_detail(client, db_session):
    seed = await _seed_merge_scenario(db_session)
    resp = await client.get("/api/disambiguation")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    item = items[0]
    names = {item["person_a"]["name"], item["person_b"]["name"]}
    assert names == {"Wei Zhang"}
    assert item["score"] == 0.65
    assert item["score_detail"]["name"] == 1.0
    empty = (await client.get("/api/disambiguation", params={"status": "merged"})).json()
    assert empty["items"] == []


async def test_merge_unifies_person_and_recomputes(client, db_session):
    seed = await _seed_merge_scenario(db_session)
    a, b, c = seed["a"], seed["b"], seed["c"]
    resp = await client.post(f"/api/disambiguation/{seed['q'].id}/merge", json={"keep": a.id})
    assert resp.status_code == 200
    assert resp.json() == {"status": "merged", "kept": a.id, "merged_into": a.id, "removed": b.id}

    # B 置为墓碑（保留行供审计），活跃人只剩 A、C；署名/标签/机构全部归 A
    persons = (await db_session.execute(select(Person))).scalars().all()
    b_row = next(p for p in persons if p.id == b.id)
    assert b_row.merged_into_id == a.id
    live = {p.id for p in persons if p.merged_into_id is None}
    assert live == {a.id, c.id}
    pa_rows = (await db_session.execute(select(PaperAuthor))).scalars().all()
    assert {r.person_id for r in pa_rows} == {a.id, c.id}
    tags = (await db_session.execute(select(PersonResearchTag))).scalars().all()
    assert {t.person_id for t in tags} == {a.id} and tags[0].tag == "llm agent"
    orgs = (
        await db_session.execute(select(PersonOrg).join(Organization))
    ).scalars().all()
    a_orgs = [o for o in orgs if o.person_id == a.id]
    assert len(a_orgs) == 2  # PKU 1.0 + THU（迁移保留）

    # 关系：A-C 合并后 3 篇证据（p4 自环删除不计）；strength 重算
    rels = (await db_session.execute(select(Relationship))).scalars().all()
    assert len(rels) == 1
    rel = rels[0]
    lo, hi = min(a.id, c.id), max(a.id, c.id)
    assert (rel.person_a_id, rel.person_b_id) == (lo, hi)
    assert rel.coop_count == 3
    ev = (
        await db_session.execute(select(RelationshipEvidence))
    ).scalars().all()
    assert len(ev) == 3  # p1 p2 p3
    # identity(A)=0.4+0.6×1.0=1.0；identity(C)=0.4+0.6×0.4=0.64；tier(3)=0.95
    assert float(rel.strength) == pytest.approx(0.64 * 0.95, abs=0.005)
    assert rel.time_start == seed["p1"].published_at.date()
    assert rel.time_end == seed["p3"].published_at.date()

    # 队列已结
    await db_session.refresh(seed["q"])
    assert seed["q"].status == "merged" and seed["q"].resolved_at is not None

    # 墓碑不再出现在搜索结果（同名人只回保留者）
    data = (await client.get("/api/persons/search", params={"q": "wei"})).json()
    assert [i["id"] for i in data["items"]] == [a.id]


async def test_merge_conflicts_and_validation(client, db_session):
    seed = await _seed_merge_scenario(db_session)
    # keep 不属于两端
    resp = await client.post(
        f"/api/disambiguation/{seed['q'].id}/merge", json={"keep": seed["c"].id}
    )
    assert resp.status_code == 422
    # 不存在
    assert (await client.post("/api/disambiguation/999/merge", json={"keep": 1})).status_code == 404
    # 重复处理
    await client.post(f"/api/disambiguation/{seed['q'].id}/merge", json={"keep": seed["a"].id})
    resp = await client.post(
        f"/api/disambiguation/{seed['q'].id}/merge", json={"keep": seed["a"].id}
    )
    assert resp.status_code == 409


async def test_reject_persists_and_blocks_requeue(client, db_session):
    seed = await _seed_merge_scenario(db_session)
    a, b = seed["a"], seed["b"]
    resp = await client.post(f"/api/disambiguation/{seed['q'].id}/reject")
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    # 重复 reject → 409
    assert (await client.post(f"/api/disambiguation/{seed['q'].id}/reject")).status_code == 409

    # 消歧器再次尝试同对入队 → uq_disamb_pair 拦截，不新增
    from app.services.disambiguator import ScoreDetail, _enqueue
    detail = ScoreDetail(name=1.0, org=0.4, research=0.5, time=0.5, network=0.2)
    await _enqueue(db_session, a.id, b.id, detail)
    rows = (await db_session.execute(select(DisambiguationQueue))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "rejected"

    # 新作者与 A 仍可正常入队（不误伤）
    d = await _person(db_session, "Wei Zhang")
    await db_session.flush()
    await _enqueue(db_session, a.id, d.id, detail)
    rows = (await db_session.execute(select(DisambiguationQueue))).scalars().all()
    assert len(rows) == 2
