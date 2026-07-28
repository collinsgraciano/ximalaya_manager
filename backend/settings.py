"""应用配置 — 通过环境变量注入。"""

from __future__ import annotations

import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局应用配置。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ─── 数据库连接 ───
    database_url: str = "postgresql://xm_app:changeme@localhost:5432/ximalaya"

    # ─── 自建数据库密码 ───
    postgres_password: str = "changeme_strong_password"

    # ─── Web 服务 ───
    secret_key: str = "dev_secret_key_change_in_production"
    base_url: str = "http://localhost:59388"
    app_password: str = "inriynisse"

    # ─── Colab Worker 认证 ───
    worker_auth_token: str = "changeme_worker_token"


settings = Settings()


def get_dsn() -> str:
    """返回 PostgreSQL DSN 连接串。"""
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        dsn = settings.database_url
    if dsn.startswith("postgresql+psycopg://"):
        dsn = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    return dsn
