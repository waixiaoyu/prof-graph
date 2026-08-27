"""M2-T12 实跑：RSS 资讯链（news_collect → news_link）真实拉取入库（AC-3/AC-8 验收）。

用法：backend 下 `./.venv/Scripts/python.exe scripts/run_news.py`
真实网络 + 真实 GLM（backend/.env 的 key；预算走 D1 开发放宽档）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import NewsItem, Project, Relationship  # noqa: E402
from app.services.integrity import check_integrity  # noqa: E402
from app.services.pipeline import run_pipeline  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def main() -> None:
    async with SessionLocal() as session:
        batch = await run_pipeline(session, scope="news")
        print("批次:", batch.batch_id, "stage:", batch.stage, "error:", batch.error)
        print("counts:", json.dumps(batch.counts, ensure_ascii=False, indent=2))

        news_total = (await session.execute(select(func.count()).select_from(NewsItem))).scalar_one()
        news_by_status = dict(
            (await session.execute(
                select(NewsItem.status, func.count()).group_by(NewsItem.status)
            )).all()
        )
        projects = (await session.execute(select(func.count()).select_from(Project))).scalar_one()
        coop = (
            await session.execute(
                select(func.count()).select_from(Relationship)
                .where(Relationship.type == "project_cooperation")
            )
        ).scalar_one()
        print(f"news_items 总数={news_total} 分布={news_by_status}")
        print(f"projects 总数={projects} project_cooperation 关系数={coop}")

        report = await check_integrity(session)
        print("C1-C10:", "全绿" if report["ok"] else "有违例")
        for c in report["checks"]:
            if c["violations"]:
                print("  违例", c["check"], c["violations"], c["sample"])


if __name__ == "__main__":
    asyncio.run(main())
