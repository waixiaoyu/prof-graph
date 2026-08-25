"""清除 T22/T23 的种子演示数据（seed_demo.py 产物，"不进生产"）。

识别：papers.arxiv_id LIKE '2601.1%'（种子专属假 ID 段，真实采集为 2608.x 等）。
种子人物 = 删除种子论文署名后无任何 paper_authors 残留、且非合并墓碑的人。

步骤：删种子证据/署名/论文 → 删孤儿人物（含队列项/机构归属/标签）→
删种子 failed_jobs → 全量重建关系（范围 = 含中国学者论文）。

用法：backend 目录下  ./.venv/Scripts/python.exe scripts/purge_seed_demo.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def main() -> None:
    from sqlalchemy import text

    from app.db import SessionLocal
    from app.services.linker import run_linker

    async with SessionLocal() as session:

        async def scalar(sql: str) -> int:
            return (await session.execute(text(sql))).scalar()

        samples = (await session.execute(text(
            "SELECT arxiv_id, title FROM papers WHERE arxiv_id LIKE '2601.1%' LIMIT 3"
        ))).all()
        print("样例识别（应为假标题）：", [(r.arxiv_id, r.title[:30]) for r in samples])

        await session.execute(text(
            "DELETE FROM relationship_evidence WHERE paper_id IN"
            " (SELECT id FROM papers WHERE arxiv_id LIKE '2601.1%')"))
        await session.execute(text(
            "DELETE FROM person_org WHERE paper_id IN"
            " (SELECT id FROM papers WHERE arxiv_id LIKE '2601.1%')"))
        await session.execute(text(
            "DELETE FROM paper_authors WHERE paper_id IN"
            " (SELECT id FROM papers WHERE arxiv_id LIKE '2601.1%')"))
        papers = await scalar(
            "WITH d AS (DELETE FROM papers WHERE arxiv_id LIKE '2601.1%' RETURNING 1)"
            " SELECT count(*) FROM d")
        print(f"删除种子论文 {papers} 篇")

        # 全量清空关系（随后按范围重建；也解除人物删除的 FK 依赖）
        old = await scalar("WITH d AS (DELETE FROM relationships RETURNING 1) SELECT count(*) FROM d")
        await session.execute(text("DELETE FROM relationship_evidence"))
        print(f"清空关系 {old} 条")

        # 孤儿人物：无署名残留且非墓碑（真实人物都有 ≥1 署名；墓碑靠 merged_into_id 排除）
        orphans = (await session.execute(text(
            "SELECT id FROM persons p WHERE merged_into_id IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM paper_authors pa WHERE pa.person_id = p.id)"
        ))).scalars().all()
        if orphans:
            # 闭包：指向孤儿集的合并墓碑（种子内合并测试对）一并删除
            ids = set(orphans)
            for _ in range(3):
                extra = (await session.execute(text(
                    f"SELECT id FROM persons WHERE merged_into_id IN ({','.join(map(str, ids))})"
                    f" AND id NOT IN ({','.join(map(str, ids))})"
                ))).scalars().all()
                if not extra:
                    break
                ids.update(extra)
            id_list = ",".join(map(str, ids))
            await session.execute(text(
                f"UPDATE persons SET merged_into_id = NULL"
                f" WHERE merged_into_id IN ({id_list})"))
            queue = await scalar(
                f"WITH d AS (DELETE FROM disambiguation_queue"
                f" WHERE person_a_id IN ({id_list}) OR person_b_id IN ({id_list}) RETURNING 1)"
                f" SELECT count(*) FROM d")
            gone = await scalar(
                f"WITH d AS (DELETE FROM persons WHERE id IN ({id_list}) RETURNING 1)"
                f" SELECT count(*) FROM d")
            print(f"删除孤儿人物（含其合并墓碑）{gone} 个（含队列项 {queue} 条；机构归属/标签随级联删除）")

        jobs = await scalar(
            "WITH d AS (DELETE FROM failed_jobs WHERE target LIKE '2601.%' RETURNING 1)"
            " SELECT count(*) FROM d")
        print(f"删除种子 failed_jobs {jobs} 条")

        await session.commit()
        print("按 M1 范围重建关系…")
        report = await run_linker(session)
        print(f"重建完成：{report}")

        for label, sql in [
            ("persons", "SELECT count(*) FROM persons"),
            ("papers(extracted)", "SELECT count(*) FROM papers WHERE status='extracted'"),
            ("papers(cn)", "SELECT count(*) FROM papers WHERE has_cn_scholar"),
            ("relationships", "SELECT count(*) FROM relationships"),
            ("queue(pending)", "SELECT count(*) FROM disambiguation_queue WHERE status='pending'"),
        ]:
            print(f"{label}: {await scalar(sql)}")


if __name__ == "__main__":
    asyncio.run(main())
