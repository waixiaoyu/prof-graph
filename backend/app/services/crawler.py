"""定向爬虫（M2-T3，FR-2 / NFR-1）。

高校官网定向爬取（学术传承主线数据入口）：
- 种子到期计算：新种子全量首爬；老页面按 recrawl_days 到期重爬
- robots.txt：per-host 缓存 1 天（urllib.robotparser），禁止则跳过记 warning
- 限速：同 host 相邻请求 ≥ rate_limit_seconds（默认 2s）
- 深度 ≤ depth_limit（默认 1）：仅跟进同 host 且命中成员/列表链接模式的 <a>
- BeautifulSoup 去导航/脚本取正文；SHA-256 内容指纹
- web_pages 快照 upsert（url 唯一）：内容变化才置回 pending_extraction
- 单页失败写 failed_jobs（job_type=web_crawl），批次不中断（FR-2.5）

封闭爬取（NFR-1）：不做开放发现，只跟种子配置的入口页 + 一层过滤后的子页。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WebPage
from app.services.failed_jobs import schedule_retry
from app.sources_config import CrawlSeed, SourcesConfig, load_sources

log = logging.getLogger("prof-graph.crawler")

USER_AGENT = "prof-graph/0.2 (academic-network-governance; internal)"
ROBOTS_CACHE_SECONDS = 86_400  # per-host robots.txt 缓存 1 天

# 深度 1 跟进的链接模式：成员/师资列表词（中英常见变体）或 中文姓名+称谓
LIST_LINK_RE = re.compile(
    r"成员|师资|毕业生|团队|校友|教师|学生|在读|members?|faculty|people|alumni|students?", re.IGNORECASE
)
CN_NAME_TITLE_RE = re.compile(r"[\u4e00-\u9fff]{2,4}(教授|博士|老师|院士|研究员|副教授|讲师)")

_STRIP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "noscript", "form")


@dataclass
class CrawlReport:
    seeds_done: list[str] = field(default_factory=list)
    pages_new: int = 0
    pages_changed: int = 0
    pages_unchanged: int = 0
    robots_skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def extract_content(html: str) -> tuple[str | None, str]:
    """去导航/脚本取正文。返回 (title, 正文多行文本)。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else None
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
    return title, "\n".join(ln for ln in lines if ln)


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """页面全部 <a>（去 fragment），供同站成员链接过滤。"""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        url = urldefrag(urljoin(base_url, a["href"]))[0]
        out.append((url, a.get_text(strip=True)))
    return out


