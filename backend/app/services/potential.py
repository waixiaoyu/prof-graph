"""潜在关系计算（M3，spec FR-2，plan §2）。

两种方法（均人↔人，纯派生、零 GLM、零外部请求，NFR-1）：
- common_network：不同共同合作者 ≥2 且无活跃直接关系的对（RD-10 探针定门槛——
  ≥1 时 83% 候选本已有直接关系、单一高产枢纽可辐射千对弱信号）
- research_similarity：标签 Jaccard ≥0.3 且双向互认 top-5（RD-11 封顶——
  人均 RS 边恒 ≤5，不随标签孪生集群增长失控）

口径：活跃人 = deleted_at 与 merged_into_id 均 NULL；活跃关系 = deleted_at NULL。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Person, PersonOrg, PersonResearchTag, Relationship

log = logging.getLogger("prof-graph.potential")

COMMON_MIN_COLLABORATORS = 2  # RD-10
RS_JACCARD_MIN = 0.3  # RD-11 探针定阈（J 分布双峰，0.3 位于谷底）
RS_TOP_K = 5  # RD-11 人均 RS 边封顶
METHOD_COMMON = "common_network"
METHOD_RS = "research_similarity"


def clamp_confidence(v: float) -> float:
    """spec FR-1.1：置信度锁在 [0.1, 0.7]。"""
    return round(min(0.7, max(0.1, v)), 2)


@dataclass(frozen=True)
class PotentialRow:
    a: int
    b: int
    method: str
    confidence: float
    reason: str
    signals: dict


@dataclass
class Network:
    """潜在关系计算的输入快照（一次载入，两方法共用）。"""

    names: dict[int, str]                                    # 活跃人 id -> 姓名
    adj: dict[int, set[int]]                                 # 活跃直接关系邻接表
    direct: set[tuple[int, int]]                             # 活跃直接关系对（a<b）
    orgs: dict[int, set[int]]                                # 活跃人机构归属
    tags: dict[int, set[str]]                                # 活跃人研究方向标签（小写）


async def load_network(session: AsyncSession) -> Network:
    """载入活跃网络（FR-3.2：墓碑人/墓碑关系不参与）。"""
    persons = (
        await session.execute(
            select(Person.id, Person.name).where(
                Person.deleted_at.is_(None), Person.merged_into_id.is_(None)
            )
        )
    ).all()
    names = {pid: name for pid, name in persons}
    active_ids = set(names)

    adj: dict[int, set[int]] = defaultdict(set)
    direct: set[tuple[int, int]] = set()
    for a, b in (
        await session.execute(
            select(Relationship.person_a_id, Relationship.person_b_id).where(
                Relationship.deleted_at.is_(None)
            )
        )
    ).all():
        if a not in active_ids or b not in active_ids:
            continue
        adj[a].add(b)
        adj[b].add(a)
        direct.add((a, b))  # 存量约束 person_a_id < person_b_id

    orgs: dict[int, set[int]] = defaultdict(set)
    for pid, org_id in (
        await session.execute(
            select(PersonOrg.person_id, PersonOrg.org_id).where(
                PersonOrg.person_id.in_(active_ids)
            )
        )
    ).all():
        orgs[pid].add(org_id)

    tags: dict[int, set[str]] = defaultdict(set)
    for pid, tag in (
        await session.execute(
            select(PersonResearchTag.person_id, PersonResearchTag.tag).where(
                PersonResearchTag.person_id.in_(active_ids)
            )
        )
    ).all():
        tags[pid].add(tag.lower())  # RD-2：抽取端已归一，此处兜底大小写

    return Network(names=names, adj=adj, direct=direct, orgs=orgs, tags=tags)


def compute_common_network(net: Network) -> list[PotentialRow]:
    """FR-2.1：不同共同合作者 ≥2 且无活跃直接关系的对。

    置信度（plan §2.1，docs/02 公式沿用；|common|≥2 时网络距离恒为 2）：
    0.5×min(n/5,1) + 0.3×(1/3) + 0.2×org_sim，clamp 到 [0.1, 0.7]。
    """
    # 每个共同合作者 c 给其邻居的两两组合各贡献 1 个不同共同者
    pair_common: dict[tuple[int, int], set[int]] = defaultdict(set)
    for _c, neigh in net.adj.items():
        if len(neigh) < 2:
            continue
        for a, b in combinations(sorted(neigh), 2):
            pair_common[(a, b)].add(_c)

    rows: list[PotentialRow] = []
    for (a, b), common in pair_common.items():
        if len(common) < COMMON_MIN_COLLABORATORS or (a, b) in net.direct:
            continue
        org_sim = 1.0 if net.orgs.get(a, set()) & net.orgs.get(b, set()) else 0.5
        conf = clamp_confidence(
            0.5 * min(len(common) / 5, 1.0) + 0.3 * (1 / 3) + 0.2 * org_sim
        )
        names = [net.names.get(c, str(c)) for c in sorted(common)]
        rows.append(
            PotentialRow(
                a=a,
                b=b,
                method=METHOD_COMMON,
                confidence=conf,
                reason=f"共同合作者 {len(common)} 人：{'、'.join(names)}",
                signals={
                    "common_collaborators": sorted(common),
                    "common_collaborator_names": names,
                    "count": len(common),
                },
            )
        )
    return rows


def compute_research_similarity(net: Network) -> list[PotentialRow]:
    """FR-2.2：标签 Jaccard ≥0.3 且双向互认 top-5。

    互认封顶（RD-11）：每人将合格对端按 (jaccard 降序, 对方 id 升序) 取前 5，
    对 (a,b) 保留当且仅当双方互选——人均 RS 边恒 ≤5，不随标签孪生集群失控。
    已有活跃直接关系的对不产出（FR-3.1 / RD-3 一律排除）。
    """
    # 倒排索引：标签 -> 持有者；同一标签的持有者两两累积一个重叠标签
    by_tag: dict[str, list[int]] = defaultdict(list)
    for pid, tset in net.tags.items():
        for t in tset:
            by_tag[t].append(pid)
    pair_overlap: dict[tuple[int, int], set[str]] = defaultdict(set)
    for t, pids in by_tag.items():
        if len(pids) < 2:
            continue
        for a, b in combinations(sorted(pids), 2):
            pair_overlap[(a, b)].add(t)

    # 合格对：Jaccard ≥ 阈值 且无活跃直接关系
    qualified: dict[tuple[int, int], tuple[float, list[str]]] = {}
    picks: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for (a, b), overlap in pair_overlap.items():
        if (a, b) in net.direct:
            continue
        union = len(net.tags[a]) + len(net.tags[b]) - len(overlap)
        jaccard = len(overlap) / union
        if jaccard < RS_JACCARD_MIN:
            continue
        qualified[(a, b)] = (jaccard, sorted(overlap))
        picks[a].append((jaccard, b))
        picks[b].append((jaccard, a))

    chosen: dict[int, set[int]] = {}
    for pid, lst in picks.items():
        lst.sort(key=lambda x: (-x[0], x[1]))
        chosen[pid] = {p for _, p in lst[:RS_TOP_K]}

    rows: list[PotentialRow] = []
    for (a, b), (jaccard, overlap) in sorted(qualified.items()):
        if b not in chosen.get(a, set()) or a not in chosen.get(b, set()):
            continue
        conf = clamp_confidence(0.7 * jaccard + 0.3 * min(len(overlap) / 5, 1.0))
        rows.append(
            PotentialRow(
                a=a,
                b=b,
                method=METHOD_RS,
                confidence=conf,
                reason=f"研究方向相似（{jaccard:.2f}）：{'、'.join(overlap)}",
                signals={
                    "overlap_tags": overlap,
                    "jaccard": round(jaccard, 4),
                    "tags_a": len(net.tags[a]),
                    "tags_b": len(net.tags[b]),
                },
            )
        )
    return rows
