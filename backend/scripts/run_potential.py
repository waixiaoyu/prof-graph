"""潜在关系全量重算 CLI（M3-T4，FR-4.2）：手动触发周任务的等价补跑/验收入口。

用法：
  uv run python scripts/run_potential.py   （或 ./.venv/Scripts/python.exe）
"""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from app.db import SessionLocal  # noqa: E402
from app.services.potential import recompute_potential  # noqa: E402


async def main() -> int:
    async with SessionLocal() as session:
        report = await recompute_potential(session)
    print(
        f"潜在关系重算完成：common_network={report['common_network']}，"
        f"research_similarity={report['research_similarity']}，"
        f"耗时 {report['duration_s']}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
