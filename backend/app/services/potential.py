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
METHOD_COMMON = "common_network"


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

    return Network(names=names, adj=adj, direct=direct, orgs=orgs)


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
