"""prof-graph M1 后端入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.filters import router as filters_router
from app.api.graph import router as graph_router

log = logging.getLogger("prof-graph")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.scheduler import build_scheduler

    scheduler = build_scheduler()
    scheduler.start()
    log.info("调度器已启动：%s", [j.id for j in scheduler.get_jobs()])
    yield
    scheduler.shutdown(wait=False)
    log.info("调度器已停止")


app = FastAPI(title="prof-graph", version="0.1.0", lifespan=lifespan)
app.include_router(filters_router)
app.include_router(graph_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
