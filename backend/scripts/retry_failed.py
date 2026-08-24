"""死信手动重跑 CLI（T13）：uv run python scripts/retry_failed.py [--job-id N]

用法：
  python scripts/retry_failed.py            # 重跑全部 dead 任务
  python scripts/retry_failed.py --job-id 3 # 重跑指定任务
"""
from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from app.db import SessionLocal  # noqa: E402
from app.services.failed_jobs import rerun_dead  # noqa: E402


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重跑 failed_jobs 死信")
    parser.add_argument("--job-id", type=int, default=None, help="指定任务 id（缺省全部 dead）")
    args = parser.parse_args(argv)

    async with SessionLocal() as session:
        stats = await rerun_dead(session, job_id=args.job_id)
    print(
        f"重跑 {stats['rerun']} 个死信：成功 {stats['done']}，"
        f"仍失败 {stats['still_dead']}"
    )
    return 0 if stats["still_dead"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
