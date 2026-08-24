"""T4：/api/filters/options —— 筛选器选项（FR-5.2）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.config import load_directions

router = APIRouter(prefix="/api")


@router.get("/filters/options")
async def filter_options() -> dict:
    cfg = load_directions()
    return {
        "directions": [{"id": d.id, "name": d.name} for d in cfg.directions],
        "tracks": [{"id": t.id, "name": t.name} for t in cfg.tracks],
        "arxiv_categories": list(cfg.arxiv_categories),
    }
