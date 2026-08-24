"""数据库引擎与会话（plan §2）。"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.settings import settings

DATABASE_URL = settings.database_url

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖（测试用 dependency_overrides 注入测试会话）。"""
    async with SessionLocal() as session:
        yield session


class Base(DeclarativeBase):
    pass
