"""M2-T3 定向爬虫单测：入库指纹 / 幂等重爬 / robots / 限速 / 失败不中断（FR-2.1~2.5）。"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx
from sqlalchemy import select

from app.models import FailedJob, WebPage
from app.services.crawler import Crawler, extract_content, is_member_link
from app.sources_config import CrawlSeed, SourcesConfig

HOST = "lab.example.edu"
ENTRY = f"https://{HOST}/people/"
SUB_PHD = f"https://{HOST}/people/phd-archive/"
SUB_ALUMNI = f"https://{HOST}/people/alumni-archive/"
OUTSIDE = "https://other.example.edu/members"

ROBOT_ALLOW = "User-agent: *\nAllow: /\n"
ROBOT_DISALLOW = "User-agent: *\nDisallow: /\n"


def _sources(recrawl_days: int = 7, rate: float = 2) -> SourcesConfig:
    return SourcesConfig(
        rss=(),
        seeds=(
            CrawlSeed(
                id="seed-1",
                school="某大学",
                org_path="某学院 / 某实验室",
                url=ENTRY,
                page_type="lab_members",
            ),
        ),
        rate_limit_seconds=rate,
        depth_limit=1,
        recrawl_days=recrawl_days,
    )


def _entry_html(contacts: bool = False) -> str:
    return f"""<html><head><title>实验室成员</title></head><body>
    <nav>导航</nav>
    <h1>教师</h1><p>张伟 教授，网络安全</p>
    <a href="/people/phd-archive/">博士在读</a>
    <a href="/people/alumni-archive/">毕业生校友</a>
    <a href="/people/contacts.html">联系方式</a>
    <a href="https://other.example.edu/members">外站成员页</a>
    <script>var x = 1;</script>
    </body></html>"""


SUB_HTML = "<html><head><title>博士名单</title></head><body><p>李雷 博士三年级</p></body></html>"


def _mock_site(robot: str = ROBOT_ALLOW, entry_html: str | None = None, sub_status: int = 200):
    entry_html = entry_html or _entry_html()
    routes = [
        respx.get(f"https://{HOST}/robots.txt").respond(text=robot),
        respx.get(ENTRY).respond(text=entry_html),
        respx.get(SUB_PHD).respond(
            status_code=sub_status, text=SUB_HTML if sub_status == 200 else "err"
        ),
        respx.get(SUB_ALUMNI).respond(text=SUB_HTML),
    ]
    return routes


@respx.mock
async def test_crawl_new_seed_snapshots_pages(db_session):
    _mock_site()
    crawler = Crawler(http=httpx.AsyncClient(), sources=_sources(rate=0.0))
    report = await crawler.run(db_session)

    assert report.seeds_done == ["seed-1"]
    assert report.pages_new == 3  # 入口 + 博士在读 + 校友（contacts 不命中模式、外站不同 host）
    rows = (await db_session.execute(select(WebPage).order_by(WebPage.url))).scalars().all()
    assert [r.url for r in rows] == sorted([ENTRY, SUB_PHD, SUB_ALUMNI])
    for r in rows:
        assert r.content_hash and r.status == "pending_extraction"
        assert r.seed_id == "seed-1" and r.page_type == "lab_members"
        assert "导航" not in (r.content_text or "")  # nav 已剔除
    entry_row = next(r for r in rows if r.url == ENTRY)
    assert entry_row.title == "实验室成员"
    assert "张伟" in entry_row.content_text


@respx.mock
async def test_recrawl_unchanged_keeps_status(db_session):
    """AC-9 幂等：内容不变 → 不重置 extracted 状态、不触发重抽取。"""
    routes = _mock_site()
    crawler = Crawler(http=httpx.AsyncClient(), sources=_sources(rate=0.0))
    await crawler.run(db_session)
    entry = (
        await db_session.execute(select(WebPage).where(WebPage.url == ENTRY))
    ).scalar_one()
    entry.status = "extracted"
    await db_session.commit()
    # 到期重爬：全部页面回拨 8 天（只有入口到期时子页不重爬）
    for row in (await db_session.execute(select(WebPage))).scalars():
        row.fetched_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
    await db_session.commit()

    report = await crawler.run(db_session)
    assert report.pages_unchanged == 3 and report.pages_new == 0 and report.pages_changed == 0
    await db_session.refresh(entry)
    assert entry.status == "extracted"  # 未被重置
    assert routes[1].call_count == 2  # 到期确有重爬（内容比对过）


@respx.mock
async def test_content_changed_resets_pending(db_session):
    _mock_site()
    crawler = Crawler(http=httpx.AsyncClient(), sources=_sources(rate=0.0))
    await crawler.run(db_session)
    entry = (
        await db_session.execute(select(WebPage).where(WebPage.url == ENTRY))
    ).scalar_one()
    entry.status = "extracted"
    entry.fetched_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)
    await db_session.commit()

    # 换 mock：入口页内容变化
    _mock_site(entry_html=_entry_html().replace("张伟 教授", "张伟 教授，新晋"))
    report = await crawler.run(db_session)
    assert report.pages_changed == 1
    await db_session.refresh(entry)
    assert entry.status == "pending_extraction"


@respx.mock
async def test_robots_disallow_skips_seed(db_session):
    _mock_site(robot=ROBOT_DISALLOW)
    crawler = Crawler(http=httpx.AsyncClient(), sources=_sources(rate=0.0))
    report = await crawler.run(db_session)
    assert report.robots_skipped == [ENTRY]
    assert report.pages_new == 0
    assert (await db_session.execute(select(WebPage))).scalars().all() == []


@respx.mock
async def test_not_due_seed_not_refetched(db_session):
    _mock_site()
    crawler = Crawler(http=httpx.AsyncClient(), sources=_sources(rate=0.0))
    await crawler.run(db_session)
    # 全部页面 fetched_at 是刚才 → 未到 7 天，第二轮不发起任何页面请求
    report = await crawler.run(db_session)
    assert report.seeds_done == [] and report.pages_new == 0


@respx.mock
async def test_page_failure_writes_failed_job_and_continues(db_session):
    """单子页 500：写 failed_jobs（web_crawl）但批次继续抓其他子页。"""
    _mock_site(sub_status=500)
    crawler = Crawler(http=httpx.AsyncClient(), sources=_sources(rate=0.0))
    report = await crawler.run(db_session)

    assert report.failed == [SUB_PHD]
    assert report.pages_new == 2  # 入口 + 校友页仍入库
    jobs = (await db_session.execute(select(FailedJob))).scalars().all()
    assert [(j.job_type, j.target, j.status) for j in jobs] == [("web_crawl", SUB_PHD, "retrying")]


@respx.mock
async def test_same_host_requests_spaced_by_rate_limit(db_session):
    """限速机制：同 host 相邻请求间隔 ≥ rate_limit_seconds。"""
    _mock_site()
    rate = 0.08
    crawler = Crawler(http=httpx.AsyncClient(), sources=_sources(rate=rate))
    await crawler.run(db_session)
    stamps = [t for _, t in crawler._limiter.request_log]
    assert len(stamps) == 3  # 入口 + 2 个命中子页（robots 不走限速器）
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= rate - 0.02 for g in gaps)  # 允许调度误差


def test_rate_limit_default_from_config() -> None:
    """NFR-1：sources.yaml 定稿值 2s。"""
    from app.sources_config import load_sources

    assert load_sources().rate_limit_seconds == 2


def test_is_member_link_patterns() -> None:
    assert is_member_link(SUB_PHD, "博士在读", HOST)
    assert is_member_link(SUB_ALUMNI, "毕业生校友", HOST)
    assert is_member_link(f"https://{HOST}/x", "张伟教授", HOST)  # 中文姓名+称谓
    assert is_member_link(f"https://{HOST}/x", "Faculty & People", HOST)
    # 学位档列表词（NISL 实跑：博士生/硕士生/博士后 链接文本）
    assert is_member_link(f"https://{HOST}/people/phd-archive/", "博士生", HOST)
    assert is_member_link(f"https://{HOST}/people/master-archive/", "硕士生", HOST)
    assert is_member_link(f"https://{HOST}/people/postdoc-archive/", "博士后", HOST)
    # 成员路径词 URL（IPADS 实跑：成员名链接无列表词文本）
    assert is_member_link(f"https://{HOST}/pub/members/haibo_chen", "陈海波", HOST)
    assert is_member_link(f"https://{HOST}/pub/members/haibo_chen", "", HOST)
    assert is_member_link(f"https://{HOST}/people/duanhx/", "Haixin Duan (段海新)", HOST)
    assert not is_member_link(f"https://{HOST}/contacts.html", "联系方式", HOST)
    assert not is_member_link(f"https://{HOST}/news/2026.html", "新闻", HOST)
    assert not is_member_link(OUTSIDE, "成员", HOST)  # 外站
    assert not is_member_link("mailto:a@b.c", "成员", HOST)


def test_extract_content_strips_noise() -> None:
    title, content = extract_content(
        "<html><head><title>t</title></head><body><header>hd</header>"
        "<script>bad()</script><p>正文 保留</p><footer>ft</footer></body></html>"
    )
    assert title == "t"
    assert "正文 保留" in content
    assert "bad()" not in content and "hd" not in content and "ft" not in content
