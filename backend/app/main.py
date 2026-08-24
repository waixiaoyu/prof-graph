"""prof-graph M1 后端入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    # T14 将在此启动 APScheduler
    yield
    # T14 将在此关闭 APScheduler


app = FastAPI(title="prof-graph", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
