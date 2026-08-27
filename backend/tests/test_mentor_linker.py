"""T6 单测：四子类型推导 / §4.1 公式算例 / 证据幂等合并 / 组合截断 / 失败路径。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest
from sqlalchemy import func, select

from app.models import (
    FailedJob,
    Paper,
    PaperAuthor,
    Person,
    PersonOrg,
    Relationship,
    RelationshipEvidence,
    RelationshipEvidencePage,
    WebPage,
)
from app.services.breaker import BreakerOpenError
from app.services.glm import GLMClient, TransportResult
from app.services.mentor_linker import (
    MAX_PAIRS,
    PAIRWISE_CUTOFF,
    PairSignal,
    compute_confidence,
    link_page_relations,
    link_paper_acknowledgments,
    run_mentor_link,
    _strength,
)
from app.services.openalex import upsert_organization
from app.services.page_extractor import Member, PageExtraction
from app.utils.names import normalize_person_name


class FakeTransport:
    def __init__(self, text: str | Exception = "{}"):
        self.text = text

    async def __call__(self, system: str, user: str, max_tokens: int) -> TransportResult:
        if isinstance(self.text, Exception):
            raise self.text
        return TransportResult(self.text, 1500, 1000)


async def _mk_person(db_session, name: str, orgs: list[tuple[str, str]] = ()) -> Person:
    p = Person(name=name, name_normalized=normalize_person_name(name))
    db_session.add(p)
    await db_session.flush()
    for org_name, level in orgs:
        o = await upsert_organization(db_session, org_name)
        if o.level is None:
            o.level = level
        db_session.add(PersonOrg(person_id=p.id, org_id=o.id, org_confidence=1.0, source="webpage"))
    await db_session.flush()
    return p


async def _mk_page(db_session, **kw) -> WebPage:
    content = kw.get("content", "实验室成员名单：段海鑫 教授；张三 博士生（导师：段海鑫）")
    page = WebPage(
        url=kw.get("url", "https://netsec.ccert.edu.cn/chs/people/"),
        seed_id=kw.get("seed_id", "thu-nisl-members"),
        page_type=kw.get("page_type", "lab_members"),
        title=kw.get("title", "成员"),
        content_text=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        status=kw.get("status", "pending_extraction"),
    )
    db_session.add(page)
    await db_session.flush()
    return page


LAB_JSON = json.dumps(
    {
        "lab_name": "NISL 实验室",
        "org_school": "清华大学",
        "org_department": "网络研究院",
        "page_context": "official_lab",
        "members": [
            {"name": "段海鑫", "role": "professor", "title": "教授",
             "homepage": "https://netsec.ccert.edu.cn/~duan/"},
            {"name": "张三", "role": "phd", "advisor": "段海鑫", "grad_year": None},
        ],
    },
    ensure_ascii=False,
)


# ---------- §4.1 公式纯函数（对齐 plan 算例） ----------

def test_confidence_formula_plan_examples() -> None:
    """官方实验室全信息 → 0.95（+同组 0.10 封顶 1.0）；致谢弱档 → 0.62。"""
    lab = PageExtraction(page_context="official_lab")  # src 1.0
    adv = Member(name="段海鑫", role="professor")
    stu = Member(name="张三", role="phd")  # 无 grad_year → time 0.5
    mentor = PairSignal("mentor_student", adv, stu, "")
    assert compute_confidence(lab, mentor) == 0.95
    assert compute_confidence(lab, mentor, bonus=0.10) == 1.0  # cap
    assert _strength(1.0, "mentor_student", 1) == 0.95  # plan 示例 strength

    weak = PageExtraction(page_context="unclear")  # src 0.6（致谢档）
    a = Member(name="李四", role="unknown")
    b = Member(name="王五", role="unknown")
    cohort = PairSignal("same_cohort", a, b, "")  # infer 0.7
    assert compute_confidence(weak, cohort) == 0.62  # 0.24+0.21+0.12+0.05
    assert _strength(0.9, "same_lab", 1) == 0.765  # plan 致谢 ≈0.77
    assert _strength(0.9, "same_cohort", 1) == 0.675


def test_evidence_boost() -> None:
    assert _strength(1.0, "mentor_student", 2) == 0.9975  # ×1.05
    assert _strength(1.0, "same_lab", 5) == 0.8925
    assert _strength(1.0, "same_advisor", 2) == 0.945


# ---------- 端到端：official_lab 全信息（plan 示例 0.95） ----------

async def test_official_lab_full_info(db_session) -> None:
    """强归并双端 identity 1.0 → mentor_student strength 0.95；same_lab 并存另一行。"""
    orgs = [("清华大学", "university"), ("网络研究院", "department"), ("NISL 实验室", "lab")]
    prof = await _mk_person(db_session, "段海鑫", orgs)
    stu = await _mk_person(db_session, "张三", orgs)
    page = await _mk_page(db_session)
    await db_session.commit()

    glm = GLMClient(transport=FakeTransport(LAB_JSON))
    report = await run_mentor_link(db_session, glm)

    assert (report.pages_extracted, report.pages_failed) == (1, 0)
    await db_session.refresh(page)
    assert page.status == "extracted"
    assert page.last_extracted_hash == page.content_hash

    rels = (
        await db_session.execute(
            select(Relationship).where(Relationship.type == "academic_mentorship")
        )
    ).scalars().all()
    by_subtype = {r.subtype: r for r in rels}
    assert set(by_subtype) == {"mentor_student", "same_lab"}  # 不同 subtype 并存两行

    mentor = by_subtype["mentor_student"]
    lo, hi = sorted((prof.id, stu.id))
    assert (mentor.person_a_id, mentor.person_b_id) == (lo, hi)
    assert float(mentor.identity_confidence) == 1.0  # 双端强归并
    assert float(mentor.strength) == pytest.approx(0.95)  # plan §4.1 算例
    assert mentor.coop_count == 1
    assert "指导 张三" in mentor.evidence_summary
    assert "置信度 1.00" in mentor.evidence_summary  # 0.95 + 同实验室 0.10 → cap

    lab_rel = by_subtype["same_lab"]
    assert float(lab_rel.strength) == pytest.approx(0.85)
    assert lab_rel.coop_count == 1

    ev_rows = (await db_session.execute(select(func.count()).select_from(RelationshipEvidencePage))).scalar_one()
    assert ev_rows == 2  # 每关系 1 条页面证据

    # 双跑幂等：页面已 extracted，二次运行不重做
    glm2 = GLMClient(transport=FakeTransport(LAB_JSON))
    report2 = await run_mentor_link(db_session, glm2)
    assert report2.pages_extracted == 0
    rels2 = (
        await db_session.execute(select(func.count()).select_from(Relationship))
    ).scalar_one()
    assert rels2 == len(rels)


async def test_relink_same_page_idempotent(db_session) -> None:
    """页面内容变化重抽、成员不变：(rel, page) 证据已存在 → 不重算不重复计。"""
    orgs = [("清华大学", "university"), ("网络研究院", "department"), ("NISL 实验室", "lab")]
    prof = await _mk_person(db_session, "段海鑫", orgs)
    stu = await _mk_person(db_session, "张三", orgs)
    page = await _mk_page(db_session)
    await db_session.commit()

    glm = GLMClient(transport=FakeTransport(LAB_JSON))
    await run_mentor_link(db_session, glm)

    ext = PageExtraction(
        lab_name="NISL 实验室", org_school="清华大学", org_department="网络研究院",
        page_context="official_lab",
        members=[
            Member(name="段海鑫", role="professor", person_id=prof.id, identity=1.0),
            Member(name="张三", role="phd", advisor="段海鑫", person_id=stu.id, identity=1.0),
        ],
    )
    stats = await link_page_relations(db_session, page, ext)
    assert stats == {"created": 0, "merged": 0, "dup": 2}

    mentor = (
        await db_session.execute(
            select(Relationship).where(Relationship.subtype == "mentor_student")
        )
    ).scalar_one()
    assert mentor.coop_count == 1  # 未重复计
    ev_rows = (
        await db_session.execute(select(func.count()).select_from(RelationshipEvidencePage))
    ).scalar_one()
    assert ev_rows == 2


async def test_new_evidence_merges_and_boosts(db_session) -> None:
    """第二个种子页新证据：coop_count=2、独立来源 2 → ×1.05 boost、时间范围并集。"""
    orgs = [("清华大学", "university"), ("NISL 实验室", "lab")]
    prof = await _mk_person(db_session, "段海鑫", orgs)
    stu = await _mk_person(db_session, "张三", orgs)
    page = await _mk_page(db_session)
    await db_session.commit()

    glm = GLMClient(transport=FakeTransport(LAB_JSON))
    await run_mentor_link(db_session, glm)

    # 另一种子（毕业生档案页）明示同一对师生，带毕业年份
    page2 = await _mk_page(
        db_session, url="https://netsec.ccert.edu.cn/chs/alumni/",
        seed_id="thu-nisl-archive", page_type="grad_list",
    )
    ext2 = PageExtraction(
        page_context="grad_list", org_school="清华大学",
        members=[
            Member(name="张三", role="alumni", advisor="段海鑫", grad_year=2026,
                   person_id=stu.id, identity=1.0),
        ],
    )
    # 张三 advisor 页外解析：段海鑫 强归并命中（NISL/清华 机构）
    stats = await link_page_relations(db_session, page2, ext2)
    assert stats["merged"] == 1

    mentor = (
        await db_session.execute(
            select(Relationship).where(Relationship.subtype == "mentor_student")
        )
    ).scalar_one()
    assert mentor.coop_count == 2
    assert float(mentor.strength) == pytest.approx(1.0, abs=0.005)  # 1.0×0.95×1.05 → 封顶档
    assert mentor.time_start == dt.date(2026, 1, 1)  # 年份并集（原无年份）
    assert mentor.time_end == dt.date(2026, 12, 31)
    assert "共 2 个独立来源" in mentor.evidence_summary


# ---------- grad_list / same_cohort ----------

async def test_grad_list_same_cohort(db_session) -> None:
    """同届建 same_cohort、跨届不建；新身份 0.9 → strength 0.675；年份入时间范围。"""
    await _mk_page(db_session)
    payload = json.dumps(
        {
            "page_context": "grad_list",
            "members": [
                {"name": "李甲", "role": "alumni", "grad_year": 2023},
                {"name": "王乙", "role": "alumni", "grad_year": 2023},
                {"name": "赵丙", "role": "alumni", "grad_year": 2022},
            ],
        },
        ensure_ascii=False,
    )
    await db_session.commit()
    glm = GLMClient(transport=FakeTransport(payload))
    report = await run_mentor_link(db_session, glm)

    assert report.pages_extracted == 1
    rels = (
        await db_session.execute(
            select(Relationship).where(Relationship.subtype == "same_cohort")
        )
    ).scalars().all()
    assert len(rels) == 1  # 只有 2023 届一对；赵丙 2022 无同届
    rel = rels[0]
    assert float(rel.identity_confidence) == 0.9  # 新建 Person
    assert 0.67 <= float(rel.strength) <= 0.68  # 0.9×0.75=0.675（numeric(3,2) 落 0.67/0.68）
    assert rel.time_start == dt.date(2023, 1, 1)
    assert rel.time_end == dt.date(2023, 12, 31)
    assert "2023 届同届" in rel.evidence_summary


async def test_advisor_unresolvable_not_built(db_session) -> None:
    """导师不在页内且强归并失败 → 不建 mentor_student（plan §3.1）。"""
    prof = await _mk_person(db_session, "段海鑫", [("NISL 实验室", "lab")])
    await _mk_page(db_session)
    payload = json.dumps(
        {
            "lab_name": "NISL 实验室", "page_context": "official_lab",
            "members": [
                {"name": "张三", "role": "phd", "advisor": "段海鑫"},
                {"name": "李四", "role": "phd", "advisor": "查无此师"},
            ],
        },
        ensure_ascii=False,
    )
    await db_session.commit()
    glm = GLMClient(transport=FakeTransport(payload))
    await run_mentor_link(db_session, glm)

    rels = (
        await db_session.execute(select(Relationship).where(Relationship.type == "academic_mentorship"))
    ).scalars().all()
    subtypes = {(r.subtype) for r in rels}
    mentor_rels = [r for r in rels if r.subtype == "mentor_student"]
    assert len(mentor_rels) == 1  # 仅张三→段海鑫（页外强归并）
    pair_ids = {mentor_rels[0].person_a_id, mentor_rels[0].person_b_id}
    assert prof.id in pair_ids and len(pair_ids) == 2
    assert "same_advisor" not in subtypes  # 查无此师无同门
    # 张三/李四 同页 official_lab（2 人 ≤30）→ same_lab 存在
    assert "same_lab" in subtypes


# ---------- 组合截断（OQ-3） ----------

async def test_large_lab_truncation(db_session) -> None:
    """35 人 official_lab：same_lab 不建；师生+同门分组保留；总对数 ≤400。"""
    prof = await _mk_person(db_session, "导师甲")
    students = []
    for i in range(PAIRWISE_CUTOFF + 4):  # 34 名学生 + 1 导师 = 35 人
        students.append(await _mk_person(db_session, f"学生{i:02d}"))
    page = await _mk_page(db_session)
    await db_session.commit()

    ext = PageExtraction(page_context="official_lab")
    ext.members.append(Member(name="导师甲", role="professor", person_id=prof.id, identity=1.0))
    for s in students:
        ext.members.append(
            Member(name=s.name, role="phd", advisor="导师甲", person_id=s.id, identity=1.0)
        )
    await link_page_relations(db_session, page, ext)
    await db_session.commit()

    counts = dict(
        (await db_session.execute(
            select(Relationship.subtype, func.count())
            .where(Relationship.type == "academic_mentorship")
            .group_by(Relationship.subtype)
        )).all()
    )
    assert counts.get("mentor_student") == 34      # 师生边全保留
    assert counts.get("same_lab", 0) == 0           # >30 截断
    assert counts.get("same_advisor") == 400 - 34   # C(34,2)=561 截到剩余额度
    total = (await db_session.execute(
        select(func.count()).select_from(Relationship)
    )).scalar_one()
    assert total <= MAX_PAIRS


# ---------- 失败路径 ----------

async def test_glm_failure_schedules_retry(db_session) -> None:
    """GLM 解析失败 → failed_jobs(page_extract) + extraction_failed；显式重跑可恢复。"""
    page = await _mk_page(db_session)
    await db_session.commit()

    glm_bad = GLMClient(transport=FakeTransport("{not json"))
    report = await run_mentor_link(db_session, glm_bad)
    assert (report.pages_failed, report.pages_extracted) == (1, 0)
    await db_session.refresh(page)
    assert page.status == "extraction_failed"
    job = (
        await db_session.execute(select(FailedJob).where(FailedJob.job_type == "page_extract"))
    ).scalar_one()
    assert job.target == page.url

    # 重试执行器路径：显式 page_ids 不限状态
    glm_good = GLMClient(transport=FakeTransport(LAB_JSON))
    report2 = await run_mentor_link(db_session, glm_good, page_ids=[page.id])
    assert report2.pages_extracted == 1
    await db_session.refresh(page)
    assert page.status == "extracted"


async def test_breaker_and_no_signal(db_session) -> None:
    """熔断跳过（页面留 pending、不写 failed_jobs）；空名单短路 no_signal。"""
    p1 = await _mk_page(db_session, url="https://netsec.ccert.edu.cn/chs/a/")
    p2 = await _mk_page(db_session, url="https://netsec.ccert.edu.cn/chs/b/")
    await db_session.commit()

    glm_breaker = GLMClient(transport=FakeTransport(BreakerOpenError("budget", "日预算耗尽")))
    report = await run_mentor_link(db_session, glm_breaker)
    assert report.breaker_skipped >= 1
    await db_session.refresh(p1)
    assert p1.status == "pending_extraction"  # 恢复后由调度器重扫
    jobs = (await db_session.execute(select(func.count()).select_from(FailedJob))).scalar_one()
    assert jobs == 0

    glm_empty = GLMClient(transport=FakeTransport('{"members": []}'))
    report2 = await run_mentor_link(db_session, glm_empty)
    assert report2.pages_no_signal == 2
    await db_session.refresh(p1)
    assert p1.status == "no_signal"


# ---------- T7 论文致谢信号（RD-M2-8） ----------

async def _mk_paper_with_authors(db_session, arxiv_id, authors, signals, published=None) -> Paper:
    """authors: list[(raw_name, person|None)]；person None 表示未消歧。"""
    paper = Paper(
        arxiv_id=arxiv_id, title=f"Paper {arxiv_id}", abstract="abs",
        authors_raw=[a for a, _ in authors], categories=["cs.AI"],
        status="extracted", mentorship_signals=signals,
        published_at=published or dt.datetime(2025, 6, 1, tzinfo=dt.timezone.utc),
    )
    db_session.add(paper)
    await db_session.flush()
    for seq, (raw, person) in enumerate(authors):
        db_session.add(PaperAuthor(paper_id=paper.id, author_seq=seq, raw_name=raw, person_id=person.id if person else None))
    await db_session.flush()
    return paper


async def test_acknowledgment_builds_mentor_student(db_session) -> None:
    """致谢明示 advisor/student（均为论文作者）→ mentor_student + 论文证据 + src 0.6。"""
    wang = await _mk_person(db_session, "王五")
    li = await _mk_person(db_session, "李教授")
    paper = await _mk_paper_with_authors(
        db_session, "2608.300", [("王五", wang), ("李教授", li)],
        signals=[{"advisor": "李教授", "student": "王五", "lab": None,
                  "hint": "感谢导师李教授的悉心指导"}],
    )
    await db_session.commit()

    stats = await link_paper_acknowledgments(db_session, paper)
    assert stats == {"created": 1, "merged": 0, "dup": 0}

    rel = (await db_session.execute(
        select(Relationship).where(Relationship.subtype == "mentor_student")
    )).scalar_one()
    assert {rel.person_a_id, rel.person_b_id} == {wang.id, li.id}
    assert float(rel.identity_confidence) == 1.0   # 作者精确匹配
    assert float(rel.strength) == pytest.approx(0.95)
    assert rel.coop_count == 1
    assert rel.time_start == dt.date(2025, 1, 1)   # 证据时间 = 发表年
    assert rel.time_end == dt.date(2025, 12, 31)
    assert "论文致谢" in rel.evidence_summary and "感谢导师 李教授" in rel.evidence_summary
    assert "置信度 0.76" in rel.evidence_summary  # 0.4×0.6+0.3×1.0+0.2×0.6+0.1×1.0
    ev = (await db_session.execute(
        select(RelationshipEvidence).where(RelationshipEvidence.paper_id == paper.id)
    )).scalar_one()
    assert ev.relationship_id == rel.id

    # 幂等：重放信号 → 证据已存在不重复计
    stats2 = await link_paper_acknowledgments(db_session, paper)
    assert stats2 == {"created": 0, "merged": 0, "dup": 1}
    await db_session.refresh(rel)
    assert rel.coop_count == 1


async def test_acknowledgment_pairing_rules(db_session) -> None:
    """泛致谢唯一作者可配对；多作者/指名配不上/自指 → 丢弃；lab 锚定新建导师 0.9。"""
    solo = await _mk_person(db_session, "独著甲")
    a1 = await _mk_person(db_session, "作者一")
    a2 = await _mk_person(db_session, "作者二")

    p_generic = await _mk_paper_with_authors(
        db_session, "2608.310", [("独著甲", solo)],
        signals=[{"advisor": "陈大导师", "student": None, "lab": "智能实验室", "hint": "感谢导师"}],
    )
    p_multi = await _mk_paper_with_authors(
        db_session, "2608.311", [("作者一", a1), ("作者二", a2)],
        signals=[{"advisor": "某导师", "student": None, "lab": None, "hint": None}],
    )
    p_unmatched = await _mk_paper_with_authors(
        db_session, "2608.312", [("作者一", a1), ("作者二", a2)],
        signals=[{"advisor": "神秘导师", "student": "不在作者列表的人", "lab": None, "hint": None}],
    )
    await db_session.commit()

    s1 = await link_paper_acknowledgments(db_session, p_generic)
    s2 = await link_paper_acknowledgments(db_session, p_multi)
    s3 = await link_paper_acknowledgments(db_session, p_unmatched)
    assert (s1["created"], s2["created"], s3["created"]) == (1, 0, 0)

    rel = (await db_session.execute(
        select(Relationship).where(Relationship.type == "academic_mentorship")
    )).scalar_one()
    advisor_ids = {rel.person_a_id, rel.person_b_id} - {solo.id}
    assert len(advisor_ids) == 1
    advisor = await db_session.get(Person, advisor_ids.pop())
    assert advisor.name == "陈大导师"
    assert float(rel.identity_confidence) == 0.9  # 新建导师端取弱档
    assert 0.85 <= float(rel.strength) <= 0.86    # 0.9×0.95=0.855
    orgs = (await db_session.execute(select(PersonOrg).where(PersonOrg.person_id == advisor.id))).scalars().all()
    assert len(orgs) == 1  # lab 机构锚定


async def test_run_mentor_link_covers_ack_papers(db_session) -> None:
    """阶段入口重扫带信号的已抽取论文；未消歧作者本轮跳过、就绪后收敛。"""
    stu = await _mk_person(db_session, "王五")
    li = await _mk_person(db_session, "李教授")
    paper = await _mk_paper_with_authors(
        db_session, "2608.320", [("王五", stu), ("李教授", None)],  # 李教授作者位未消歧
        signals=[{"advisor": "李教授", "student": "王五", "lab": None, "hint": None}],
    )
    await db_session.commit()

    glm = GLMClient(transport=FakeTransport("{}"))  # 无 pending 页面
    report1 = await run_mentor_link(db_session, glm)
    assert report1.pairs_created == 0  # advisor 作者位无 person_id → 信号跳过
    rels = (await db_session.execute(select(Relationship))).scalars().all()
    assert rels == []

    # 消歧完成后（作者位挂上 person）：下轮重扫自然建链
    pa_li = (await db_session.execute(
        select(PaperAuthor).where(PaperAuthor.paper_id == paper.id, PaperAuthor.raw_name == "李教授")
    )).scalar_one()
    pa_li.person_id = li.id
    await db_session.commit()
    report2 = await run_mentor_link(db_session, glm)
    assert report2.pairs_created == 1
    rels2 = (await db_session.execute(select(Relationship))).scalars().all()
    assert len(rels2) == 1
