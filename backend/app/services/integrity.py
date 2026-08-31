"""数据不变量防护网（M1 做实 2026-08-26；M2-T2 扩展 C7-C10）。

linker 膨胀事故（2026-08-26，修复 1d46d1f）的教训：管线每轮全量重跑，
任何非幂等写入都会让数据悄悄变脏且界面上看不出来。本模块集中声明
"干净数据"的可验证不变量，只读不改，供三处调用：

- 凌晨管线跑完后自动巡检（app/scheduler.py，违例记 WARNING 日志）
- 后台可视化 GET /api/admin/integrity（app/api/admin.py）
- pytest 回归（tests/test_integrity.py，合并/幂等测试共用）

不变量清单（违例即数据脏，需人工或脚本修复）：
  C1 关系计数 == 证据行数（三表合计：论文/网页/资讯），且证据 ≥1
     —— coop_count 的事实来源是证据表（M2 起分类型证据表合计）
  C2 strength / identity_confidence ∈ [0, 1]
  C3 无自环关系（person_a_id != person_b_id）
  C4 同类型同子类型关系无重复人对（(type, subtype, lo, hi) 唯一；M2 起含 subtype）
  C5 证据论文均为已抽取（extracted）且在 CN 范围内（has_cn_scholar）
  C6 活关系两端均非墓碑（merged_into_id IS NULL 且 deleted_at IS NULL）
  （M2.5 起关系自身可墓碑：C1-C4/C7-C9 仅巡检未删行，墓碑行不复活即合规）
  C7 关系唯一性含 subtype：(a,b,type,subtype) 无重复、无 a>=b 反向行
     （唯一键 uq_rel_pair_type_subtype + ck_rel_a_lt_b 之外的巡检兜底）
  C8 新类型证据非空：academic_mentorship ≥1 条 pages/paper 证据；
     project_cooperation ≥1 条 news 证据且 projects 表非空
  C9 新关系值域：confidence/strength ∈ [0,1]，subtype 与 type 匹配
     （传承四子类型；论文/项目合作 subtype=''）
  C10 证据幂等：pages/news 证据表无重复主键
  C11 潜在关系不变量：a<b、method 枚举、confidence 界内、两端活跃人、
     两端无活跃直接关系（M3 FR-3.4；a<b/confidence 另有 DB CHECK 兜底，
     巡检防约束被旁路/误删——与 C7 同定位）
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Paper,
    Person,
    PotentialRelationship,
    Project,
    Relationship,
    RelationshipEvidence,
    RelationshipEvidenceNews,
    RelationshipEvidencePage,
)

SAMPLE_LIMIT = 5  # 每项检查最多展示的违例样本数

MENTORSHIP_SUBTYPES = ("mentor_student", "same_lab", "same_advisor", "same_cohort")
POTENTIAL_METHODS = ("common_network", "research_similarity")


@dataclass(frozen=True)
class CheckResult:
    check: str  # 编号 + 名称，如 "C1 合作数与证据一致"
    violations: int
    sample: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.violations == 0


def _fmt(rows, template) -> list[str]:
    return [template.format(*r[: template.count("{")]) for r in rows[:SAMPLE_LIMIT]]


async def check_integrity(session: AsyncSession) -> dict:
    """跑全部不变量检查，返回 {ok, checked_at, checks:[...]}，只读不写。"""
    checks = [
        await _c1_coop_matches_evidence(session),
        await _c2_score_bounds(session),
        await _c3_no_self_loops(session),
        await _c4_no_duplicate_pairs(session),
        await _c5_evidence_paper_scope(session),
        await _c6_no_tombstone_refs(session),
        await _c7_uniqueness_with_subtype(session),
        await _c8_new_type_evidence_present(session),
        await _c9_new_type_value_domain(session),
        await _c10_evidence_tables_no_dup(session),
        await _c11_potential_invariants(session),
    ]
    return {
        "ok": all(c.ok for c in checks),
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checks": [
            {"check": c.check, "violations": c.violations, "sample": c.sample}
            for c in checks
        ],
    }


def _evidence_count_subquery(model, col) -> tuple:
    """按 relationship_id 聚合的证据计数子表及其可空计数列。"""
    sub = (
        select(model.relationship_id, func.count().label("n"))
        .group_by(model.relationship_id)
        .subquery()
    )
    return sub, func.coalesce(sub.c.n, 0).label(col)


async def _c1_coop_matches_evidence(session: AsyncSession) -> CheckResult:
    paper_sub, paper_n = _evidence_count_subquery(RelationshipEvidence, "paper_n")
    page_sub, page_n = _evidence_count_subquery(RelationshipEvidencePage, "page_n")
    news_sub, news_n = _evidence_count_subquery(RelationshipEvidenceNews, "news_n")
    total = (paper_n + page_n + news_n).label("total")
    rows = (
        await session.execute(
            select(Relationship.id, Relationship.coop_count, total)
            .where(Relationship.deleted_at.is_(None))  # 墓碑行不巡检（计数已冻结）
            .outerjoin(paper_sub, paper_sub.c.relationship_id == Relationship.id)
            .outerjoin(page_sub, page_sub.c.relationship_id == Relationship.id)
            .outerjoin(news_sub, news_sub.c.relationship_id == Relationship.id)
        )
    ).all()
    bad = [(r.id, r.coop_count, r.total) for r in rows if r.coop_count != r.total or r.total == 0]
    return CheckResult(
        "C1 关系计数与证据一致",
        len(bad),
        [f"关系 {r[0]}: coop_count={r[1]}, 证据={r[2]} 行" for r in bad[:SAMPLE_LIMIT]],
    )


async def _c2_score_bounds(session: AsyncSession) -> CheckResult:
    rows = (
        await session.execute(
            select(Relationship.id, Relationship.strength, Relationship.identity_confidence).where(
                Relationship.deleted_at.is_(None),
                or_(
                    Relationship.strength < 0,
                    Relationship.strength > 1,
                    Relationship.identity_confidence < 0,
                    Relationship.identity_confidence > 1,
                )
            )
        )
    ).all()
    return CheckResult(
        "C2 强度/身份置信度在 [0,1]",
        len(rows),
        _fmt(rows, "关系 {}: strength={}, identity={}"),
    )


async def _c3_no_self_loops(session: AsyncSession) -> CheckResult:
    rows = (
        await session.execute(
            select(Relationship.id).where(
                Relationship.deleted_at.is_(None),
                Relationship.person_a_id == Relationship.person_b_id,
            )
        )
    ).all()
    return CheckResult("C3 无自环关系", len(rows), [f"关系 {r[0]}: 两端同人" for r in rows[:SAMPLE_LIMIT]])


async def _c4_no_duplicate_pairs(session: AsyncSession) -> CheckResult:
    lo = func.least(Relationship.person_a_id, Relationship.person_b_id)
    hi = func.greatest(Relationship.person_a_id, Relationship.person_b_id)
    rows = (
        await session.execute(
            select(lo, hi, Relationship.type, Relationship.subtype, func.count())
            .where(Relationship.deleted_at.is_(None))
            .group_by(lo, hi, Relationship.type, Relationship.subtype)
            .having(func.count() > 1)
        )
    ).all()
    return CheckResult(
        "C4 同类型同子类型无重复人对",
        len(rows),
        _fmt(rows, "人对 ({}, {}) 类型 {} 子类型 {} 有 {} 条"),
    )


async def _c5_evidence_paper_scope(session: AsyncSession) -> CheckResult:
    rows = (
        await session.execute(
            select(RelationshipEvidence.relationship_id, RelationshipEvidence.paper_id)
            .join(Paper, Paper.id == RelationshipEvidence.paper_id)
            .where(or_(Paper.status != "extracted", Paper.has_cn_scholar.is_(False)))
        )
    ).all()
    return CheckResult(
        "C5 证据论文已抽取且在 CN 范围",
        len(rows),
        _fmt(rows, "关系 {} 的证据论文 {} 越界"),
    )


async def _c6_no_tombstone_refs(session: AsyncSession) -> CheckResult:
    """仅活关系不得引用消歧/合规墓碑；已墓碑的关系本身即死亡状态，不重复计违例。"""
    tomb = select(Person.id).where(
        or_(Person.merged_into_id.is_not(None), Person.deleted_at.is_not(None))
    )
    rows = (
        await session.execute(
            select(Relationship.id, Relationship.person_a_id, Relationship.person_b_id).where(
                Relationship.deleted_at.is_(None),
                or_(Relationship.person_a_id.in_(tomb), Relationship.person_b_id.in_(tomb)),
            )
        )
    ).all()
    return CheckResult(
        "C6 关系不引用消歧墓碑",
        len(rows),
        _fmt(rows, "关系 {} 引用了已合并学者 ({}, {})"),
    )


async def _c7_uniqueness_with_subtype(session: AsyncSession) -> CheckResult:
    """唯一键 uq(a,b,type,subtype) + CHECK a<b 的巡检兜底（防约束被旁路/误删）。"""
    dup_rows = (
        await session.execute(
            select(
                Relationship.person_a_id,
                Relationship.person_b_id,
                Relationship.type,
                Relationship.subtype,
                func.count(),
            )
            .where(Relationship.deleted_at.is_(None))
            .group_by(
                Relationship.person_a_id,
                Relationship.person_b_id,
                Relationship.type,
                Relationship.subtype,
            )
            .having(func.count() > 1)
        )
    ).all()
    reversed_rows = (
        await session.execute(
            select(Relationship.id, Relationship.person_a_id, Relationship.person_b_id).where(
                Relationship.deleted_at.is_(None),
                Relationship.person_a_id >= Relationship.person_b_id,
            )
        )
    ).all()
    samples = [f"人对 ({r[0]}, {r[1]}) 类型 {r[2]} 子类型 {r[3]!r} 有 {r[4]} 条" for r in dup_rows[:SAMPLE_LIMIT]]
    samples += [f"关系 {r[0]}: a={r[1]} >= b={r[2]}（违反 ck_rel_a_lt_b）" for r in reversed_rows[:SAMPLE_LIMIT]]
    return CheckResult("C7 关系唯一性含 subtype", len(dup_rows) + len(reversed_rows), samples)


async def _c8_new_type_evidence_present(session: AsyncSession) -> CheckResult:
    mentor_no_ev = (
        await session.execute(
            select(Relationship.id, Relationship.subtype).where(
                Relationship.deleted_at.is_(None),
                Relationship.type == "academic_mentorship",
                ~exists(
                    select(1).where(
                        RelationshipEvidencePage.relationship_id == Relationship.id
                    )
                ),
                ~exists(
                    select(1).where(RelationshipEvidence.relationship_id == Relationship.id)
                ),
            )
        )
    ).all()
    project_no_ev = (
        await session.execute(
            select(Relationship.id).where(
                Relationship.deleted_at.is_(None),
                Relationship.type == "project_cooperation",
                ~exists(
                    select(1).where(
                        RelationshipEvidenceNews.relationship_id == Relationship.id
                    )
                ),
            )
        )
    ).all()
    samples = [
        f"传承关系 {r[0]}（子类型 {r[1]!r}）无 pages/paper 证据" for r in mentor_no_ev[:SAMPLE_LIMIT]
    ]
    samples += [f"项目合作关系 {r[0]} 无 news 证据" for r in project_no_ev[:SAMPLE_LIMIT]]
    # "关联 project 存在"：项目关系只能来自资讯抽取的 participations，
    # 有项目关系而无任何 project 实体 = 链路写入了无锚点的证据
    n_proj_rel = (
        await session.execute(
            select(func.count()).select_from(Relationship).where(
                Relationship.deleted_at.is_(None),
                Relationship.type == "project_cooperation",
            )
        )
    ).scalar_one()
    n_projects = (await session.execute(select(func.count()).select_from(Project))).scalar_one()
    if n_proj_rel > 0 and n_projects == 0:
        samples.append(f"存在 {n_proj_rel} 条项目合作关系但 projects 表为空")
    violations = len(mentor_no_ev) + len(project_no_ev) + (1 if (n_proj_rel > 0 and n_projects == 0) else 0)
    return CheckResult("C8 新类型证据非空", violations, samples[:SAMPLE_LIMIT])


async def _c9_new_type_value_domain(session: AsyncSession) -> CheckResult:
    rows = (
        await session.execute(
        select(Relationship.id, Relationship.type, Relationship.subtype, Relationship.strength)
        .where(
            Relationship.deleted_at.is_(None),
            or_(
                    and_(
                        Relationship.type == "academic_mentorship",
                        Relationship.subtype.not_in(MENTORSHIP_SUBTYPES),
                    ),
                    and_(
                        Relationship.type != "academic_mentorship",
                        Relationship.subtype != "",
                    ),
                    and_(
                        Relationship.type.in_(("academic_mentorship", "project_cooperation")),
                        or_(
                            Relationship.strength < 0,
                            Relationship.strength > 1,
                            Relationship.identity_confidence < 0,
                            Relationship.identity_confidence > 1,
                        ),
                    ),
                )
            )
        )
    ).all()
    return CheckResult(
        "C9 新关系值域（subtype 枚举 + 分值界）",
        len(rows),
        _fmt(rows, "关系 {}: type={} subtype={!r} strength={}"),
    )


async def _c10_evidence_tables_no_dup(session: AsyncSession) -> CheckResult:
    page_dups = (
        await session.execute(
            select(
                RelationshipEvidencePage.relationship_id,
                RelationshipEvidencePage.web_page_id,
                func.count(),
            )
            .group_by(
                RelationshipEvidencePage.relationship_id,
                RelationshipEvidencePage.web_page_id,
            )
            .having(func.count() > 1)
        )
    ).all()
    news_dups = (
        await session.execute(
            select(
                RelationshipEvidenceNews.relationship_id,
                RelationshipEvidenceNews.news_item_id,
                func.count(),
            )
            .group_by(
                RelationshipEvidenceNews.relationship_id,
                RelationshipEvidenceNews.news_item_id,
            )
            .having(func.count() > 1)
        )
    ).all()
    samples = [f"pages 证据 ({r[0]}, {r[1]}) 有 {r[2]} 行" for r in page_dups[:SAMPLE_LIMIT]]
    samples += [f"news 证据 ({r[0]}, {r[1]}) 有 {r[2]} 行" for r in news_dups[:SAMPLE_LIMIT]]
    return CheckResult("C10 证据表无重复主键", len(page_dups) + len(news_dups), samples[:SAMPLE_LIMIT])


async def _c11_potential_invariants(session: AsyncSession) -> CheckResult:
    """M3 FR-3.4：潜在关系五不变量（a<b/method/confidence/两端活跃/无活跃直接关系）。"""
    domain_rows = (
        await session.execute(
            select(
                PotentialRelationship.id,
                PotentialRelationship.person_a_id,
                PotentialRelationship.person_b_id,
                PotentialRelationship.discovery_method,
                PotentialRelationship.confidence,
            ).where(
                or_(
                    PotentialRelationship.person_a_id >= PotentialRelationship.person_b_id,
                    PotentialRelationship.discovery_method.not_in(POTENTIAL_METHODS),
                    ~PotentialRelationship.confidence.between(0.10, 0.70),
                )
            )
        )
    ).all()
    tomb = select(Person.id).where(
        or_(Person.merged_into_id.is_not(None), Person.deleted_at.is_not(None))
    )
    tomb_rows = (
        await session.execute(
            select(
                PotentialRelationship.id,
                PotentialRelationship.person_a_id,
                PotentialRelationship.person_b_id,
            ).where(
                or_(
                    PotentialRelationship.person_a_id.in_(tomb),
                    PotentialRelationship.person_b_id.in_(tomb),
                )
            )
        )
    ).all()
    direct_rows = (
        await session.execute(
            select(PotentialRelationship.id, PotentialRelationship.person_a_id, PotentialRelationship.person_b_id).where(
                exists().where(
                    and_(
                        Relationship.person_a_id == PotentialRelationship.person_a_id,
                        Relationship.person_b_id == PotentialRelationship.person_b_id,
                        Relationship.deleted_at.is_(None),
                    )
                )
            )
        )
    ).all()
    samples = [
        f"潜在关系 {r[0]}: a={r[1]} b={r[2]} method={r[3]} conf={r[4]}（域违例）"
        for r in domain_rows[:SAMPLE_LIMIT]
    ]
    samples += [f"潜在关系 {r[0]}: 引用墓碑端点 ({r[1]}, {r[2]})" for r in tomb_rows[:SAMPLE_LIMIT]]
    samples += [
        f"潜在关系 {r[0]}: 两端已有活跃直接关系 ({r[1]}, {r[2]})（复活对）"
        for r in direct_rows[:SAMPLE_LIMIT]
    ]
    return CheckResult(
        "C11 潜在关系不变量",
        len(domain_rows) + len(tomb_rows) + len(direct_rows),
        samples[:SAMPLE_LIMIT],
    )
