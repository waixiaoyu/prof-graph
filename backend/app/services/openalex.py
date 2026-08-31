"""OpenAlex 客户端 + 机构双源补全（T10，FR-2.4，RD-2）。

双源置信度（plan §6）：
- GLM 抽到机构（paper_authors.affiliation 且 org_source 非 'openalex'）→ 1.0
- OpenAlex 匹配（姓名精确 + 论文吻合）→ 0.8，并回写 openalex_id
- 均无 → 0.4 兜底（无 org 可挂，不写 person_org）

管线顺序：extractor（T9）→ enrich_papers（本模块，落 paper_authors.openalex_id
与机构，供 T11 强匹配）→ disambiguator（T11 建 Person 并关联）→
sync_person_org（本模块，写 organizations + person_org + person.openalex_id）。

OpenAlex 规范：mailto 参数提高速率配额，客户端限 5 req/s；机构归一化去重
（"Tsinghua University" 与 "Tsinghua Univ." 合并）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Organization, Paper, PaperAuthor, Person, PersonOrg
from app.settings import settings
from app.utils.names import normalize_name

log = logging.getLogger("prof-graph.openalex")

API_BASE = "https://api.openalex.org"
MIN_INTERVAL = 0.2  # 5 req/s
USER_AGENT = "prof-graph/0.2 (mailto:{mailto})"
# 连续失败熔断：OpenAlex 限流(429)/网络故障时中止本阶段富集，剩余论文下批续跑，
# 避免单源补全卡死整条管线（机构缺失走 0.4 兜底，不阻塞消歧/挂接）
CONSECUTIVE_FAILURE_LIMIT = 10


@dataclass(frozen=True)
class AuthorshipMatch:
    openalex_id: str
    institution: str | None


def _short_openalex_id(url: str) -> str:
    """https://openalex.org/A5076100134 → A5076100134。"""
    return url.rstrip("/").rsplit("/", 1)[-1]


def _authorship_map(authorships: list[dict]) -> dict[str, AuthorshipMatch]:
    """authorships → {name_normalized: 匹配}（姓名精确匹配）。"""
    result: dict[str, AuthorshipMatch] = {}
    for a in authorships:
        author = a.get("author") or {}
        display_name = author.get("display_name")
        author_id = author.get("id")
        if not display_name or not author_id:
            continue
        raw_affs = a.get("raw_affiliation_strings") or []
        institutions = a.get("institutions") or []
        institution = None
        if institutions:
            institution = institutions[0].get("display_name")
        elif raw_affs:
            institution = raw_affs[0]
        result[normalize_name(display_name)] = AuthorshipMatch(
            openalex_id=_short_openalex_id(author_id), institution=institution
        )
    return result


