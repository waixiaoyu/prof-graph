"""M2-T9 RSS 采集器测试：预筛 / 去重 / 源级容错 / OQ-2 自动停用。"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx
from sqlalchemy import select

import app.services.news_collector as nc
from app.models import FailedJob, NewsItem
from app.services.news_collector import (
    collect_news,
    has_signal,
    normalize_entry_url,
    parse_feed,
)
from app.sources_config import RssSource, SourcesConfig

FEED_URL = "https://news.example.com/feed.xml"
OTHER_URL = "https://blog.example.com/rss.xml"


def _rss(*items: str) -> str:
    body = "\n".join(items)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>AI 资讯</title>
{body}
</channel></rss>"""


ITEM_SIGNAL = """
<item><title>张伟教授团队获批国家重点研发计划项目</title>
<link>https://news.example.com/a/1.html</link>
<description>联合实验室签约报道</description>
<pubDate>Mon, 24 Aug 2026 08:00:00 GMT</pubDate></item>"""

ITEM_NO_SIGNAL = """
<item><title>本周学术例会时间调整</title>
<link>https://news.example.com/a/2.html</link>
<description>周五下午 2 点</description>
<pubDate>Tue, 25 Aug 2026 08:00:00 GMT</pubDate></item>"""

ITEM_DUP_IN_BATCH = """
<item><title>李娜研究员入选青年人才计划课题</title>
<link>https://news.example.com/a/1.html#comment</link>
<description>重复链接（fragment 不同）</description></item>"""


def _cfg(*rss: RssSource) -> SourcesConfig:
    return SourcesConfig(rss=tuple(rss), seeds=())


def _src(id_: str = "ai-news", url: str = FEED_URL) -> RssSource:
    return RssSource(id=id_, url=url, tier="known_media")


@pytest.fixture(autouse=True)
def _reset_source_state():
    """OQ-2 停用态是进程内存态，测试间必须复位。"""
    nc._FAIL_COUNTS.clear()
    nc._DISABLED.clear()
    yield
    nc._FAIL_COUNTS.clear()
    nc._DISABLED.clear()


# ---------- 纯函数 ----------


def test_has_signal_patterns() -> None:
    assert has_signal("张伟教授获资助", "")
    assert has_signal("", "合作签约仪式举行")
    assert not has_signal("本周例会通知", "时间调整")
    assert not has_signal("Professor Zhang got funded", "")


def test_normalize_entry_url() -> None:
    assert normalize_entry_url(" https://x.example/a#frag ", None) == "https://x.example/a"
    assert normalize_entry_url(None, "guid-123") == "guid-123"
    assert normalize_entry_url("  ", "  ") is None


def test_parse_feed_fields() -> None:
    items = parse_feed(_rss(ITEM_SIGNAL, ITEM_NO_SIGNAL))
    assert [i["url"] for i in items] == [
        "https://news.example.com/a/1.html",
        "https://news.example.com/a/2.html",
    ]
    assert items[0]["published_at"] == dt.datetime(2026, 8, 24, 8, 0, tzinfo=dt.timezone.utc)
    assert items[0]["rss_entry"]["title"].startswith("张伟教授")
    # 缺 title 的条目跳过
    bad = """<item><link>https://news.example.com/a/9.html</link></item>"""
    assert parse_feed(_rss(bad)) == []


# ---------- 入库 / 预筛 / 去重 ----------


@respx.mock
async def test_collect_news_inserts_and_screens(db_session):
    """有信号 pending_screen；无信号 screened_no_signal 直接短路（不进 GLM）。"""
    respx.get(FEED_URL).respond(text=_rss(ITEM_SIGNAL, ITEM_NO_SIGNAL))
    report = await collect_news(db_session, http=httpx.AsyncClient(), sources=_cfg(_src()))

    assert report.sources_ok == ["ai-news"] and not report.sources_failed
    assert report.added == 2 and report.screened_no_signal == 1
    rows = {
        r.url: r
        for r in (await db_session.execute(select(NewsItem).order_by(NewsItem.id))).scalars()
    }
    assert set(rows) == {"https://news.example.com/a/1.html", "https://news.example.com/a/2.html"}
    assert rows["https://news.example.com/a/1.html"].status == "pending_screen"
    assert rows["https://news.example.com/a/2.html"].status == "screened_no_signal"
    assert rows["https://news.example.com/a/1.html"].rss_entry["summary"] == "联合实验室签约报道"
    assert rows["https://news.example.com/a/1.html"].source_id == "ai-news"


