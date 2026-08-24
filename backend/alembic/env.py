"""Alembic 迁移环境：异步引擎（asyncpg），URL 取 backend/.env 的 DATABASE_URL。"""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from app.db import Base
from app import models  # noqa: F401 确保模型注册

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

FALLBACK_URL = "postgresql+asyncpg://prof_graph:prof_graph_dev@127.0.0.1:5432/prof_graph"


def _database_url() -> str:
    # 优先真实环境变量；否则从 backend/.env 加载（不覆盖已有值）
    if "DATABASE_URL" not in os.environ:
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(env_path):
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip())
    return os.environ.get("DATABASE_URL", FALLBACK_URL)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    connectable = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
