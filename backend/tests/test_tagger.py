"""T8 单测：关键词打标（FR-1.3）。"""
from __future__ import annotations

from app.config import load_directions
from app.models import Paper
from app.services.tagger import _match_tags, run_tagger


def _paper(title: str, abstract: str = "", ai_relevant: bool = True) -> Paper:
    return Paper(
        arxiv_id=title[:12],
        title=title,
        abstract=abstract,
        authors_raw=["A"],
        categories=["cs.NI"],
        ai_relevant=ai_relevant,
    )


def test_match_intent_based_networking() -> None:
    """含 'intent-based networking' → network_autonomy 赛道 + ADN 方向（关键词两处都有）。"""
    p = _paper("Intent-based networking for autonomous driving network")
    directions, tracks = _match_tags(p, load_directions())
    assert "ADN" in directions
    assert "network_autonomy" in tracks
    assert "intent_based_networking" in tracks


def test_match_no_hit_empty() -> None:
    p = _paper("A study of queueing theory", abstract="markov chains and bounds")
    directions, tracks = _match_tags(p, load_directions())
    assert directions == [] and tracks == []


def test_match_multi_tag() -> None:
    """一篇可同时命中方向 + 多赛道。"""
    p = _paper(
        "LLM agent scheduling on GPU cluster with distributed training",
        abstract="multi-agent coordination for distributed training pipelines",
    )
    directions, tracks = _match_tags(p, load_directions())
    assert "LLM_Agent" in directions and "openFuyao" in directions
    assert "multi_agent" in tracks and "distributed_training" in tracks


async def test_run_tagger_writes_fields(db_session) -> None:
    tagged = _paper("Self-healing with closed-loop autonomy and LLM agent")
    untagged = _paper("Generic CS paper title", abstract="some systems work")
    filtered_out = _paper("Dropped paper", ai_relevant=False)
    for p in (tagged, untagged, filtered_out):
        db_session.add(p)
    await db_session.flush()

    report = await run_tagger(db_session)

    assert (report.tagged, report.untouched) == (1, 1)
    await db_session.refresh(tagged)
    await db_session.refresh(untagged)
    await db_session.refresh(filtered_out)
    assert "LLM_Agent" in tagged.directions and "closed_loop_autonomy" in tagged.tracks
    assert untagged.directions == [] and untagged.tracks == []
    assert filtered_out.directions == []  # 被剔除论文不处理
