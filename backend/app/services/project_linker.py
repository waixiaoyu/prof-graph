"""项目合作关系建立（M2-T11，FR-6.1~6.5，plan §4.2 公式 + §4.3 合并语义）。

同项目参与者两两建 project_cooperation（推导在代码，不让 GLM 配对）：
confidence = 0.3×explicitness + 0.3×sufficiency + 0.2×src + 0.2×accessibility
identity   = min(两端成员 identity)：强归并 1.0 / 打分取分 / 新建 0.9（RD-M2-12）
strength   = identity × tier(共同项目数)：1 个 0.90 / 2 个 0.95 / 3+ 1.00

confidenct 无存储列（同传承链路）：计算值落 evidence_summary 文本审计；
落库数值为 identity_confidence / strength（C9 检查域）。

证据幂等（§4.3）：(relationship, news_item) 主键已存在 → 不重算不重复计；
新增证据 → coop_count=资讯证据行数（共同项目数的代理指标——证据表无项目
外键，同一资讯内多项目同对无法区分，跨资讯同项目重复报道会略高估，
两皆罕见，取行数口径与 C1 事实来源一致）、strength 按公式重查、时间范围
取资讯 published_at 并集。

编排（run_news_link，T12 管线入口）：新闻公示页先同步成 NewsItem（C8：
项目关系证据锚点统一 news_items），再走与 RSS 条目一致的抽取-建链路径。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    NewsItem,
    Relationship,
    RelationshipEvidenceNews,
    WebPage,
)
from app.services.breaker import BreakerOpenError
from app.services.failed_jobs import schedule_retry
from app.services.glm import GLMClient, GLMError, GLMParseError, GLMTransientError
from app.services.news_extractor import (
    EXPLICITNESS_SCORES,
    SUFFICIENCY_SCORES,
    NewsExtraction,
    NewsPerson,
    Participation,
    extract_news_item,
    extract_news_page,
    normalize_project_name,
    resolve_news_person,
    sync_news_page_item,
    upsert_project,
)
from app.services.page_extractor import Member
from app.utils.names import normalize_person_name

log = logging.getLogger("prof-graph.project_linker")

REL_TYPE = "project_cooperation"
W_EXPL, W_SUFF, W_SRC, W_ACCESS = 0.3, 0.3, 0.2, 0.2


def tier_strength(coop_count: int) -> float:
    """共同项目数 → 强度基准（1 个 0.90 / 2 个 0.95 / 3+ 1.00）。"""
    if coop_count >= 3:
        return 1.0
    return {1: 0.90, 2: 0.95}.get(coop_count, 0.90)


def compute_confidence(expl: str, suff: str, src: float, access: float) -> float:
    """plan §4.2 公式（纯函数，单测对齐分档算例）。"""
    value = (
        W_EXPL * EXPLICITNESS_SCORES[expl]
        + W_SUFF * SUFFICIENCY_SCORES[suff]
        + W_SRC * src
        + W_ACCESS * access
    )
    return round(min(1.0, value), 4)


def _summary(
    participation: Participation, ext: NewsExtraction, confidence: float, coop: int,
) -> str:
    parts = [
        f"基于{ext.source_desc}：共同参与「{participation.project_name}」",
        f"明确性 {participation.explicitness}、信息充分度 {participation.sufficiency}",
        f"置信度 {confidence:.2f}",
    ]
    if coop > 1:
        parts.append(f"共 {coop} 项合作证据")
    return "；".join(parts)


async def link_project_pair(
    session: AsyncSession,
    a: NewsPerson,
    b: NewsPerson,
    participation: Participation,
    ext: NewsExtraction,
    item: NewsItem,
) -> str:
    """建立/合并一对项目合作关系（news 证据锚点）。返回 created / merged / dup。"""
    if a.person_id is None or b.person_id is None:
        return "dup"
    lo, hi = sorted((a.person_id, b.person_id))
    if lo == hi:
        return "dup"

    rel = (
        await session.execute(
            select(Relationship).where(
                Relationship.person_a_id == lo,
                Relationship.person_b_id == hi,
                Relationship.type == REL_TYPE,
                Relationship.subtype == "",
            )
        )
    ).scalar_one_or_none()
    if rel is not None:
        ev_exists = (
            await session.execute(
                select(RelationshipEvidenceNews).where(
                    RelationshipEvidenceNews.relationship_id == rel.id,
                    RelationshipEvidenceNews.news_item_id == item.id,
                )
            )
        ).scalar_one_or_none() is not None
        if ev_exists:
            return "dup"  # 证据幂等：既有 (rel, news) 不重算不重复计
    is_new = rel is None

    confidence = compute_confidence(
        participation.explicitness, participation.sufficiency, ext.src_confidence, ext.accessibility
    )
    identity = round(min(a.identity, b.identity), 4)

    if is_new:
        rel = Relationship(
            person_a_id=lo,
            person_b_id=hi,
            type=REL_TYPE,
            subtype="",
            identity_confidence=identity,
            strength=identity,  # 占位，下方按公式重算
            coop_count=0,
        )
        session.add(rel)
        await session.flush()

    session.add(RelationshipEvidenceNews(relationship_id=rel.id, news_item_id=item.id))
    await session.flush()

    # coop_count 的事实来源是证据表（C1）；口径为共同合作证据（资讯）数
    coop = (
        await session.execute(
            select(func.count())
            .select_from(RelationshipEvidenceNews)
            .where(RelationshipEvidenceNews.relationship_id == rel.id)
        )
    ).scalar_one()
    rel.coop_count = coop
    # identity 历史最好：新证据端身份更确定时不降
    rel.identity_confidence = round(max(float(rel.identity_confidence), identity), 4)
    rel.strength = round(min(1.0, float(rel.identity_confidence) * tier_strength(coop)), 4)
    rel.evidence_summary = _summary(participation, ext, confidence, coop)

    if item.published_at is not None:
        d = item.published_at.date()
        if rel.time_start is None or d < rel.time_start:
            rel.time_start = d
        if rel.time_end is None or d > rel.time_end:
            rel.time_end = d
    return "created" if is_new else "merged"


async def link_news_relations(
    session: AsyncSession, ext: NewsExtraction, item: NewsItem
) -> dict[str, int]:
    """单条资讯关系建立：实体入库 → 按项目分组 → 同项目两两配对。"""
    stats = {"created": 0, "merged": 0, "dup": 0}
    for np in ext.persons:
        await resolve_news_person(session, np)
    for proj in ext.projects:
        await upsert_project(session, proj)
    by_norm_person = {normalize_person_name(p.name): p for p in ext.persons}
    by_norm_project = {normalize_project_name(p.name): p for p in ext.projects}

    # 同项目参与者分组（同项目去重到人）
    groups: dict[str, tuple[Participation, list[NewsPerson]]] = {}
    for part in ext.participations:
        person = by_norm_person.get(normalize_person_name(part.person_name))
        project = by_norm_project.get(normalize_project_name(part.project_name))
        if person is None or project is None:
            continue  # 参与对引用的人员/项目不在抽取结果中（schema 要求对应，容错跳过）
        key = normalize_project_name(part.project_name)
        entry = groups.setdefault(key, (part, []))
        if person not in entry[1]:
            entry[1].append(person)

    for part, members in groups.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                outcome = await link_project_pair(session, members[i], members[j], part, ext, item)
                stats[outcome] += 1
    await session.flush()
    return stats


@dataclass
class NewsLinkReport:
    items_extracted: int = 0
    items_no_signal: int = 0
    items_failed: int = 0
    breaker_skipped: int = 0
    pages_extracted: int = 0  # 新闻公示页（同步成 NewsItem 后同路径处理）
    pairs_created: int = 0
    pairs_merged: int = 0
    pairs_dup: int = 0


async def _process_item(
    session: AsyncSession, glm: GLMClient, ext: NewsExtraction, item: NewsItem,
    report: NewsLinkReport, page: WebPage | None = None,
) -> None:
    """单条资讯：无信号短路 / 建链成功才置 extracted（崩溃安全）。"""
    if ext.no_signal or not ext.participations:
        item.status = "no_signal"
        report.items_no_signal += 1
        if page is not None:
            page.status = "no_signal"
        await session.commit()
        return
    stats = await link_news_relations(session, ext, item)
    report.pairs_created += stats["created"]
    report.pairs_merged += stats["merged"]
    report.pairs_dup += stats["dup"]
    item.status = "extracted"
    report.items_extracted += 1
    if page is not None:
        page.status = "extracted"
        page.last_extracted_hash = page.content_hash
        report.pages_extracted += 1
    await session.commit()  # 逐条提交：批次中断不丢已完成进度


async def run_news_link(
    session: AsyncSession,
    glm: GLMClient,
    news_ids: list[int] | None = None,
    page_ids: list[int] | None = None,
) -> NewsLinkReport:
    """news_extract/project_link 阶段入口（T12 管线接入）。

    新闻公示页（page_type=news，RD-M2-11）先同步成 NewsItem 再处理；
    RSS 条目处理 pending_screen / extraction_failed（重试执行器显式 news_ids
    不限状态）。GLM 失败 → failed_jobs(news_extract) + 状态回退等待重试。
    """
    report = NewsLinkReport()

    pages: list[WebPage] = []
    if page_ids is not None:
        pages = (
            (
                await session.execute(
                    select(WebPage).where(
                        WebPage.page_type == "news", WebPage.id.in_(page_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
    else:
        pages = (
            (
                await session.execute(
                    select(WebPage).where(
                        WebPage.page_type == "news",
                        WebPage.status.in_(("pending_extraction", "extraction_failed")),
                    )
                )
            )
            .scalars()
            .all()
        )
    for page in pages:
        item = await sync_news_page_item(session, page)
        try:
            ext = await extract_news_page(session, glm, page)
        except BreakerOpenError:
            report.breaker_skipped += 1
            break
        except (GLMTransientError, GLMParseError, GLMError, ValueError) as e:
            await schedule_retry(session, "news_extract", page.url, f"{type(e).__name__}: {e}")
            page.status = "extraction_failed"
            report.items_failed += 1
            await session.commit()
            continue
        await _process_item(session, glm, ext, item, report, page=page)

    stmt = select(NewsItem)
    if news_ids is None:
        stmt = stmt.where(NewsItem.status.in_(("pending_screen", "extraction_failed")))
    else:
        stmt = stmt.where(NewsItem.id.in_(news_ids))
    items = (await session.execute(stmt)).scalars().all()
    for item in items:
        if item.rss_entry and item.rss_entry.get("source") == "webpage":
            continue  # 新闻公示页已在上方按 page 路径处理
        try:
            ext = await extract_news_item(session, glm, item)
        except BreakerOpenError:
            report.breaker_skipped += 1
            break
        except (GLMTransientError, GLMParseError, GLMError, ValueError) as e:
            await schedule_retry(session, "news_extract", item.url, f"{type(e).__name__}: {e}")
            item.status = "extraction_failed"
            report.items_failed += 1
            await session.commit()
            continue
        await _process_item(session, glm, ext, item, report)
    return report
