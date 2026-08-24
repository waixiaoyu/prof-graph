"""directions.yaml 加载器（T4）：启动时校验，进程内缓存。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.environ.get("DIRECTIONS_CONFIG", Path(__file__).resolve().parents[1] / "config" / "directions.yaml"))


@dataclass(frozen=True)
class TagRule:
    id: str
    name: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class DirectionsConfig:
    directions: tuple[TagRule, ...]
    tracks: tuple[TagRule, ...]
    arxiv_categories: tuple[str, ...]

    @property
    def ai_core_categories(self) -> frozenset[str]:
        """第一段规则粗筛的'直接保留'集合（plan §4）。"""
        return frozenset({"cs.AI", "cs.LG", "stat.ML"})

    def keyword_rules(self) -> tuple[TagRule, ...]:
        """打标器用的全部规则（方向 + 赛道）。"""
        return self.directions + self.tracks


def _parse_rules(items: list[dict], kind: str) -> tuple[TagRule, ...]:
    rules: list[TagRule] = []
    seen: set[str] = set()
    for item in items:
        rule_id = item.get("id")
        kws = item.get("keywords") or []
        if not rule_id or rule_id in seen:
            raise ValueError(f"directions.yaml {kind} 段存在缺失或重复的 id：{rule_id!r}")
        if not kws or not all(isinstance(k, str) and k.strip() for k in kws):
            raise ValueError(f"directions.yaml {kind}[{rule_id}] 关键词为空或含非法值")
        seen.add(rule_id)
        rules.append(TagRule(id=rule_id, name=item.get("name_cn") or item.get("name") or rule_id,
                             keywords=tuple(k.lower() for k in kws)))
    return tuple(rules)


@lru_cache(maxsize=1)
def load_directions() -> DirectionsConfig:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    directions = _parse_rules(raw.get("directions") or [], "directions")
    tracks = _parse_rules(raw.get("tracks") or [], "tracks")
    categories = tuple(raw.get("arxiv_categories") or [])
    if not categories:
        raise ValueError("directions.yaml 缺 arxiv_categories")
    dir_ids = {d.id for d in directions}
    track_ids = {t.id for t in tracks}
    if dir_ids & track_ids:
        raise ValueError(f"directions 与 tracks 的 id 冲突：{dir_ids & track_ids}")
    return DirectionsConfig(directions=directions, tracks=tracks, arxiv_categories=categories)
