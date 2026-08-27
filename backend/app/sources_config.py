"""sources.yaml 加载器（M2-T0）：M2 数据源配置，启动时校验，进程内缓存。

仿 directions.yaml 加载器（app/config.py）：
- rss 段：AI 资讯源（tier 枚举 → 数据源可信度档，plan §4）
- crawl 段：高校官网种子 + 爬取合规参数（NFR-1：限速 / 深度 / 重爬周期）
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import yaml

SOURCES_PATH = Path(
    os.environ.get("SOURCES_CONFIG", Path(__file__).resolve().parents[1] / "config" / "sources.yaml")
)

# 数据源可信度档（plan §4）：知名 AI 媒体 0.8 / 其他 0.6；高校官网新闻不走 rss 段（恒 1.0）
RSS_TIERS: dict[str, float] = {"known_media": 0.8, "other": 0.6}
# 页面类型（web_pages.page_type，plan §2 DDL）
PAGE_TYPES = frozenset({"faculty", "lab_members", "grad_list", "news"})


@dataclass(frozen=True)
class RssSource:
    id: str
    url: str
    tier: str
    enabled: bool = True

    @property
    def confidence(self) -> float:
        return RSS_TIERS[self.tier]


@dataclass(frozen=True)
class CrawlSeed:
    id: str
    school: str
    org_path: str
    url: str
    page_type: str


@dataclass(frozen=True)
class SourcesConfig:
    rss: tuple[RssSource, ...]
    seeds: tuple[CrawlSeed, ...]
    rate_limit_seconds: int = 2
    depth_limit: int = 1
    recrawl_days: int = 7

    def enabled_rss(self) -> tuple[RssSource, ...]:
        return tuple(s for s in self.rss if s.enabled)


def _require_url(url: object, where: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"sources.yaml {where} 缺 url")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"sources.yaml {where} url 非法：{url!r}")
    return url.strip()


@lru_cache(maxsize=1)
def load_sources() -> SourcesConfig:
    raw = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}

    rss_items = raw.get("rss") or []
    if not rss_items:
        raise ValueError("sources.yaml 缺 rss 段或为空")
    rss: list[RssSource] = []
    rss_seen: set[str] = set()
    for item in rss_items:
        sid = item.get("id")
        if not sid or sid in rss_seen:
            raise ValueError(f"sources.yaml rss 段存在缺失或重复的 id：{sid!r}")
        rss_seen.add(sid)
        tier = item.get("tier")
        if tier not in RSS_TIERS:
            raise ValueError(f"sources.yaml rss[{sid}] tier 非法：{tier!r}（合法值 {sorted(RSS_TIERS)}）")
        rss.append(
            RssSource(
                id=sid,
                url=_require_url(item.get("url"), f"rss[{sid}]"),
                tier=tier,
                enabled=bool(item.get("enabled", True)),
            )
        )

    crawl = raw.get("crawl") or {}
    seed_items = crawl.get("seeds") or []
    if not seed_items:
        raise ValueError("sources.yaml 缺 crawl.seeds 或为空")
    seeds: list[CrawlSeed] = []
    seed_seen: set[str] = set()
    for item in seed_items:
        sid = item.get("id")
        if not sid or sid in seed_seen:
            raise ValueError(f"sources.yaml crawl.seeds 存在缺失或重复的 id：{sid!r}")
        seed_seen.add(sid)
        page_type = item.get("page_type")
        if page_type not in PAGE_TYPES:
            raise ValueError(
                f"sources.yaml crawl.seeds[{sid}] page_type 非法：{page_type!r}（合法值 {sorted(PAGE_TYPES)}）"
            )
        school = (item.get("school") or "").strip()
        org_path = (item.get("org_path") or "").strip()
        if not school or not org_path:
            raise ValueError(f"sources.yaml crawl.seeds[{sid}] 缺 school 或 org_path（消歧强合并需机构依据）")
        seeds.append(
            CrawlSeed(
                id=sid,
                school=school,
                org_path=org_path,
                url=_require_url(item.get("url"), f"crawl.seeds[{sid}]"),
                page_type=page_type,
            )
        )

    rate_limit_seconds = crawl.get("rate_limit_seconds", 2)
    depth_limit = crawl.get("depth_limit", 1)
    recrawl_days = crawl.get("recrawl_days", 7)
    if not isinstance(rate_limit_seconds, (int, float)) or rate_limit_seconds < 1:
        raise ValueError("sources.yaml crawl.rate_limit_seconds 必须 ≥1（NFR-1 同主机限速）")
    if not isinstance(depth_limit, int) or depth_limit < 0:
        raise ValueError("sources.yaml crawl.depth_limit 必须为 ≥0 的整数")
    if not isinstance(recrawl_days, int) or recrawl_days < 1:
        raise ValueError("sources.yaml crawl.recrawl_days 必须为 ≥1 的整数")

    return SourcesConfig(
        rss=tuple(rss),
        seeds=tuple(seeds),
        rate_limit_seconds=rate_limit_seconds,
        depth_limit=depth_limit,
        recrawl_days=recrawl_days,
    )
