"""arXiv RSS 采集器（T5，FR-1.1/1.6/1.7）。

按 directions.yaml 的 18 分类逐个拉取 RSS，解析后批量 upsert 进 papers
（arxiv_id 冲突跳过）。单分类失败写 failed_jobs（job_type=rss_fetch），
批次不中断（FR-1.6）。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import feedparser
import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import load_directions
from app.models import Paper
from app.services.failed_jobs import schedule_retry
from app.settings import settings

ARXIV_ID_RE = re.compile(r"abs/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)")
CATEGORY_RE = re.compile(r"^[a-z-]+\.[A-Z]{2}$")
USER_AGENT = "prof-graph/0.1 (academic-network-governance; internal)"
HTML_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class CollectReport:
    categories_ok: list[str] = field(default_factory=list)
    categories_failed: list[str] = field(default_factory=list)
    added: int = 0
    skipped: int = 0

    @property
    def failed(self) -> bool:
        return bool(self.categories_failed)


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", HTML_TAG_RE.sub("", text or "")).strip()


def _parse_authors(raw: str | None) -> list[str]:
    """dc:creator 形如 'Wei Zhang (PKU), Li Wang (THU)'：按 ') ,' 切分保住人名内逗号。"""
    if not raw:
        return []
    if "(" in raw:
        parts = re.split(r"\)\s*,\s*", raw)
        # 分隔符吞掉了上一个机构的右括号，补回；最后一段保留原样
        fixed = [p.strip() + ")" for p in parts[:-1]] + [parts[-1].strip()]
        return [p for p in fixed if p]
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_categories(entry, feed_category: str) -> list[str]:
    """条目分类 = 订阅分类 + 条目自带的 arXiv 分类标签（交叉列表论文有多类）。"""
    cats = {feed_category}
    for tag in getattr(entry, "tags", None) or []:
        term = tag.get("term", "") if isinstance(tag, dict) else getattr(tag, "term", "")
        if CATEGORY_RE.match(term):
            cats.add(term)
    return sorted(cats)


def parse_rss(xml_text: str, feed_category: str) -> list[dict]:
    """RSS XML → 论文行（纯函数，不入库）。无法解析出 arxiv_id 的条目跳过。"""
    rows: list[dict] = []
    for entry in feedparser.parse(xml_text).entries:
        link = entry.get("link", "") or entry.get("id", "")
        m = ARXIV_ID_RE.search(link)
        if not m:
            continue
        published = None
        # feedparser 对 RSS2 的 dc:date 映射为 updated_parsed，对 pubDate 才是 published_parsed
        time_struct = (
            entry.get("published_parsed")
            or entry.get("updated_parsed")
            or entry.get("created_parsed")
        )
        if time_struct:
            published = dt.datetime(*time_struct[:6], tzinfo=dt.timezone.utc)
        rows.append(
            {
                "arxiv_id": m.group(1),
                "title": _clean(entry.get("title")),
                "abstract": _clean(entry.get("summary")) or None,
                "authors_raw": _parse_authors(entry.get("author")),
                "published_at": published,
                "categories": _parse_categories(entry, feed_category),
                "rss_entry": {
                    "title": entry.get("title", ""),
                    "link": link,
                    "author": entry.get("author", ""),
                    "published": entry.get("published", ""),
                    "summary": entry.get("summary", ""),
                },
            }
        )
    return rows


async def fetch_category(client: httpx.AsyncClient, category: str) -> list[dict]:
    # arXiv RSS 现行格式为 {base}/{category}（旧 /rss.xml 后缀已 404）
    resp = await client.get(f"{settings.arxiv_rss_base}/{category}")
    resp.raise_for_status()
    return parse_rss(resp.text, category)


async def ingest_papers(session: AsyncSession, rows: list[dict]) -> tuple[int, int]:
    """批量 upsert（ON CONFLICT DO NOTHING）。返回 (added, skipped)。"""
    if not rows:
        return 0, 0
    # 不同分类的 RSS 可能含同一篇交叉列表论文，同批内先按 arxiv_id 去重
    unique = list({r["arxiv_id"]: r for r in rows}.values())
    stmt = (
        pg_insert(Paper)
        .values(unique)
        .on_conflict_do_nothing(index_elements=[Paper.arxiv_id])
        .returning(Paper.id)
    )
    inserted = (await session.execute(stmt)).scalars().all()
    return len(inserted), len(unique) - len(inserted)


async def _collect(
    session: AsyncSession, client: httpx.AsyncClient, categories: tuple[str, ...]
) -> CollectReport:
    report = CollectReport()
    for category in categories:
        try:
            rows = await fetch_category(client, category)
        except Exception as e:  # noqa: BLE001 — 任何单分类失败都不能中断批次
            report.categories_failed.append(category)
            await schedule_retry(session, "rss_fetch", category, f"{type(e).__name__}: {e}")
            continue
        added, skipped = await ingest_papers(session, rows)
        report.added += added
        report.skipped += skipped
        report.categories_ok.append(category)
    await session.commit()
    return report


async def collect_all(
    session: AsyncSession,
    client: httpx.AsyncClient | None = None,
    categories: tuple[str, ...] | None = None,
) -> CollectReport:
    """全量采集入口。categories 默认取 directions.yaml 的 arxiv_categories。"""
    cats = categories if categories is not None else load_directions().arxiv_categories
    if client is not None:
        return await _collect(session, client, cats)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as own:
        return await _collect(session, own, cats)