class OpenAlexClient:
    """限速封装：全局串行 + 最小间隔（5 req/s，礼貌配额内远低于上限）。"""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def _get(self, path: str, params: dict | None = None) -> dict | list | None:
        async with self._lock:
            wait = self._last_call + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
        params = {**(params or {}), "mailto": settings.openalex_mailto}
        resp = await self._http.get(f"{API_BASE}{path}", params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def lookup_paper(self, arxiv_id: str, title: str) -> list[dict] | None:
        """按 DOI（arXiv 预印本 DOI）直查，404 时回退标题搜索。返回 authorships。"""
        arxiv_no_ver = re.sub(r"v\d+$", "", arxiv_id)
        work = await self._get(f"/works/https://doi.org/10.48550/arxiv.{arxiv_no_ver}")
        if work is None:
            results = await self._get(
                "/works", {"filter": f"title.search:{title}", "per-page": 5}
            )
            norm_title = normalize_name(title)
            for w in (results or {}).get("results", []):
                if normalize_name(w.get("display_name") or "") == norm_title:
                    work = w
                    break
        if not work:
            return None
        return work.get("authorships") or []


# ---------- 机构归一化 ----------

_ORG_STRIP_TOKENS = {"university", "univ", "institute", "inst", "college",
                     "lab", "laboratory", "school", "academy"}


def normalize_org(name: str) -> str:
    """机构归一化：小写、去标点、去掉通用后缀词（University/Univ./Institute...）。"""
    tokens = re.split(r"[\s,]+", name.lower())
    kept = [t.strip(".") for t in tokens if t and t.strip(".") not in _ORG_STRIP_TOKENS]
    return " ".join(kept) or name.lower().strip()


async def upsert_organization(session: AsyncSession, name: str) -> Organization:
    norm = normalize_org(name)
    org = (
        await session.execute(
            select(Organization).where(Organization.name_normalized == norm)
        )
    ).scalar_one_or_none()
    if org is None:
        org = Organization(name=name.strip(), name_normalized=norm)
        session.add(org)
        await session.flush()
    return org


# ---------- 批量补全 ----------


async def enrich_papers(
    session: AsyncSession,
    paper_ids: list[int] | None = None,
    client: OpenAlexClient | None = None,
    http: httpx.AsyncClient | None = None,
) -> int:
    """对已抽取论文补全 OpenAlex 信息（只处理 GLM 没抽到机构的作者行）。

    写 paper_authors.openalex_id / affiliation / org_source='openalex'。
    返回补全的作者行数。
    """
    # own = 两个注入位都没传才视为本阶段自建（双跑测试发现：只查 client 时
    # 会把调用方注入的 http 客户端关掉，第二轮采集报 client has been closed）
    own = client is None and http is None
    oa = client or OpenAlexClient(http or httpx.AsyncClient(timeout=httpx.Timeout(20.0)))
    try:
        stmt = (
            select(PaperAuthor, Paper)
            .join(Paper, PaperAuthor.paper_id == Paper.id)
            .where(
                Paper.status == "extracted",
                PaperAuthor.affiliation.is_(None),
                PaperAuthor.openalex_id.is_(None),
            )
        )
        if paper_ids is not None:
            stmt = stmt.where(Paper.id.in_(paper_ids))

        rows = (await session.execute(stmt)).all()
        by_paper: dict[int, list[PaperAuthor]] = {}
        paper_titles: dict[int, Paper] = {}
        for pa, paper in rows:
            by_paper.setdefault(paper.id, []).append(pa)
            paper_titles[paper.id] = paper

        enriched = 0
        consecutive_failures = 0
        total_papers = len(by_paper)
        for idx, (paper_id, authors) in enumerate(by_paper.items()):
            paper = paper_titles[paper_id]
            try:
                authorships = await oa.lookup_paper(paper.arxiv_id, paper.title)
            except httpx.HTTPError as e:
                log.warning("OpenAlex 查询失败（%s）：%s", paper.arxiv_id, e)
                consecutive_failures += 1
                if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                    log.warning(
                        "OpenAlex 连续 %d 次失败（疑似限流），中止本阶段富集，"
                        "剩余 %d 篇下批续跑",
                        consecutive_failures,
                        total_papers - idx,
                    )
                    break
                continue
            consecutive_failures = 0
            if not authorships:
                continue
            matches = _authorship_map(authorships)
            for pa in authors:
                m = matches.get(normalize_name(pa.raw_name))
                if m is None:
                    continue
                pa.openalex_id = m.openalex_id
                if m.institution:
                    pa.affiliation = m.institution
                pa.org_source = "openalex"
                enriched += 1
        await session.commit()
        return enriched
    finally:
        if own and oa._http is not None:  # noqa: SLF001
            await oa._http.aclose()  # noqa: SLF001


async def sync_person_org(session: AsyncSession, person_id: int) -> PersonOrg | None:
    """Person 落定后写 person_org（三源置信度）并回写 openalex_id。

    GLM 机构 1.0 > OpenAlex 0.8 > 无（不写行，语义上 0.4 兜底）。
    """
    # organizations.name/name_normalized 列宽（VARCHAR(300)）；超长拼接串跳过
    ORG_NAME_MAX = 300
    person = await session.get(Person, person_id)
    if person is None:
        raise ValueError(f"person {person_id} 不存在")

    rows = (
        await session.execute(
            select(PaperAuthor).where(PaperAuthor.person_id == person_id)
        )
    ).scalars().all()

    glm_row = next(
        (r for r in rows if r.affiliation and r.org_source != "openalex"), None
    )
    openalex_row = next(
        (r for r in rows if r.org_source == "openalex" and r.affiliation), None
    )

    source_row = glm_row or openalex_row
    if source_row is None or not source_row.affiliation:
        return None  # 均无机构：0.4 兜底，无 org 可挂

    if len(source_row.affiliation) > ORG_NAME_MAX:
        # 多机构拼接串装不进 organizations.name/name_normalized VARCHAR(300)
        # （2026-08-25~29 夜批消歧连崩根因）：跳过不挂、不建垃圾实体；
        # 分号拆分取主机构留 M4 机构规范化统一做
        return None

    org = await upsert_organization(session, source_row.affiliation)
    confidence = 1.0 if glm_row else 0.8
    source = "glm" if glm_row else "openalex"

    existing = (
        await session.execute(
            select(PersonOrg).where(
                PersonOrg.person_id == person_id, PersonOrg.org_id == org.id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = PersonOrg(
            person_id=person_id,
            org_id=org.id,
            org_confidence=confidence,
            source=source,
            paper_id=source_row.paper_id,
        )
        session.add(existing)
    else:  # 已有同机构关联，保留更高置信度
        if float(existing.org_confidence) < confidence:
            existing.org_confidence = confidence
            existing.source = source

    # 回写 openalex_id（0.8 路径的强身份信号）
    if not person.openalex_id:
        oa_id = next((r.openalex_id for r in rows if r.openalex_id), None)
        if oa_id:
            person.openalex_id = oa_id

    await session.flush()
    return existing
