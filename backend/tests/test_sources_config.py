"""M2-T0：sources 配置加载与校验单测。"""
from pathlib import Path

import pytest
import yaml

from app.sources_config import RssSource, load_sources


def _use_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict) -> None:
    p = tmp_path / "s.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    import app.sources_config as mod

    monkeypatch.setattr(mod, "SOURCES_PATH", p)
    mod.load_sources.cache_clear()


GOOD = {
    "rss": [
        {"id": "qbitai", "url": "https://www.qbitai.com/feed", "tier": "known_media", "enabled": True},
        {"id": "b", "url": "https://example.com/rss", "tier": "other"},
    ],
    "crawl": {
        "rate_limit_seconds": 2,
        "depth_limit": 1,
        "recrawl_days": 7,
        "seeds": [
            {
                "id": "thu-nisl-members",
                "school": "清华大学",
                "org_path": "网络科学与网络空间研究院 / 网络与信息安全实验室",
                "url": "https://netsec.ccert.edu.cn/chs/people/",
                "page_type": "lab_members",
            }
        ],
    },
}


def test_load_real_config() -> None:
    cfg = load_sources()
    assert len(cfg.rss) == 3
    assert len(cfg.seeds) == 2
    assert {s.id for s in cfg.rss} == {"qbitai", "jiqizhixin", "xinzhiyuan"}
    assert {s.id for s in cfg.seeds} == {"thu-nisl-members", "sjtu-ipads-members"}
    assert all(s.tier == "known_media" and s.confidence == 0.8 for s in cfg.rss)
    assert all(s.enabled for s in cfg.enabled_rss())
    assert (cfg.rate_limit_seconds, cfg.depth_limit, cfg.recrawl_days) == (2, 1, 7)
    nisl = cfg.seeds[0]
    assert nisl.school == "清华大学" and nisl.page_type == "lab_members"


def test_duplicate_rss_id_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {
        "rss": [
            {"id": "dup", "url": "https://a.com/rss", "tier": "known_media"},
            {"id": "dup", "url": "https://b.com/rss", "tier": "known_media"},
        ],
        "crawl": {"seeds": [dict(GOOD["crawl"]["seeds"][0])]},
    }
    _use_config(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="重复"):
        load_sources()
    load_sources.cache_clear()


def test_duplicate_seed_id_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed = dict(GOOD["crawl"]["seeds"][0])
    bad = {"rss": GOOD["rss"], "crawl": {"seeds": [seed, dict(seed, url="https://x.edu/members")]}}
    _use_config(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="重复"):
        load_sources()
    load_sources.cache_clear()


def test_invalid_tier_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {
        "rss": [{"id": "a", "url": "https://a.com/rss", "tier": "vip"}],
        "crawl": {"seeds": [dict(GOOD["crawl"]["seeds"][0])]},
    }
    _use_config(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="tier 非法"):
        load_sources()
    load_sources.cache_clear()


def test_invalid_page_type_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {
        "rss": GOOD["rss"],
        "crawl": {"seeds": [dict(GOOD["crawl"]["seeds"][0], page_type="staff")]},
    }
    _use_config(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="page_type 非法"):
        load_sources()
    load_sources.cache_clear()


def test_invalid_url_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {
        "rss": [{"id": "a", "url": "not-a-url", "tier": "known_media"}],
        "crawl": {"seeds": [dict(GOOD["crawl"]["seeds"][0])]},
    }
    _use_config(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="url 非法"):
        load_sources()
    load_sources.cache_clear()


def test_empty_seeds_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch, {"rss": GOOD["rss"], "crawl": {"seeds": []}})
    with pytest.raises(ValueError, match="seeds"):
        load_sources()
    load_sources.cache_clear()


def test_seed_missing_org_path_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {
        "rss": GOOD["rss"],
        "crawl": {"seeds": [{"id": "s1", "school": "清华大学", "url": "https://x.edu/p", "page_type": "faculty"}]},
    }
    _use_config(tmp_path, monkeypatch, bad)
    with pytest.raises(ValueError, match="org_path"):
        load_sources()
    load_sources.cache_clear()


def test_valid_config_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch, GOOD)
    cfg = load_sources()
    load_sources.cache_clear()
    assert len(cfg.rss) == 2 and len(cfg.seeds) == 1
    assert isinstance(cfg.rss[0], RssSource)
    # enabled 缺省为 True
    assert cfg.rss[1].enabled is True
    # crawl 参数缺省值
    assert (cfg.rate_limit_seconds, cfg.depth_limit, cfg.recrawl_days) == (2, 1, 7)
