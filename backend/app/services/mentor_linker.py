"""学术传承关系建立（M2-T6，FR-5.1~5.6，plan §3.1 推导规则 + §4.1 公式）。

子类型推导（代码完成，不让 GLM 配对）：
- mentor_student：成员 advisor 明示（导师在页内匹配，或按机构链强归并到
  既有 Person；消歧失败则不建——plan §3.1）
- same_advisor：同导师的不同成员（与 same_lab 可并存，subtype 不同行）
- same_lab：同页成员且 page_context=official_lab 两两配对（>30 人截断不做
  全配对，只保留师生/同门/同届分组边，plan OQ-3）
- same_cohort：grad_list 页同 grad_year
- 单页推导总对数上限 400（防组合爆炸）

confidence = 0.4×src + 0.3×infer + 0.2×clarity + 0.1×time + same_org_bonus（cap 1.0）
identity   = min(两端成员 identity)：强归并 1.0 / 打分取分 / 新建 0.9（RD-M2-12）
strength   = identity × subtype_base(0.95/0.90/0.85/0.75) × evidence_boost(≥2 独立来源 ×1.05 cap 1.0)

confidence 无存储列（plan §2 DDL 未加列）：计算值落入 evidence_summary 文本
供审计展示；落库数值仍为 identity_confidence / strength（C9 检查域）。

证据幂等（§4.3，M1 linker 教训）：(relationship, page) 证据主键已存在 →
不重算不重复计；新增证据 → coop_count=证据行数、strength 按公式重查
（evidence_boost 数独立来源：web_pages 种子数 + 论文数）、时间范围取并集。
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Organization,
    PersonOrg,
    Relationship,
    RelationshipEvidence,
    RelationshipEvidencePage,
    WebPage,
)
from app.services.breaker import BreakerOpenError
from app.services.disambiguator import strong_merge_match
from app.services.failed_jobs import schedule_retry
from app.services.glm import GLMClient, GLMError, GLMParseError, GLMTransientError
from app.services.page_extractor import (
    CLARITY_KNOWN,
    CLARITY_UNKNOWN,
    Member,
    PageExtraction,
    TIME_NO_YEAR,
    TIME_WITH_YEAR,
    extract_page,
    persist_page_result,
)
from app.utils.names import normalize_person_name

log = logging.getLogger("prof-graph.mentor_linker")

REL_TYPE = "academic_mentorship"
W_SRC, W_INFER, W_CLARITY, W_TIME = 0.4, 0.3, 0.2, 0.1

# 推断确定性分档（推导规则 → 分档，plan §3.1 表）
INFER_BY_SUBTYPE = {"mentor_student": 1.0, "same_advisor": 0.9, "same_lab": 0.8, "same_cohort": 0.7}
# 子类型基准强度（plan §4.1）
SUBTYPE_BASE = {"mentor_student": 0.95, "same_advisor": 0.90, "same_lab": 0.85, "same_cohort": 0.75}
# 同机构加分（RD 粒度：组/系/院/校）
BONUS_LAB, BONUS_DEPT_XI, BONUS_DEPT, BONUS_UNIV = 0.10, 0.05, 0.03, 0.01

PAIRWISE_CUTOFF = 30  # 同页成员超过此数不做 same_lab 全配对（OQ-3）
MAX_PAIRS = 400       # 单页推导总对数上限

SOURCE_DESC = {
    "official_lab": "实验室官网成员页",
    "department_site": "院系官网页面",
    "grad_list": "毕业生名单页",
    "unclear": "高校网页",
}


@dataclass
class PairSignal:
    subtype: str
    a: Member
    b: Member
    label: str  # evidence_summary 的关系描述片段


@dataclass
class MentorLinkReport:
    pages_extracted: int = 0
    pages_no_signal: int = 0
    pages_failed: int = 0
    breaker_skipped: int = 0
    pairs_created: int = 0
    pairs_merged: int = 0   # 新证据并入既有关系
    pairs_dup: int = 0      # 证据已存在（幂等跳过）


def _pair_clarity(a: Member, b: Member) -> float:
    return CLARITY_KNOWN if (a.role != "unknown" and b.role != "unknown") else CLARITY_UNKNOWN


def _pair_time(a: Member, b: Member) -> float:
    return TIME_WITH_YEAR if (a.grad_year or b.grad_year) else TIME_NO_YEAR


def compute_confidence(ext: PageExtraction, signal: PairSignal, bonus: float = 0.0) -> float:
    """plan §4.1 公式（纯函数，单测对齐算例 0.95 / 0.62）。"""
    value = (
        W_SRC * ext.src_confidence
        + W_INFER * INFER_BY_SUBTYPE[signal.subtype]
        + W_CLARITY * _pair_clarity(signal.a, signal.b)
        + W_TIME * _pair_time(signal.a, signal.b)
        + bonus
    )
    return round(min(1.0, value), 4)


async def _same_org_bonus(session: AsyncSession, id_a: int, id_b: int) -> tuple[float, str | None]:
    """两端共享机构的最细粒度 → (加分, 描述)。系/院按机构名是否含"系"区分。"""

    async def orgs_of(pid: int) -> dict[int, tuple[str, str]]:
        rows = (
            await session.execute(
                select(Organization.id, Organization.level, Organization.name)
                .join(PersonOrg, PersonOrg.org_id == Organization.id)
                .where(PersonOrg.person_id == pid)
            )
        ).all()
        return {r[0]: ((r[1] or ""), (r[2] or "")) for r in rows}

    orgs_a = await orgs_of(id_a)
    orgs_b = await orgs_of(id_b)
    shared = set(orgs_a) & set(orgs_b)
    if not shared:
        return 0.0, None
    levels = {orgs_a[oid][0]: orgs_a[oid][1] for oid in shared}
    if "lab" in levels:
        return BONUS_LAB, "同实验室"
    if "department" in levels:
        if "系" in levels["department"]:
            return BONUS_DEPT_XI, "同系"
        return BONUS_DEPT, "同院"
    if "university" in levels:
        return BONUS_UNIV, "同校"
    return 0.0, None


async def _resolve_advisor(session: AsyncSession, ext: PageExtraction, name: str) -> Member | None:
    """导师解析：先页内按姓名归一匹配；页外按机构链强归并（消歧失败不建关系）。"""
    norm = normalize_person_name(name)
    for m in ext.members:
        if normalize_person_name(m.name) == norm and m.person_id:
            return m
    for org_str in (ext.lab_name, ext.org_department, ext.org_school):
        if not org_str:
            continue
        p = await strong_merge_match(session, name, org_str)
        if p is not None:
            return Member(name=name, person_id=p.id, identity=1.0, role="unknown")
    return None


async def derive_pairs(session: AsyncSession, ext: PageExtraction) -> list[PairSignal]:
    """按 plan §3.1 规则推导关系信号（推导在代码，不让 GLM 配对）。"""
    pairs: list[PairSignal] = []
    members = [m for m in ext.members if m.person_id]

    # 1. advisor 明示 → mentor_student
    advisor_of: dict[int, Member] = {}
    for m in members:
        if not m.advisor:
            continue
        adv = await _resolve_advisor(session, ext, m.advisor)
        if adv is None or adv.person_id == m.person_id:
            continue
        advisor_of[m.person_id] = adv
        pairs.append(PairSignal("mentor_student", adv, m, f"{adv.name} 指导 {m.name}（页面明示导师）"))

    # 2. 同导师不同成员 → same_advisor（同门分组）
    by_advisor: dict[str, list[Member]] = {}
    for m in members:
        adv = advisor_of.get(m.person_id)
        if adv is not None:
            by_advisor.setdefault(normalize_person_name(adv.name), []).append(m)
    for group in by_advisor.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                pairs.append(
                    PairSignal("same_advisor", group[i], group[j], f"同门（同导师 {group[i].advisor}）")
                )

    # 3. 同页 official_lab → same_lab 两两（>30 人截断，OQ-3）
    if ext.page_context == "official_lab" and len(members) <= PAIRWISE_CUTOFF:
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if members[j].person_id == members[i].person_id:
                    continue
                pairs.append(PairSignal("same_lab", members[i], members[j], "同实验室成员"))

    # 4. grad_list 页同 grad_year → same_cohort
    if ext.page_context == "grad_list":
        by_year: dict[int, list[Member]] = {}
        for m in members:
            if m.grad_year:
                by_year.setdefault(m.grad_year, []).append(m)
        for year, group in by_year.items():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    pairs.append(PairSignal("same_cohort", group[i], group[j], f"{year} 届同届"))

    if len(pairs) > MAX_PAIRS:
        log.warning(
            "页面组合超过 %d 对上限，截断（members=%d, page_context=%s）",
            MAX_PAIRS, len(members), ext.page_context,
        )
        pairs = pairs[:MAX_PAIRS]
    return pairs


def _year_range(a: Member, b: Member) -> tuple[dt.date | None, dt.date | None]:
    years = [m.grad_year for m in (a, b) if m.grad_year]
    if not years:
        return None, None
    return dt.date(min(years), 1, 1), dt.date(max(years), 12, 31)


async def _source_count(session: AsyncSession, rel_id: int) -> int:
    """独立来源数：页面证据按 seed 去重 + 论文证据数（§4.3 evidence_boost 用）。"""
    seeds = (
        await session.execute(
            select(func.distinct(WebPage.seed_id))
            .join(RelationshipEvidencePage, RelationshipEvidencePage.web_page_id == WebPage.id)
            .where(RelationshipEvidencePage.relationship_id == rel_id)
        )
    ).scalars().all()
    papers = (
        await session.execute(
            select(func.count())
            .select_from(RelationshipEvidence)
            .where(RelationshipEvidence.relationship_id == rel_id)
        )
    ).scalar_one()
    return len(set(seeds)) + papers


def _strength(identity: float, subtype: str, sources: int) -> float:
    boost = 1.05 if sources >= 2 else 1.0
    return round(min(1.0, identity * SUBTYPE_BASE[subtype] * boost), 4)


def _summary(
    ext: PageExtraction, signal: PairSignal, confidence: float, sources: int, bonus_desc: str | None
) -> str:
    parts = [
        f"基于{SOURCE_DESC.get(ext.page_context, '高校网页')}：{signal.label}",
        f"置信度 {confidence:.2f}",
    ]
    if sources > 1:
        parts.append(f"共 {sources} 个独立来源")
    if bonus_desc:
        parts.append(bonus_desc)
    return "；".join(parts)


async def link_pair(
    session: AsyncSession, page: WebPage, ext: PageExtraction, signal: PairSignal
) -> str:
    """建立/合并一对传承关系。返回 created / merged / dup（dup=证据已存在）。"""
    a, b = signal.a, signal.b
    lo, hi = sorted((a.person_id, b.person_id))
    if lo == hi:
        return "dup"

    rel = (
        await session.execute(
            select(Relationship).where(
                Relationship.person_a_id == lo,
                Relationship.person_b_id == hi,
                Relationship.type == REL_TYPE,
                Relationship.subtype == signal.subtype,
            )
        )
    ).scalar_one_or_none()
    if rel is not None:
        ev_exists = (
            await session.execute(
                select(RelationshipEvidencePage).where(
                    RelationshipEvidencePage.relationship_id == rel.id,
                    RelationshipEvidencePage.web_page_id == page.id,
                )
            )
        ).scalar_one_or_none() is not None
        if ev_exists:
            return "dup"  # 证据幂等：既有 (rel, page) 不重算不重复计
    is_new = rel is None

    bonus, bonus_desc = await _same_org_bonus(session, lo, hi)
    confidence = compute_confidence(ext, signal, bonus)
    identity = round(min(a.identity, b.identity), 4)

    if is_new:
        rel = Relationship(
            person_a_id=lo,
            person_b_id=hi,
            type=REL_TYPE,
            subtype=signal.subtype,
            identity_confidence=identity,
            strength=identity,  # 占位，下方按公式重算
            coop_count=0,
        )
        session.add(rel)
        await session.flush()

    session.add(RelationshipEvidencePage(relationship_id=rel.id, web_page_id=page.id))
    await session.flush()

    # coop_count 的事实来源是证据表（C1：页面 + 论文证据行数）
    pages_n = (
        await session.execute(
            select(func.count())
            .select_from(RelationshipEvidencePage)
            .where(RelationshipEvidencePage.relationship_id == rel.id)
        )
    ).scalar_one()
    papers_n = (
        await session.execute(
            select(func.count())
            .select_from(RelationshipEvidence)
            .where(RelationshipEvidence.relationship_id == rel.id)
        )
    ).scalar_one()
    rel.coop_count = pages_n + papers_n

    sources = await _source_count(session, rel.id)
    # identity 历史最好：新证据端身份更确定时不降
    rel.identity_confidence = round(max(float(rel.identity_confidence), identity), 4)
    rel.strength = _strength(float(rel.identity_confidence), signal.subtype, sources)
    rel.evidence_summary = _summary(ext, signal, confidence, sources, bonus_desc)

    ts, te = _year_range(a, b)
    if ts and (rel.time_start is None or ts < rel.time_start):
        rel.time_start = ts
    if te and (rel.time_end is None or te > rel.time_end):
        rel.time_end = te
    return "created" if is_new else "merged"


async def link_page_relations(
    session: AsyncSession, page: WebPage, ext: PageExtraction
) -> dict[str, int]:
    """单页关系建立。调用前成员已消歧入库（persist_page_result）。"""
    stats = {"created": 0, "merged": 0, "dup": 0}
    for signal in await derive_pairs(session, ext):
        outcome = await link_pair(session, page, ext, signal)
        stats[outcome] += 1
    await session.flush()
    return stats


async def run_mentor_link(
    session: AsyncSession, glm: GLMClient, page_ids: list[int] | None = None
) -> MentorLinkReport:
    """mentor_link 阶段入口（T8 管线接入）：抽取 → 入库 → 建链 → 置状态。

    默认处理 pending_extraction 的非 news 页面（news 页走资讯链路 T10）；
    显式 page_ids（重试执行器）不限状态，允许重跑 extraction_failed。
    抽取/入库/建链成功后才置 extracted（崩溃时不丢重做信号）。
    """
    report = MentorLinkReport()
    stmt = select(WebPage).where(WebPage.page_type != "news")
    if page_ids is None:
        stmt = stmt.where(WebPage.status == "pending_extraction")
    else:
        stmt = stmt.where(WebPage.id.in_(page_ids))
    pages = (await session.execute(stmt)).scalars().all()

    for page in pages:
        try:
            ext = await extract_page(session, glm, page)
        except BreakerOpenError:
            # 熔断：跳过（不写 failed_jobs，恢复后由调度器重扫）
            report.breaker_skipped += 1
            break
        except (GLMTransientError, GLMParseError, GLMError, ValueError) as e:
            await schedule_retry(session, "page_extract", page.url, f"{type(e).__name__}: {e}")
            page.status = "extraction_failed"
            report.pages_failed += 1
            await session.commit()
            continue
        if not ext.members:
            page.status = "no_signal"
            report.pages_no_signal += 1
            await session.commit()
            continue
        await persist_page_result(session, page, ext)
        stats = await link_page_relations(session, page, ext)
        report.pairs_created += stats["created"]
        report.pairs_merged += stats["merged"]
        report.pairs_dup += stats["dup"]
        page.status = "extracted"
        page.last_extracted_hash = page.content_hash
        report.pages_extracted += 1
        await session.commit()  # 逐页提交：批次中断不丢已完成进度
    return report
