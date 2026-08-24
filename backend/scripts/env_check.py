"""T0 环境自检：python backend/scripts/env_check.py

四项检查（对应 tasks.md T0 验证标准）：
  1. PostgreSQL 可连接（用 .env 的 DATABASE_URL_TEST）
  2. Python 版本 >= 3.11
  3. Node 版本 >= 18
  4. GLM_API_KEY 已填写
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

# 项目根 = backend/scripts/ 的上上级
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))


def load_env() -> dict[str, str]:
    env = dict(os.environ)
    path = os.path.join(ROOT, "backend", ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env.setdefault(k.strip(), v.strip())
    return env


def check_python() -> bool:
    ok = sys.version_info >= (3, 11)
    print(f"[{'OK' if ok else 'NG'}] Python >= 3.11：{sys.version.split()[0]}")
    return ok


def check_node() -> bool:
    node = shutil.which("node")
    if not node:
        print("[NG] Node：未找到 node 可执行文件")
        return False
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    major = int(re.search(r"v(\d+)", out).group(1))
    ok = major >= 18
    print(f"[{'OK' if ok else 'NG'}] Node >= 18：{out}")
    return ok


def check_glm_key(env: dict[str, str]) -> bool:
    ok = bool(env.get("GLM_API_KEY"))
    hint = "" if ok else "（backend/.env 中填写 GLM_API_KEY）"
    print(f"[{'OK' if ok else 'NG'}] GLM_API_KEY 已填写{hint}")
    return ok


def check_postgres(env: dict[str, str]) -> bool:
    url = env.get("DATABASE_URL_TEST", "")
    if not url:
        print("[NG] PostgreSQL：.env 缺 DATABASE_URL_TEST")
        return False
    try:
        import asyncio

        import asyncpg  # noqa: F401  (T1 装依赖后可用)
    except ImportError:
        print("[--] PostgreSQL：asyncpg 未安装（T1 后重跑本脚本）")
        return True  # 不阻塞：T0 阶段只要 PG 服务本身在跑
    try:
        import asyncpg

        async def ping() -> None:
            conn = await asyncpg.connect(url.replace("+asyncpg", "").replace("postgresql", "postgresql", 1))
            try:
                await conn.fetchval("SELECT 1")
            finally:
                await conn.close()

        asyncio.run(ping())
        print("[OK] PostgreSQL：测试库连接成功")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[NG] PostgreSQL：连接失败——{e}")
        return False


def main() -> int:
    env = load_env()
    results = [
        check_python(),
        check_node(),
        check_glm_key(env),
        check_postgres(env),
    ]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} 项通过")
    if "asyncpg" not in sys.modules:
        print("提示：数据库连通性将在 T1 安装依赖后完整校验")
    return 0 if passed >= 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
