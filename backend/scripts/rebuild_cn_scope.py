"""M1 范围约束迁移（2026-08-31）：只治理含中国学者的论文。

步骤：
1. papers 加 has_cn_scholar 列（幂等 IF NOT EXISTS）
2. 启发式回填全部已抽取论文的标记
3. 清空 M1 关系（relationships + relationship_evidence），按范围重建
   （重建是确定性的：coop_count/时间范围/证据由 linker 从 paper_authors 重算）

用法：backend 目录下  ./.venv/Scripts/python.exe scripts/rebuild_cn_scope.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


async def main() -> None:
    from sqlalchemy import text

    from app.db import SessionLocal, engine
    from app.services.cn_scope import flag_papers
    from app.services.linker import run_linker

    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE papers ADD COLUMN IF NOT EXISTS has_cn_scholar"
                 " boolean NOT NULL DEFAULT false")
        )
    print("列就绪：papers.has_cn_scholar")

    async with SessionLocal() as session:
        stats = await flag_papers(session)
        total = (await session.execute(
            text("SELECT count(*) FROM papers WHERE status='extracted'")
        )).scalar()
        cn = (await session.execute(
            text("SELECT count(*) FROM papers WHERE status='extracted' AND has_cn_scholar")
        )).scalar()
        print(f"范围回填：扫描 {stats['scanned']} / 新标记 {stats['flagged']}；"
              f"已抽取 {total} 篇中含中国学者 {cn} 篇（{(cn / total * 100 if total else 0):.0f}%）")

        old_rels = (await session.execute(text("SELECT count(*) FROM relationships"))).scalar()
        await session.execute(text("DELETE FROM relationship_evidence"))
        await session.execute(text("DELETE FROM relationships"))
        await session.commit()
        print(f"清空旧关系 {old_rels} 条，按范围重建…")

        report = await run_linker(session)
        print(f"重建完成：{report}")

        rels = (await session.execute(text("SELECT count(*) FROM relationships"))).scalar()
        evs = (await session.execute(text("SELECT count(*) FROM relationship_evidence"))).scalar()
        print(f"当前范围内：关系 {rels} 条 / 证据 {evs} 行")


if __name__ == "__main__":
    asyncio.run(main())
