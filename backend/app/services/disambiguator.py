"""实体消歧器（T11，FR-3.1~3.3，准确度优先）。

流程（plan §6）：
1. openalex_id 强匹配 → 直归并
2. 候选集（全量扫描，不考虑性能）：name_normalized 精确同名 + 模糊变体
   （编辑距离 ≤2、姓名序颠倒双向）
3. 5 因素加权：姓名 30% / 机构 25% / 研究方向 20% Jaccard /
   时间 15%（活跃区间自动聚合）/ 合作网络 10%
4. ≥0.8 自动归并；0.5–0.8 入 disambiguation_queue（含分项得分）；
   <0.5 新建 Person；paper_authors 关联 person_id
5. reject 语义：已判定 A≠B 的组合不再重复入队（uq_disamb_pair 兜底）；
   新相似作者仍与 A、B 分别正常匹配
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DisambiguationQueue,
    Organization,
    Paper,
    PaperAuthor,
    Person,
    PersonOrg,
    PersonResearchTag,
)
from app.services.openalex import normalize_org, sync_person_org
from app.utils.names import (
    levenshtein,
    normalize_person_name,
    swap_name_order,
)

log = logging.getLogger("prof-graph.disambiguator")

FUZZY_MAX_DIST = 2          # 模糊候选的编辑距离阈值
AUTO_MERGE_THRESHOLD = 0.8  # ≥ 自动归并
QUEUE_THRESHOLD = 0.5       # ≥ 入审核队列，< 新建

W_NAME, W_ORG, W_RESEARCH, W_TIME, W_NETWORK = 0.30, 0.25, 0.20, 0.15, 0.10


@dataclass
class ScoreDetail:
    name: float
    org: float
    research: float
    time: float
    network: float

    @property
    def total(self) -> float:
        return (
            W_NAME * self.name
            + W_ORG * self.org
            + W_RESEARCH * self.research
            + W_TIME * self.time
            + W_NETWORK * self.network
        )

    def as_dict(self) -> dict:
        d = {
            "name": self.name, "org": self.org, "research": self.research,
            "time": self.time, "network": self.network,
        }
        d["total"] = round(self.total, 4)
        return d


# ---------- 打分（纯函数，单测友好） ----------

def score_name(a_raw: str, b_raw: str) -> float:
    """编辑距离映射：比值 ≥0.95 → 1.0；≥0.85 → 0.7；否则 0.2。

    颠倒序比较必须在归一化前的原始名上做（normalize 会抹掉词序）。
    M2-T4：人名归一走 normalize_person_name（中文→拼音，与英文同域）。
    """
    a = normalize_person_name(a_raw)
    if not a or not b_raw:
        return 0.2
    b = normalize_person_name(b_raw)
    b_swapped = normalize_person_name(swap_name_order(b_raw))
    dist = min(levenshtein(a, b), levenshtein(a, b_swapped))
    ratio = 1 - dist / max(len(a), len(b))
    if ratio >= 0.95:
        return 1.0
    if ratio >= 0.85:
        return 0.7
    return 0.2


def score_org(author_affiliation: str | None, person_org_norms: set[str]) -> float:
    """同 org 1.0 / 相近不同写 0.7 / 无机构或不相干 0.4。"""
    if not author_affiliation or not person_org_norms:
        return 0.4
    affil = normalize_org(author_affiliation)
    if affil in person_org_norms:
        return 1.0
    import difflib

    for org in person_org_norms:
        if difflib.SequenceMatcher(None, affil, org).ratio() >= 0.8:
            return 0.7
    return 0.4


def score_research(paper_tags: set[str], person_tags: set[str]) -> float:
    """Jaccard；双方皆空视为中性 0.5。"""
    if not paper_tags and not person_tags:
        return 0.5
    union = paper_tags | person_tags
    if not union:
        return 0.5
    return len(paper_tags & person_tags) / len(union)


def score_time(
    paper_date: dt.date | None, active_start: dt.date | None, active_end: dt.date | None
) -> float:
    """区间内 1.0 / 相邻（±1 年）0.6 / 更远 0.3；无区间数据中性 0.5。"""
    if paper_date is None or active_start is None or active_end is None:
        return 0.5
    if active_start <= paper_date <= active_end:
        return 1.0
    gap = min(abs((paper_date - active_start).days), abs((paper_date - active_end).days))
    return 0.6 if gap <= 365 else 0.3


def score_network(shared_coauthors: int) -> float:
    """共享 ≥2 合作者 1.0 / 1 个 0.6 / 0 个 0.2。"""
    if shared_coauthors >= 2:
        return 1.0
    if shared_coauthors == 1:
        return 0.6
    return 0.2


# ---------- Person 聚合数据 ----------

async def person_org_norms(session: AsyncSession, person_id: int) -> set[str]:
    rows = (
        await session.execute(
            select(Organization.name_normalized)
            .join(PersonOrg, PersonOrg.org_id == Organization.id)
            .where(PersonOrg.person_id == person_id)
        )
    ).scalars().all()
    return set(rows)


async def _person_paper_meta(
    session: AsyncSession, person_id: int, exclude_paper_id: int | None = None
) -> tuple[set[str], list[dt.date], set[str]]:
    """（研究方向标签, 论文日期列表, 合作者 normalized 集合）。"""
    stmt = (
        select(Paper, PaperAuthor)
        .join(PaperAuthor, PaperAuthor.paper_id == Paper.id)
        .where(PaperAuthor.person_id == person_id)
    )
    if exclude_paper_id is not None:
        stmt = stmt.where(Paper.id != exclude_paper_id)
    rows = (await session.execute(stmt)).all()

    tags: set[str] = set()
    dates: list[dt.date] = []
    paper_ids = {paper.id for paper, _ in rows}
    for paper, _ in rows:
        tags.update(paper.research_tags or [])
        if paper.published_at is not None:
            dates.append(paper.published_at.date())
    coauthors = set()
    if paper_ids:
        from sqlalchemy import or_

        co_rows = (
            await session.execute(
                select(PaperAuthor.raw_name).where(
                    PaperAuthor.paper_id.in_(paper_ids),
                    or_(
                        PaperAuthor.person_id.is_(None),
                        PaperAuthor.person_id != person_id,
                    ),
                )
            )
        ).scalars().all()
        coauthors = {normalize_person_name(n) for n in co_rows}
    return tags, dates, coauthors


# ---------- 候选与主流程 ----------

async def find_candidates(session: AsyncSession, raw_name: str) -> list[Person]:
    """准确度优先：精确同名 + 模糊变体（编辑距离 ≤2，姓名序颠倒双向）。

    颠倒比较必须在归一化前的原始名上做（normalize 会抹掉词序）。
    M2-T4：归一走 normalize_person_name——中文"张三"的拼音归一
    与既有英文 Person("Zhang San") 精确命中（RD-M2-12）。
    """
    name_norm = normalize_person_name(raw_name)
    if not name_norm:
        return []
    reversed_norm = normalize_person_name(swap_name_order(raw_name))
    exact = (
        await session.execute(
            select(Person).where(
                Person.name_normalized == name_norm,
                Person.merged_into_id.is_(None),  # 排除审核合并墓碑
            )
        )
    ).scalars().all()
    candidate_ids = {p.id for p in exact}

    all_persons = (
        await session.execute(
            select(Person).where(Person.merged_into_id.is_(None))
        )
    ).scalars().all()
    for p in all_persons:
        if p.id in candidate_ids:
            continue
        if (
            levenshtein(name_norm, p.name_normalized) <= FUZZY_MAX_DIST
            or levenshtein(reversed_norm, p.name_normalized) <= FUZZY_MAX_DIST
        ):
            candidate_ids.add(p.id)

    by_id = {p.id: p for p in all_persons}
    return [by_id[i] for i in candidate_ids]


async def score_candidate(
    session: AsyncSession,
    candidate: Person,
    pa: PaperAuthor,
    paper: Paper,
    paper_coauthor_norms: set[str],
) -> ScoreDetail:
    tags, dates, coauthors = await _person_paper_meta(
        session, candidate.id, exclude_paper_id=paper.id
    )
    org_norms = await person_org_norms(session, candidate.id)

    active_start = min(dates) if dates else None
    active_end = max(dates) if dates else None
    self_norm = normalize_person_name(pa.raw_name)
    shared = len(
        (paper_coauthor_norms - {self_norm}) & (coauthors - {self_norm})
    )

    detail = ScoreDetail(
        name=score_name(pa.raw_name, candidate.name),
        org=score_org(pa.affiliation, org_norms),
        research=score_research(set(paper.research_tags or []), tags),
        time=score_time(
            paper.published_at.date() if paper.published_at else None,
            active_start,
            active_end,
        ),
        network=score_network(shared),
    )
    return detail


async def _refresh_person_tags(session: AsyncSession, person_id: int, paper_tags: list[str]) -> None:
    existing = set(
        (
            await session.execute(
                select(PersonResearchTag.tag).where(PersonResearchTag.person_id == person_id)
            )
        ).scalars().all()
    )
    for tag in set(paper_tags) - existing:
        session.add(PersonResearchTag(person_id=person_id, tag=tag))


async def _link_author(
    session: AsyncSession, pa: PaperAuthor, person: Person, paper: Paper
) -> None:
    pa.person_id = person.id
    await _refresh_person_tags(session, person.id, paper.research_tags or [])
    await sync_person_org(session, person.id)


async def enqueue_pair(
    session: AsyncSession, a_id: int, b_id: int, detail: ScoreDetail
) -> None:
    lo, hi = min(a_id, b_id), max(a_id, b_id)
    stmt = (
        pg_insert(DisambiguationQueue)
        .values(
            person_a_id=lo, person_b_id=hi,
            score=round(detail.total, 2), score_detail=detail.as_dict(),
            status="pending",
        )
        .on_conflict_do_nothing(index_elements=["person_a_id", "person_b_id"])
    )
    await session.execute(stmt)


async def strong_merge_match(
    session: AsyncSession, raw_name: str, affiliation: str | None
) -> Person | None:
    """强归并（M2 RD-M2-12）：姓名归一精确命中（含颠倒）且机构为同一
    organizations 实体（署名机构归一 == 候选某机构归一）→ 直接归并，
    identity 基准 1.0，不进队列。未命中返回 None（走打分路径）。
    """
    name_norm = normalize_person_name(raw_name)
    if not name_norm or not affiliation:
        return None
    swapped = normalize_person_name(swap_name_order(raw_name))
    norms = [name_norm] if swapped == name_norm else [name_norm, swapped]
    hits = (
        await session.execute(
            select(Person).where(
                Person.name_normalized.in_(norms),
                Person.merged_into_id.is_(None),
            )
        )
    ).scalars().all()
    if not hits:
        return None
    affil_norm = normalize_org(affiliation)
    if not affil_norm:
        return None
    for cand in hits:
        if affil_norm in await person_org_norms(session, cand.id):
            return cand
    return None


async def process_author(
    session: AsyncSession, pa: PaperAuthor, paper: Paper, paper_coauthor_norms: set[str]
) -> str:
    """单作者消歧。返回 linked_existing / created / queued。

    identity 基准：openalex_id 强键与 RD-M2-12 强归并 1.0；
    打分归并取消歧得分；新建 0.9（中文名略降，mentor_linker/T6 消费）。
    """
    # 1. openalex_id 强匹配
    if pa.openalex_id:
        person = (
            await session.execute(
                select(Person).where(Person.openalex_id == pa.openalex_id)
            )
        ).scalar_one_or_none()
        if person is not None:
            await _link_author(session, pa, person, paper)
            return "linked_existing"

    # 1.5 强归并（M2-T4）：姓名归一命中 + 同机构实体 → 直连不进队列
    strong = await strong_merge_match(session, pa.raw_name, pa.affiliation)
    if strong is not None:
        await _link_author(session, pa, strong, paper)
        return "linked_existing"

    # 2. 候选打分
    candidates = await find_candidates(session, pa.raw_name)
    best_person, best_detail = None, None
    for cand in candidates:
        detail = await score_candidate(session, cand, pa, paper, paper_coauthor_norms)
        if best_detail is None or detail.total > best_detail.total:
            best_person, best_detail = cand, detail

    # 3. 决策
    if best_person is not None and best_detail.total >= AUTO_MERGE_THRESHOLD:
        await _link_author(session, pa, best_person, paper)
        return "linked_existing"

    new_person = Person(
        name=pa.raw_name, name_normalized=normalize_person_name(pa.raw_name),
        openalex_id=pa.openalex_id or None,
    )
    session.add(new_person)
    await session.flush()
    await _link_author(session, pa, new_person, paper)

    if best_person is not None and best_detail.total >= QUEUE_THRESHOLD:
        await enqueue_pair(session, new_person.id, best_person.id, best_detail)
        return "queued"
    return "created"


async def run_disambiguation(
    session: AsyncSession, paper_ids: list[int] | None = None
) -> dict[str, int]:
    """对已抽取论文的全部未关联作者执行消歧。"""
    stats = {"linked_existing": 0, "created": 0, "queued": 0}

    stmt = select(Paper).where(Paper.status == "extracted")
    if paper_ids is not None:
        stmt = stmt.where(Paper.id.in_(paper_ids))
    papers = (await session.execute(stmt)).scalars().all()

    for paper in papers:
        pas = (
            await session.execute(
                select(PaperAuthor)
                .where(
                    PaperAuthor.paper_id == paper.id,
                    PaperAuthor.person_id.is_(None),
                )
                .order_by(PaperAuthor.author_seq)
            )
        ).scalars().all()
        coauthor_norms = {
            normalize_person_name(pa.raw_name)
            for pa in (
                await session.execute(
                    select(PaperAuthor).where(PaperAuthor.paper_id == paper.id)
                )
            ).scalars().all()
        }
        for pa in pas:
            result = await process_author(session, pa, paper, coauthor_norms)
            stats[result] += 1

    await session.commit()
    return stats