def is_member_link(url: str, text: str, host: str) -> bool:
    """封闭跟进判据：同 host + 列表词或中文姓名+称谓的链接文本。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.netloc != host:
        return False
    return bool(LIST_LINK_RE.search(text) or CN_NAME_TITLE_RE.search(text))


class RobotsGate:
    """robots.txt per-host 缓存 1 天；robots 不可得视为允许（标准实践）。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._cache: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None]] = {}

    async def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        host = parsed.netloc
        now = time.monotonic()
        cached = self._cache.get(host)
        if cached is None or now - cached[0] > ROBOTS_CACHE_SECONDS:
            parser = await self._fetch(f"{parsed.scheme}://{host}/robots.txt")
            self._cache[host] = (now, parser)
            cached = self._cache[host]
        parser = cached[1]
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    async def _fetch(self, robots_url: str) -> urllib.robotparser.RobotFileParser | None:
        try:
            resp = await self._http.get(robots_url, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser


class HostRateLimiter:
    """同 host 相邻请求间隔 ≥ min_interval 秒（进程内时间戳）。"""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last: dict[str, float] = {}
        self.request_log: list[tuple[str, float]] = []  # 单测观测用

    async def wait(self, host: str) -> None:
        now = time.monotonic()
        last = self._last.get(host)
        if last is not None:
            delay = self.min_interval - (now - last)
            if delay > 0:
                await asyncio.sleep(delay)
        self._last[host] = time.monotonic()
        self.request_log.append((host, time.monotonic()))


class Crawler:
    """按 sources.yaml 种子定向爬取，快照入库 web_pages。http 可注入以便单测。"""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        sources: SourcesConfig | None = None,
        rate_limit_seconds: float | None = None,
    ) -> None:
        self._sources = sources or load_sources()
        self._http = http
        self._limiter = HostRateLimiter(
            self._sources.rate_limit_seconds if rate_limit_seconds is None else rate_limit_seconds
        )
        self._active_client: httpx.AsyncClient | None = None
        self._robots: RobotsGate | None = None

    def _client(self) -> httpx.AsyncClient:
        return self._http or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )

    async def run(self, session: AsyncSession) -> CrawlReport:
        report = CrawlReport()
        own = self._http is None
        client = self._client()
        self._active_client = client
        self._robots = RobotsGate(client)
        try:
            for seed in self._sources.seeds:
                try:
                    await self._crawl_seed(session, seed, report)
                except Exception as e:  # noqa: BLE001 — 种子级兜底，不中断其他种子
                    log.exception("种子 %s 爬取异常", seed.id)
                    await schedule_retry(session, "web_crawl", seed.url, f"{type(e).__name__}: {e}")
                    report.failed.append(seed.url)
                await session.commit()
        finally:
            if own:
                await client.aclose()
        return report

    async def _crawl_seed(self, session: AsyncSession, seed: CrawlSeed, report: CrawlReport) -> None:
        if not await self._robots.allowed(seed.url):
            log.warning("robots.txt 禁止，跳过：%s（种子 %s）", seed.url, seed.id)
            report.robots_skipped.append(seed.url)
            return
        if not await _page_due(session, seed.url, self._sources.recrawl_days):
            return  # 未到期，本轮不重爬
        report.seeds_done.append(seed.id)
        html = await self._fetch_or_fail(session, seed.url, report)
        if html is None:
            return
        self._count(session, report, await self._snapshot(session, seed, seed.url, html))

        # 深度 1：仅同 host 且命中成员链接模式的子页，未到期的不重爬
        host = urlparse(seed.url).netloc
        for url, text in extract_links(html, seed.url):
            if url == seed.url or not is_member_link(url, text, host):
                continue
            if not await self._robots.allowed(url):
                report.robots_skipped.append(url)
                continue
            if not await _page_due(session, url, self._sources.recrawl_days):
                continue
            sub_html = await self._fetch_or_fail(session, url, report)
            if sub_html is not None:
                self._count(session, report, await self._snapshot(session, seed, url, sub_html))
        # 深度 >1 不跟（封闭爬取）

    def _count(self, session: AsyncSession, report: CrawlReport, outcome: str) -> None:
        if outcome == "new":
            report.pages_new += 1
        elif outcome == "changed":
            report.pages_changed += 1
        else:
            report.pages_unchanged += 1

    async def _fetch_or_fail(
        self, session: AsyncSession, url: str, report: CrawlReport
    ) -> str | None:
        """限速取页。失败写 failed_jobs 并返回 None（调用方继续下一页）。"""
        await self._limiter.wait(urlparse(url).netloc)
        try:
            resp = await self._active_client.get(
                url, headers={"User-Agent": USER_AGENT}, follow_redirects=True
            )
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError as e:
            await schedule_retry(session, "web_crawl", url, f"{type(e).__name__}: {e}")
            report.failed.append(url)
            return None

    async def _snapshot(
        self, session: AsyncSession, seed: CrawlSeed, url: str, html: str
    ) -> str:
        """upsert web_pages：新页入库；内容变化才置回 pending_extraction（AC-9 幂等）。"""
        title, content = extract_content(html)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = (
            await session.execute(select(WebPage).where(WebPage.url == url))
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                WebPage(
                    url=url,
                    seed_id=seed.id,
                    page_type=seed.page_type,
                    title=title,
                    content_text=content,
                    content_hash=content_hash,
                    status="pending_extraction",
                )
            )
            return "new"
        existing.fetched_at = dt.datetime.now(dt.timezone.utc)
        if existing.content_hash == content_hash:
            return "unchanged"  # 内容没变：不重置状态，不触发重抽取
        existing.title = title or existing.title
        existing.content_text = content
        existing.content_hash = content_hash
        existing.status = "pending_extraction"
        return "changed"


async def _page_due(session: AsyncSession, url: str, recrawl_days: int) -> bool:
    """新页（无快照）全爬；老页按 recrawl_days 到期。"""
    fetched = (
        await session.execute(select(WebPage.fetched_at).where(WebPage.url == url))
    ).scalar_one_or_none()
    if fetched is None:
        return True
    return dt.datetime.now(dt.timezone.utc) - fetched >= dt.timedelta(days=recrawl_days)
