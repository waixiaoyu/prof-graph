"""数据不变量防护网测试（M1 做实 2026-08-26；M2-T2 扩展 C7-C10）。

C1-C10 与 app/services/integrity.py 的声明一一对应：干净库必须全过，
各类违例逐一种入必须各自命中。linker 膨胀事故（1d46d1f）的教训：
数据脏了不能等界面上看出来。
"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlalchemy import select

from app.db import get_session
from app.main import app
from app.models import (
    DisambiguationQueue,
    NewsItem,
    Paper,
    Person,
    Relationship,
    RelationshipEvidence,
    RelationshipEvidenceNews,
    RelationshipEvidencePage,
    WebPage,
)
from app.services.integrity import check_integrity
from app.utils.names import normalize_name


@pytest.fixture
async def client(db_session):
    app.dependency_overrides[get_session] = lambda: db_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _person(s, name, merged_into=None) -> Person:
    p = Person(name=name, name_normalized=normalize_name(name), merged_into_id=merged_into)
    s.add(p)
    await s.flush()
    return p


async def _paper(s, i, *, status="extracted", cn=True, published=True) -> Paper:
    p = Paper(
        arxiv_id=f"2608.07{i:02d}",
        title=f"paper {i}",
        abstract="a",
        authors_raw=[],
        categories=["cs.AI"],
        status=status,
        has_cn_scholar=cn,
        published_at=(
            dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc) if published else None
        ),
    )
    s.add(p)
    await s.flush()
    return p


async def _rel(s, a, b, papers, *, coop=None, strength=0.5, identity=0.64) -> Relationship:
    lo, hi = min(a, b), max(a, b)
    r = Relationship(
        person_a_id=lo,
        person_b_id=hi,
        type="paper_cooperation",
        identity_confidence=identity,
        strength=strength,
        coop_count=len(papers) if coop is None else coop,
    )
    s.add(r)
    await s.flush()
    for p in papers:
        s.add(RelationshipEvidence(relationship_id=r.id, paper_id=p.id))
    await s.flush()
    return r


async def _webpage(s, i, *, status="extracted") -> WebPage:
    w = WebPage(
        url=f"https://lab.example.edu/members/{i}",
        seed_id="seed-1",
        page_type="lab_members",
        title=f"成员页 {i}",
        content_hash=f"hash{i}",
        status=status,
    )
    s.add(w)
    await s.flush()
    return w


async def _news(s, i) -> NewsItem:
    n = NewsItem(source_id="qbitai", url=f"https://news.example/{i}", title=f"资讯 {i}")
    s.add(n)
    await s.flush()
    return n


async def _violations(s, prefix) -> int:
    report = await check_integrity(s)
    return next(c["violations"] for c in report["checks"] if c["check"].startswith(prefix))


async def _seed_clean(s) -> None:
    a, b = await _person(s, "Wei Zhang"), await _person(s, "Li Wang")
    p1, p2 = await _paper(s, 1), await _paper(s, 2)
    await _rel(s, a.id, b.id, [p1, p2])


# ---------- 干净库 ----------


async def test_clean_db_all_pass(db_session):
    await _seed_clean(db_session)
    report = await check_integrity(db_session)
    assert report["ok"] is True
    assert [c["violations"] for c in report["checks"]] == [0] * 10
    assert len(report["checks"]) == 10


# ---------- C1 合作数与证据一致 ----------


async def test_c1_inflated_coop_count_detected(db_session):
    """linker 膨胀事故的形态：coop_count 虚高、证据只有一行。"""
    a, b = await _person(db_session, "A One"), await _person(db_session, "B Two")
    p = await _paper(db_session, 1)
    await _rel(db_session, a.id, b.id, [p], coop=3)  # 3 次 vs 1 行证据
    assert await _violations(db_session, "C1") == 1
    assert (await check_integrity(db_session))["ok"] is False


async def test_c1_zero_evidence_relationship_detected(db_session):
    """无证据的关系违反"证据先于关系"。"""
    a, b = await _person(db_session, "A One"), await _person(db_session, "B Two")
    await _rel(db_session, a.id, b.id, [], coop=1)
    assert await _violations(db_session, "C1") == 1


# ---------- C2 分值范围 ----------


async def test_c2_out_of_bounds_strength_detected(db_session):
    a, b = await _person(db_session, "A One"), await _person(db_session, "B Two")
    p = await _paper(db_session, 1)
    await _rel(db_session, a.id, b.id, [p], strength=1.5)
    assert await _violations(db_session, "C2") == 1


# ---------- C3/C4：数据库层已双保险（唯一约束 + a<b CHECK） ----------


async def test_c3_c4_blocked_at_db_level(db_session):
    """自环与重复对在写库时即被拒（uq(a,b,type) + ck_rel_a_lt_b）；
    检查器的 C3/C4 是约束之外的第二道巡检，兜历史数据/旁路写入。"""
    from sqlalchemy.exc import IntegrityError

    a, b = await _person(db_session, "A One"), await _person(db_session, "B Two")
    p = await _paper(db_session, 1)
    await db_session.commit()  # 种子落库 + 抓好 id：回滚会过期对象，异步下不能再用属性
    a_id, b_id, p_id = a.id, b.id, p.id
    with pytest.raises(IntegrityError):  # 自环：违反 ck_rel_a_lt_b
        await _rel(db_session, a_id, a_id, [p])
    await db_session.rollback()
    r2 = Relationship(  # 不走 helper：回滚后 helper 里取 p.id 会触发过期加载
        person_a_id=min(a_id, b_id), person_b_id=max(a_id, b_id),
        type="paper_cooperation", identity_confidence=0.64, strength=0.5, coop_count=1,
    )
    db_session.add(r2)
    await db_session.flush()
    db_session.add(RelationshipEvidence(relationship_id=r2.id, paper_id=p_id))
    await db_session.commit()
    dup = Relationship(
        person_a_id=min(a_id, b_id), person_b_id=max(a_id, b_id),
        type="paper_cooperation", identity_confidence=0.64, strength=0.5, coop_count=1,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):  # 重复对：违反 uq(person_a, person_b, type)
        await db_session.flush()
    await db_session.rollback()
    assert await _violations(db_session, "C3") == 0
    assert await _violations(db_session, "C4") == 0


# ---------- C5 证据论文范围 ----------


async def test_c5_evidence_paper_out_of_scope_detected(db_session):
    a, b = await _person(db_session, "A One"), await _person(db_session, "B Two")
    p1 = await _paper(db_session, 1, status="pending_extraction")  # 未抽取
    p2 = await _paper(db_session, 2, cn=False)  # 纯外国论文
    await _rel(db_session, a.id, b.id, [p1, p2])
    assert await _violations(db_session, "C5") == 2


# ---------- C6 墓碑引用 ----------


async def test_c6_tombstone_reference_detected(db_session):
    keep = await _person(db_session, "Keep One")
    tomb = await _person(db_session, "Dropped Two", merged_into=keep.id)
    c = await _person(db_session, "C Third")
    p = await _paper(db_session, 1)
    await _rel(db_session, tomb.id, c.id, [p])  # 引用已合并学者
    assert await _violations(db_session, "C6") == 1


# ---------- C1 三表合计（M2 起证据分三张表） ----------


async def test_c1_counts_all_three_evidence_tables(db_session):
    a, b = await _person(db_session, "A One"), await _person(db_session, "B Two")
    p = await _paper(db_session, 1)
    w = await _webpage(db_session, 1)
    n = await _news(db_session, 1)
    lo, hi = min(a.id, b.id), max(a.id, b.id)
    mentor = Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                          subtype="same_lab", identity_confidence=0.9, strength=0.8, coop_count=2)
    db_session.add(mentor)
    await db_session.flush()
    db_session.add_all([
        RelationshipEvidence(relationship_id=mentor.id, paper_id=p.id),
        RelationshipEvidencePage(relationship_id=mentor.id, web_page_id=w.id),
    ])
    proj = Relationship(person_a_id=lo, person_b_id=hi, type="project_cooperation",
                        identity_confidence=0.9, strength=0.9, coop_count=1)
    db_session.add(proj)
    await db_session.flush()
    db_session.add(RelationshipEvidenceNews(relationship_id=proj.id, news_item_id=n.id))
    await db_session.commit()
    assert await _violations(db_session, "C1") == 0
    mentor.coop_count = 3  # 计数虚高
    await db_session.commit()
    assert await _violations(db_session, "C1") == 1


# ---------- C7 subtype 唯一性（唯一键之外的第二道巡检） ----------


async def test_c7_same_subtype_dup_blocked_different_subtype_clean(db_session):
    from sqlalchemy.exc import IntegrityError

    a, b = await _person(db_session, "A One"), await _person(db_session, "B Two")
    lo, hi = min(a.id, b.id), max(a.id, b.id)
    await db_session.commit()  # 种子落库 + 抓好 id：回滚会连人一起回滚（M1 已知坑）
    r1 = Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                      subtype="mentor_student", identity_confidence=0.9, strength=0.9, coop_count=1)
    db_session.add(r1)
    await db_session.commit()
    dup = Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                       subtype="mentor_student", identity_confidence=0.9, strength=0.9, coop_count=1)
    db_session.add(dup)
    with pytest.raises(IntegrityError):  # 违反 uq_rel_pair_type_subtype
        await db_session.flush()
    await db_session.rollback()
    # 不同 subtype 合法并存（RD-M2-2）：mentor_student 已在库（r1），补一条 same_lab
    other = Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                         subtype="same_lab", identity_confidence=0.9, strength=0.8, coop_count=1)
    db_session.add(other)
    await db_session.flush()
    w1, w2 = await _webpage(db_session, 1), await _webpage(db_session, 2)
    rels = (await db_session.execute(select(Relationship))).scalars().all()
    for r in rels:
        w = w1 if r.subtype == "mentor_student" else w2
        db_session.add(RelationshipEvidencePage(relationship_id=r.id, web_page_id=w.id))
    await db_session.commit()
    assert await _violations(db_session, "C4") == 0
    assert await _violations(db_session, "C7") == 0


# ---------- C8 新类型证据非空 ----------


async def test_c8_mentorship_without_evidence_detected(db_session):
    a, b = await _person(db_session, "A One"), await _person(db_session, "B Two")
    lo, hi = min(a.id, b.id), max(a.id, b.id)
    r = Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                     subtype="same_advisor", identity_confidence=0.9, strength=0.85, coop_count=0)
    db_session.add(r)
    await db_session.commit()
    assert await _violations(db_session, "C8") == 1
    w = await _webpage(db_session, 1)
    db_session.add(RelationshipEvidencePage(relationship_id=r.id, web_page_id=w.id))
    r.coop_count = 1
    await db_session.commit()
    assert await _violations(db_session, "C8") == 0


async def test_c8_project_relation_requires_news_evidence_and_project(db_session):
    a, b = await _person(db_session, "A One"), await _person(db_session, "B Two")
    lo, hi = min(a.id, b.id), max(a.id, b.id)
    r = Relationship(person_a_id=lo, person_b_id=hi, type="project_cooperation",
                     identity_confidence=0.9, strength=0.9, coop_count=1)
    db_session.add(r)
    await db_session.commit()
    # 无 news 证据 + projects 表为空 → 两个分支同时命中
    assert await _violations(db_session, "C8") == 2
    n = await _news(db_session, 1)
    db_session.add(RelationshipEvidenceNews(relationship_id=r.id, news_item_id=n.id))
    await db_session.commit()
    assert await _violations(db_session, "C8") == 1  # 有证据但 projects 表为空
    from app.models import Project

    db_session.add(Project(name="某重点项目", name_normalized="mouzhongdian"))
    await db_session.commit()
    assert await _violations(db_session, "C8") == 0


# ---------- C9 新关系值域 ----------


async def test_c9_subtype_mismatch_and_bounds_detected(db_session):
    a, b = await _person(db_session, "A One"), await _person(db_session, "B Two")
    lo, hi = min(a.id, b.id), max(a.id, b.id)
    db_session.add_all([
        # 论文合作不该带 subtype
        Relationship(person_a_id=lo, person_b_id=hi, type="paper_cooperation",
                     subtype="same_lab", identity_confidence=0.9, strength=0.85, coop_count=1),
        # 传承关系缺子类型
        Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                     subtype="", identity_confidence=0.9, strength=0.85, coop_count=1),
        # 传承关系分值越界
        Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                     subtype="same_cohort", identity_confidence=0.9, strength=1.2, coop_count=1),
    ])
    await db_session.commit()
    assert await _violations(db_session, "C9") == 3


# ---------- C10 证据表无重复主键 ----------


async def test_c10_multi_evidence_clean(db_session):
    a, b = await _person(db_session, "A One"), await _person(db_session, "B Two")
    lo, hi = min(a.id, b.id), max(a.id, b.id)
    r = Relationship(person_a_id=lo, person_b_id=hi, type="academic_mentorship",
                     subtype="same_lab", identity_confidence=0.9, strength=0.8, coop_count=2)
    db_session.add(r)
    await db_session.flush()
    w1, w2 = await _webpage(db_session, 1), await _webpage(db_session, 2)
    db_session.add_all([
        RelationshipEvidencePage(relationship_id=r.id, web_page_id=w1.id),
        RelationshipEvidencePage(relationship_id=r.id, web_page_id=w2.id),
    ])
    await db_session.commit()
    assert await _violations(db_session, "C10") == 0


# ---------- admin 端点 ----------


async def test_admin_integrity_endpoint(client, db_session):
    await _seed_clean(db_session)
    resp = await client.get("/api/admin/integrity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert {c["check"].split()[0] for c in body["checks"]} == {f"C{i}" for i in range(1, 11)}


# ---------- 合并路径过不变量（linker 一致性 + 无日期证据回归） ----------


async def test_merge_with_undated_evidence_keeps_coop_count(db_session, client):
    """回归（2026-08-26 修复）：合并后重算曾用"有日期的证据数"当合作数，
    证据论文无 published_at 时 coop_count 被压低 → 违反 C1。"""
    a = await _person(db_session, "Wei Zhang")
    b = await _person(db_session, "Wei Zhang")
    c = await _person(db_session, "Li Wang")
    p1 = await _paper(db_session, 1, published=False)  # 无日期证据
    p2 = await _paper(db_session, 2)  # 正常证据
    await _rel(db_session, b.id, c.id, [p1, p2])

    q = DisambiguationQueue(
        person_a_id=min(a.id, b.id), person_b_id=max(a.id, b.id), score=0.65, score_detail={}
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)

    resp = await client.post(f"/api/disambiguation/{q.id}/merge", json={"keep": a.id})
    assert resp.status_code == 200

    rel = (await db_session.execute(select(Relationship))).scalars().one()
    assert rel.coop_count == 2  # 证据行数，而非有日期的 1 行
    ev = (await db_session.execute(select(RelationshipEvidence))).scalars().all()
    assert len(ev) == 2
    report = await check_integrity(db_session)
    assert report["ok"] is True  # 合并后必须全过防护网