@respx.mock
async def test_collect_news_dedup(db_session):
    """同 URL 重复条目：批内去重 + 库级 on_conflict 跳过，均不重复计数。"""
    respx.get(FEED_URL).respond(text=_rss(ITEM_SIGNAL, ITEM_DUP_IN_BATCH))
    report = await collect_news(db_session, http=httpx.AsyncClient(), sources=_cfg(_src()))
    assert report.added == 1 and report.skipped_dup == 1  # 批内 fragment 归一去重

    report2 = await collect_news(db_session, http=httpx.AsyncClient(), sources=_cfg(_src()))
    assert report2.added == 0 and report2.skipped_dup == 2
    total = len((await db_session.execute(select(NewsItem))).scalars().all())
    assert total == 1


@respx.mock
async def test_missing_pubdate_falls_back_to_now(db_session):
    item = """<item><title>王强副教授课题</title><link>https://news.example.com/a/3.html</link></item>"""
    respx.get(FEED_URL).respond(text=_rss(item))
    await collect_news(db_session, http=httpx.AsyncClient(), sources=_cfg(_src()))
    row = (await db_session.execute(select(NewsItem))).scalar_one()
    assert row.published_at is not None and row.published_at.tzinfo is not None


# ---------- 源级容错与 OQ-2 自动停用 ----------


@respx.mock
async def test_source_failure_does_not_stop_batch(db_session):
    """单源 500：写 failed_jobs（news_fetch），其余源继续。"""
    respx.get(FEED_URL).respond(status_code=500)
    respx.get(OTHER_URL).respond(
        text=_rss(
            """
<item><title>赵敏研究员主持联合实验室</title>
<link>https://blog.example.com/b/1.html</link></item>"""
        )
    )
    report = await collect_news(
        db_session,
        http=httpx.AsyncClient(),
        sources=_cfg(_src(), RssSource(id="blog", url=OTHER_URL, tier="other")),
    )
    assert report.sources_failed == ["ai-news"] and report.sources_ok == ["blog"]
    assert report.added == 1

    job = (
        await db_session.execute(select(FailedJob).where(FailedJob.job_type == "news_fetch"))
    ).scalar_one()
    assert job.target == FEED_URL and job.status == "retrying"


@respx.mock
async def test_auto_disable_after_consecutive_failures(db_session):
    """OQ-2：连续 3 次失败自动停用；第 4 轮不再发请求。"""
    route = respx.get(FEED_URL).respond(status_code=500)
    for i in range(3):
        report = await collect_news(db_session, http=httpx.AsyncClient(), sources=_cfg(_src()))
        assert report.sources_failed == ["ai-news"]
    assert route.call_count == 3
    assert nc.rss_source_states(_cfg(_src()))[0]["disabled"] is True

    report4 = await collect_news(db_session, http=httpx.AsyncClient(), sources=_cfg(_src()))
    assert route.call_count == 3  # 停用后未再拉取
    assert report4.sources_skipped_disabled == ["ai-news"] and not report4.sources_failed


@respx.mock
async def test_success_resets_failure_count(db_session):
    """两次失败后成功一次：计数复位，不再累积到停用阈值。"""
    respx.route(url__startswith=FEED_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(500), httpx.Response(200, text=_rss(ITEM_SIGNAL))]
    )
    for _ in range(2):
        await collect_news(db_session, http=httpx.AsyncClient(), sources=_cfg(_src()))
    await collect_news(db_session, http=httpx.AsyncClient(), sources=_cfg(_src()))
    assert nc._FAIL_COUNTS["ai-news"] == 0 and "ai-news" not in nc._DISABLED

    # 再失败一次仍从 1 计起，未达阈值
    respx.get(FEED_URL).respond(status_code=500)
    await collect_news(db_session, http=httpx.AsyncClient(), sources=_cfg(_src()))
    assert "ai-news" not in nc._DISABLED
