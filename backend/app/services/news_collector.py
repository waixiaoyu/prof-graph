"""RSS 资讯采集器（M2-T9，FR-1.1~1.5，plan OQ-2）。

按 sources.yaml 启用源拉取 → url 归一去重 → 信号预筛（规则粗筛，无信号
screened_no_signal 不进 GLM）→ 入库 news_items。单源失败写
failed_jobs（job_type=news_fetch）不中断批次；连续 3 次失败的源
自动置内存态停用（不换配置文件本体，重启后按配置重试），状态经
/api/admin/metrics 报警（rss_source_states）。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
from dataclasses import dataclass, field

import feedparser
import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NewsItem
from app.services.failed_jobs import schedule_retry
from app.sources_config import SourcesConfig, load_sources

log = logging.getLogger("prof-graph.news_collector")

USER_AGENT = "prof-graph/0.2 (academic-network-governance; internal)"

# 预筛规则（FR-1.3）：中文姓名+称谓 或 项目合作关键词，二者其一
CN_NAME_TITLE_RE = re.compile(r"[\u4e00-\u9fff]{2,4}(教授|院士|研究员|副教授|讲师|博士|老师|团队)")
PROJECT_KEYWORDS = ("项目", "课题", "联合实验室", "合作签约")

# OQ-2：连续失败自动停用（进程内存态；重启后按配置重试）
FAIL_DISABLE_THRESHOLD = 3
_FAIL_COUNTS: dict[str, int] = {}
_DISABLED: set[str] = set()


def has_signal(title: str, summary: str) -> bool:
    """预筛：中文姓名+称谓模式 或 项目关键词命中。"""
    text = f"{title} {summary}"
    return bool(CN_NAME_TITLE_RE.search(text)) or any(k in text for k in PROJECT_KEYWORDS)


def normalize_entry_url(link: str | None, guid: str | None) -> str | None:
    """条目 url 归一（去 fragment/空白），link 优先，guid 兜底。"""
    raw = (link or guid or "").strip()
    if not raw:
        return None
    return raw.split("#", 1)[0]


def _entry_published(entry) -> dt.datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if parsed:
        return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)
    return None


def parse_feed(xml_text: str) -> list[dict]:
    """RSS/Atom → 条目列表（url/title/summary/published_at/rss_entry 原始审计）。"""
    feed = feedparser.parse(xml_text)
    items: list[dict] = []
    for entry in feed.entries:
        url = normalize_entry_url(getattr(entry, "link", None), getattr(entry, "id", None))
        title = (getattr(entry, "title", None) or "").strip()
        if not url or not title:
            continue
        summary = (getattr(entry, "summary", None) or "").strip()
        items.append(
            {
                "url": url,
                "title": title,
                "summary": summary,
                "published_at": _entry_published(entry),
                "rss_entry": {
                    "title": title,
                    "link": url,
                    "summary": summary,
                    "author": getattr(entry, "author", None),
                },
            }
        )
    return items


def rss_source_states(sources: SourcesConfig | None = None) -> list[dict]:
    """供 /api/admin/metrics 报警：各源连续失败计数与停用态。"""
    sources = sources or load_sources()
    return [
        {
            "id": s.id,
            "url": s.url,
            "tier": s.tier,
            "consecutive_failures": _FAIL_COUNTS.get(s.id, 0),
            "disabled": s.id in _DISABLED,
        }
        for s in sources.enabled_rss()
    ]


@dataclass
class NewsReport:
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)
    sources_skipped_disabled: list[str] = field(default_factory=list)
    disabled_now: list[str] = field(default_factory=list)
    added: int = 0
    skipped_dup: int = 0
    screened_no_signal: int = 0


async def collect_news(
    session: AsyncSession,
    http: httpx.AsyncClient | None = None,
    sources: SourcesConfig | None = None,
) -> NewsReport:
    """拉取全部启用源并入库。逐源容错，源级失败不中断批次。"""
    report = NewsReport()
    sources = sources or load_sources()
    own = http is None
    client = http or httpx.AsyncClient(
        timeout=httpx.Timeout(30.0), headers={"User-Agent": USER_AGENT}, follow_redirects=True
    )
    try:
        for src in sources.enabled_rss():
            if src.id in _DISABLED:
                report.sources_skipped_disabled.append(src.id)
                continue
            try:
                resp = await client.get(src.url)
                resp.raise_for_status()
                entries = parse_feed(resp.text)
            except Exception as e:  # noqa: BLE001 — 源级兜底
                _FAIL_COUNTS[src.id] = _FAIL_COUNTS.get(src.id, 0) + 1
                await schedule_retry(session, "news_fetch", src.url, f"{type(e).__name__}: {e}")
                report.sources_failed.append(src.id)
                if _FAIL_COUNTS[src.id] >= FAIL_DISABLE_THRESHOLD:
                    _DISABLED.add(src.id)
                    report.disabled_now.append(src.id)
                    log.error(
                        "RSS 源 %s 连续 %d 次失败，自动停用（重启后按配置重试；/api/admin/metrics 查看）",
                        src.id, _FAIL_COUNTS[src.id],
                    )
                continue
            _FAIL_COUNTS[src.id] = 0  # 成功即复位

            seen_urls: set[str] = set()
            for item in entries:
                if item["url"] in seen_urls:
                    report.skipped_dup += 1
                    continue
                seen_urls.add(item["url"])
                status = "pending_screen" if has_signal(item["title"], item["summary"]) else "screened_no_signal"
                if status == "screened_no_signal":
                    report.screened_no_signal += 1
                stmt = (
                    pg_insert(NewsItem)
                    .values(
                        source_id=src.id,
                        url=item["url"],
                        title=item["title"],
                        summary=item["summary"],
                        published_at=item["published_at"] or dt.datetime.now(dt.timezone.utc),
                        rss_entry=item["rss_entry"],
                        status=status,
                    )
                    .on_conflict_do_nothing(index_elements=["url"])
                    .returning(NewsItem.id)
                )
                inserted = (await session.execute(stmt)).scalar_one_or_none()
                if inserted is None:
                    report.skipped_dup += 1
                else:
                    report.added += 1
            report.sources_ok.append(src.id)
            await session.commit()
    finally:
        if own:
            await client.aclose()
    return report
