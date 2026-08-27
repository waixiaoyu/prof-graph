"""M2-T8 实跑：NISL + IPADS 真实种子爬取 → 抽取 → 建链（AC-1/AC-2 验收）。

用法：backend 下 `./.venv/Scripts/python.exe scripts/run_crawl.py`
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
from app.models import Relationship, WebPage  # noqa: E402
from app.services.integrity import check_integrity  # noqa: E402
from app.services.pipeline import run_pipeline  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def main() -> None:
    async with SessionLocal() as session:
        batch = await run_pipeline(session, scope="crawl")
        print("批次:", batch.batch_id, "stage:", batch.stage, "error:", batch.error)
        print("counts:", json.dumps(batch.counts, ensure_ascii=False, indent=2))

        pages_total = (await session.execute(select(func.count()).select_from(WebPage))).scalar_one()
        pages_ok = (
            await session.execute(select(func.count()).select_from(WebPage).where(WebPage.status == "extracted"))
        ).scalar_one()
        mentor_total = (
            await session.execute(
                select(func.count()).select_from(Relationship).where(Relationship.type == "academic_mentorship")
            )
        ).scalar_one()
        by_subtype = dict(
            (await session.execute(
                select(Relationship.subtype, func.count())
                .where(Relationship.type == "academic_mentorship")
                .group_by(Relationship.subtype)
            )).all()
        )
        print(f"AC-1 web_pages 总数={pages_total}（extracted={pages_ok}）")
        print(f"AC-2 academic_mentorship 总数={mentor_total} 分布={by_subtype}")

        report = await check_integrity(session)
        print("C1-C10:", "全绿" if report["ok"] else "有违例")
        for c in report["checks"]:
            if c["violations"]:
                print("  违例", c["check"], c["violations"], c["sample"])


if __name__ == "__main__":
    asyncio.run(main())
