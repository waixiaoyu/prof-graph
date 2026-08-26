"""T4：directions 配置加载与校验单测。"""
from pathlib import Path

import pytest
import yaml

from app.config import DirectionsConfig, TagRule, _parse_rules, load_directions


def test_load_real_config() -> None:
    cfg = load_directions()
    assert len(cfg.directions) == 3
    assert len(cfg.tracks) == 12
    assert len(cfg.arxiv_categories) == 26  # 2026-08-26 泛AI拓宽：18 + cs.CL/CV/RO/IR/MM/SD/NE + eess.AS
    ids = [d.id for d in cfg.directions]
    assert {"ADN", "openFuyao", "LLM_Agent"} <= set(ids)
    # 默认赛道与细分赛道
    track_ids = {t.id for t in cfg.tracks}
    assert "network_autonomy" in track_ids and "gpu_scheduling" in track_ids
    # 直留核心集：泛AI类目全进且必须是已采集类目的子集
    assert {"cs.CL", "cs.CV", "cs.RO", "cs.IR", "eess.AS"} <= cfg.ai_core_categories
    assert cfg.ai_core_categories <= set(cfg.arxiv_categories)


def test_core_category_not_collected_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {
        "directions": [{"id": "a", "keywords": ["k"]}],
        "tracks": [],
        "arxiv_categories": ["cs.AI"],
        "ai_keywords": ["ml"],
        "ai_core_categories": ["cs.AI", "cs.CL"],  # cs.CL 未采集
    }
    p = tmp_path / "d.yaml"
    p.write_text(yaml.safe_dump(bad), encoding="utf-8")
    import app.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", p)
    cfgmod.load_directions.cache_clear()
    with pytest.raises(ValueError, match="未采集"):
        cfgmod.load_directions()
    cfgmod.load_directions.cache_clear()


def test_duplicate_id_rejected(tmp_path: Path) -> None:
    data = {"directions": [{"id": "x", "keywords": ["a"]}, {"id": "x", "keywords": ["b"]}]}
    with pytest.raises(ValueError, match="重复"):
        _parse_rules(data["directions"], "directions")


def test_empty_keywords_rejected() -> None:
    with pytest.raises(ValueError, match="关键词为空"):
        _parse_rules([{"id": "x", "keywords": []}], "tracks")


def test_direction_track_id_conflict_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {
        "directions": [{"id": "dup", "keywords": ["a"]}],
        "tracks": [{"id": "dup", "keywords": ["b"]}],
        "arxiv_categories": ["cs.AI"],
        "ai_keywords": ["ml"],
    }
    p = tmp_path / "d.yaml"
    p.write_text(yaml.safe_dump(bad), encoding="utf-8")
    import app.config as cfgmod

    monkeypatch.setattr(cfgmod, "CONFIG_PATH", p)
    cfgmod.load_directions.cache_clear()
    with pytest.raises(ValueError, match="冲突"):
        cfgmod.load_directions()
    cfgmod.load_directions.cache_clear()
