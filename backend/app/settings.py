"""环境配置：backend/.env 统一加载点（pydantic-settings）。

真实环境变量优先于 .env 文件。新配置项一律加在这里，不要散落 os.environ。
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # --- 数据库 ---
    database_url: str = "postgresql+asyncpg://prof_graph:prof_graph_dev@127.0.0.1:5432/prof_graph"
    database_url_test: str = "postgresql+asyncpg://prof_graph:prof_graph_dev@127.0.0.1:5432/prof_graph_test"

    # --- GLM（Anthropic 协议）---
    glm_api_key: str = ""
    glm_model: str = "glm-5.3"
    glm_base_url: str = "https://open.bigmodel.cn/api/anthropic"
    glm_request_timeout_ms: int = 120000

    # --- 熔断预算（T23 联调后校准）---
    token_budget_daily: int = 1_200_000
    token_budget_weekly: int = 6_000_000

    # --- OpenAlex ---
    openalex_mailto: str = "prof-graph@internal.example"

    # --- 采集 ---
    arxiv_rss_base: str = "https://export.arxiv.org/rss"
    backfill_window_days: int = 5

    # --- 备份（防护网）：pg_dump 可执行文件与输出目录 ---
    pg_dump_path: str = r"C:\tools\pg15\pgsql\bin\pg_dump.exe"
    backup_dir: Path = Path(__file__).resolve().parents[1] / "backups"


settings = Settings()
