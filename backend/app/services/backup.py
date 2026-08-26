"""每日全库备份防护网（M1 做实，2026-08-26）。

库中沉淀的是花了钱（GLM 抽取）和人力（人工合并/复核决策）的数据资产，
而 PostgreSQL 是单机 zip 安装、数据目录单点，此前没有任何备份——磁盘
一坏人工成果全部归零。本模块每日 02:30（凌晨管线**之前**，03:00 管线
若把数据跑坏手里有跑前快照）执行 pg_dump 自定义格式（压缩、可
pg_restore 选择性恢复），滚动保留最近 RETENTION 份。

恢复方法（记在案）：pg_restore -h 127.0.0.1 -U prof_graph -d <新库> <dump文件>
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from app.settings import settings

log = logging.getLogger("prof-graph.backup")

RETENTION = 7  # 滚动保留份数

# 测试注入点：monkeypatch 本变量即可不发真实子进程
_spawn = asyncio.create_subprocess_exec

_URL_RE = re.compile(
    r"postgresql\+asyncpg://(?P<user>[^:]+):(?P<password>[^@]+)"
    r"@(?P<host>[^:/]+):(?P<port>\d+)/(?P<db>[^?]+)"
)


def _conn_parts(url: str) -> dict:
    m = _URL_RE.match(url)
    if not m:
        raise ValueError(f"无法解析数据库连接串：{url}")
    return m.groupdict()


def _prune(backup_dir: Path, keep: int = RETENTION) -> int:
    """删除超出保留份数的旧备份，返回删除数。"""
    dumps = sorted(backup_dir.glob("prof_graph_*.dump"))
    stale = dumps[:-keep] if len(dumps) > keep else []
    for p in stale:
        p.unlink(missing_ok=True)
    return len(stale)


async def run_backup() -> Path:
    """执行一次 pg_dump，返回备份文件路径；失败抛异常（由调度层记日志）。"""
    conn = _conn_parts(settings.database_url)
    dump_exe = Path(settings.pg_dump_path)
    if not dump_exe.exists():
        raise FileNotFoundError(f"pg_dump 不存在：{dump_exe}（settings.pg_dump_path）")

    backup_dir = Path(settings.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"prof_graph_{ts}.dump"

    # 密码走环境变量，不落命令行；-Fc 自定义格式压缩
    env = {**os.environ, "PGPASSWORD": conn["password"]}
    proc = await _spawn(
        str(dump_exe), "-Fc",
        "-h", conn["host"], "-p", conn["port"], "-U", conn["user"],
        "-d", conn["db"],
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"pg_dump 失败（exit={proc.returncode}）：{err.decode(errors='replace')[:500]}")
    dest.write_bytes(out)

    pruned = _prune(backup_dir)
    log.info("备份完成：%s（%.1f MB，清理旧备份 %d 份）",
             dest.name, dest.stat().st_size / 1e6, pruned)
    return dest
