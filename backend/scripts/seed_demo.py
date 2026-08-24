"""演示/压测数据种子（T22 压测 + T23 前端走查用，不进生产）。

用法：
  uv run python scripts/seed_demo.py --persons 1000 --avg-degree 6
  uv run python scripts/seed_demo.py --small        # 60 人小图（页面走查）

生成：社团结构合作网络（组内密集、跨组稀疏）+ 论文/证据 + 消歧待审对 +
token_usage + failed_jobs。默认先清空相关表。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import random
import sys

sys.path.insert(0, ".")

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import (
    DisambiguationQueue,
    FailedJob,
    Organization,
    Paper,
    PaperAuthor,
    Person,
    PersonOrg,
    PersonResearchTag,
    Relationship,
    RelationshipEvidence,
    TokenUsage,
)
from app.utils.names import normalize_name

FIRST = ["Wei", "Li", "Jian", "Xin", "Yu", "Hao", "Ming", "Qiang", "Zhi", "Feng",
         "Anna", "John", "Maria", "David", "Sarah", "Omar", "Lena", "Karl", "Ivy", "Noah"]
LAST = ["Zhang", "Wang", "Chen", "Liu", "Yang", "Huang", "Zhao", "Wu", "Zhou", "Sun",
        "Smith", "Miller", "Garcia", "Brown", "Wilson", "Khan", "Novak", "Weber"]
ORGS = [("Tsinghua University", "tsinghua"), ("Peking University", "peking"),
        ("Zhejiang University", "zhejiang"), ("Shanghai Jiao Tong University", "sjtu"),
        ("MIT", "mit"), ("Stanford University", "stanford"), ("Huawei", "huawei"),
        ("Alibaba Group", "alibaba"), ("Tencent", "tencent"), ("NUS", "nus")]
DIRECTIONS = ["network_autonomy", "network_digital_twin", "intent_based_networking",
              "llm_agent", "multi_agent", "closed_loop_autonomy", "fault_analysis",
              "traffic_prediction", "wireless_5g6g", "distributed_training",
              "gpu_scheduling", "inference_serving"]
TRACKS = ["ADN", "openFuyao", "LLM_Agent"]
TAGS = ["llm agent", "intent-based networking", "traffic prediction", "digital twin",
        "federated learning", "graph neural network", "reinforcement learning"]


def _rng_seed(seed: int) -> random.Random:
    return random.Random(seed)


async def seed(persons_n: int, avg_degree: float, queue_pairs: int, clean: bool) -> None:
    rng = _rng_seed(42)
    async with SessionLocal() as session:
        if clean:
            for table in (RelationshipEvidence, Relationship, DisambiguationQueue,
                          PersonOrg, PersonResearchTag, PaperAuthor, Paper, Person,
                          Organization, FailedJob, TokenUsage):
                await session.execute(delete(table))

        # 机构
        orgs = [Organization(name=n, name_normalized=nn) for n, nn in ORGS]
        session.add_all(orgs)
        await session.flush()

        # 人（社团划分：~每 15 人一组，组内合作密集）
        community_of: dict[int, int] = {}
        names: dict[int, str] = {}
        persons: list[Person] = []
        used_names: set[str] = set()
        i = 0
        while len(persons) < persons_n:
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            if name in used_names:
                name = f"{name} {rng.choice('ABCDEFGH')}"
                if name in used_names:
                    continue
            used_names.add(name)
            p = Person(name=name, name_normalized=normalize_name(name))
            persons.append(p)
            community_of[i] = i // 15
            names[i] = name
            i += 1
        session.add_all(persons)
        await session.flush()
        id_by_idx = {i: p.id for i, p in enumerate(persons)}
        idx_by_id = {v: k for k, v in id_by_idx.items()}

        # 机构归属（70% 的人有一个机构）
        for i in range(persons_n):
            if rng.random() < 0.7:
                session.add(PersonOrg(
                    person_id=id_by_idx[i], org_id=rng.choice(orgs).id,
                    org_confidence=rng.choice([1.0, 0.8]), source="glm",
                ))

        # 合作：组内按度数偏好连接 + 少量跨组
        target_edges = int(persons_n * avg_degree / 2)
        edges: set[tuple[int, int]] = set()
        attempts = 0
        while len(edges) < target_edges and attempts < target_edges * 20:
            attempts += 1
            a = rng.randrange(persons_n)
            if rng.random() < 0.85:  # 组内
                c = community_of[a]
                mates = [j for j in range(persons_n) if community_of[j] == c and j != a]
                b = rng.choice(mates) if mates else rng.randrange(persons_n)
            else:
                b = rng.randrange(persons_n)
            if a == b:
                continue
            edges.add((min(a, b), max(a, b)))

        # 每条边 1-3 篇合作论文（论文只落证据 + 计数，不逐条作者行——压测场景）
        today = dt.date.today()
        person_tags: dict[int, set[str]] = {}
        paper_i = 0
        for a, b in edges:
            pa_id, pb_id = id_by_idx[a], id_by_idx[b]
            coop = rng.choices([1, 2, 3], weights=[70, 20, 10])[0]
            years: list[int] = []
            for _ in range(coop):
                year = rng.randrange(2021, 2027)
                years.append(year)
                paper_i += 1
                paper = Paper(
                    arxiv_id=f"2601.{10000 + paper_i}",
                    title=f"Demo paper {paper_i} on {rng.choice(TAGS)}",
                    abstract="demo",
                    authors_raw=[names[a], names[b]],
                    categories=["cs.AI"],
                    directions=rng.sample(DIRECTIONS, rng.randrange(0, 3)),
                    tracks=rng.sample(TRACKS, rng.randrange(0, 2)),
                    research_tags=rng.sample(TAGS, rng.randrange(0, 3)),
                    status="extracted",
                    published_at=dt.datetime(year, rng.randrange(1, 13), 1,
                                             tzinfo=dt.timezone.utc),
                )
                session.add(paper)
                await session.flush()
                session.add_all([
                    PaperAuthor(paper_id=paper.id, author_seq=0, raw_name=names[a], person_id=pa_id),
                    PaperAuthor(paper_id=paper.id, author_seq=1, raw_name=names[b], person_id=pb_id),
                ])
                for pid in (pa_id, pb_id):
                    tag = rng.choice(TAGS)
                    if tag not in person_tags.setdefault(pid, set()):
                        person_tags[pid].add(tag)
                        session.add(PersonResearchTag(person_id=pid, tag=tag))
                lo, hi = min(pa_id, pb_id), max(pa_id, pb_id)
                rel = (await session.execute(
                    select(Relationship).where(
                        Relationship.person_a_id == lo,
                        Relationship.person_b_id == hi,
                        Relationship.type == "paper_cooperation"))).scalar_one_or_none()
                if rel is None:
                    identity = 0.4 + 0.6 * rng.choice([1.0, 0.8, 0.4])
                    rel = Relationship(
                        person_a_id=lo, person_b_id=hi, type="paper_cooperation",
                        identity_confidence=identity, strength=identity * (0.85 + 0.05 * coop),
                        coop_count=1, time_start=today.replace(year=min(years)),
                        time_end=today.replace(year=max(years)))
                    session.add(rel)
                    await session.flush()
                else:
                    rel.coop_count += 1
                session.add(RelationshipEvidence(relationship_id=rel.id, paper_id=paper.id))

        # 消歧待审：同姓近名对（相似 0.5–0.8）
        for k in range(queue_pairs):
            base = rng.choice(LAST)
            n1, n2 = f"Wei {base}", f"WEI {base}." if k % 2 else f"{rng.choice(FIRST)} {base}"
            p1 = Person(name=n1, name_normalized=normalize_name(n1))
            p2 = Person(name=n2, name_normalized=normalize_name(n2))
            session.add_all([p1, p2])
            await session.flush()
            score = rng.uniform(0.5, 0.8)
            lo, hi = min(p1.id, p2.id), max(p1.id, p2.id)
            session.add(DisambiguationQueue(
                person_a_id=lo, person_b_id=hi, score=round(score, 2),
                score_detail={"name": 0.7, "org": round(rng.uniform(0.4, 1.0), 2),
                              "research": round(rng.uniform(0.2, 0.8), 2),
                              "time": 0.6, "network": 0.2}))

        # 运营指标
        session.add_all([
            TokenUsage(day=today, job_type="ai_fine_filter",
                       input_tokens=210_000, output_tokens=60_000),
            TokenUsage(day=today, job_type="glm_extract",
                       input_tokens=90_000, output_tokens=40_000),
            TokenUsage(day=today - dt.timedelta(days=2), job_type="glm_extract",
                       input_tokens=500_000, output_tokens=120_000),
            FailedJob(job_type="rss_fetch", target="eess.SY", attempt=1, status="retrying",
                      next_retry_at=dt.datetime.now(dt.timezone.utc),
                      error="ConnectTimeout"),
            FailedJob(job_type="glm_extract", target="2601.12345", attempt=3, status="dead",
                      error="GLMParseError"),
        ])
        await session.commit()

        counts = {
            "persons": len(persons), "edges": len(edges),
            "papers": paper_i, "queue": queue_pairs,
        }
        print(f"演示数据完成：{counts}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="灌演示/压测数据")
    parser.add_argument("--persons", type=int, default=200)
    parser.add_argument("--avg-degree", type=float, default=6.0)
    parser.add_argument("--queue", type=int, default=6, help="消歧待审对数")
    parser.add_argument("--small", action="store_true", help="小图模式（60 人，页面走查）")
    parser.add_argument("--no-clean", action="store_true", help="不先清空旧数据")
    args = parser.parse_args()
    if args.small:
        args.persons, args.avg_degree, args.queue = 60, 6.0, 5
    await seed(args.persons, args.avg_degree, args.queue, clean=not args.no_clean)


if __name__ == "__main__":
    asyncio.run(main())
