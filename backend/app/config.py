"""Concord configuration — loads from environment variables via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Database ──────────────────────────────────────────────
    database_url: str = "postgresql+psycopg://concord:concord@localhost:5432/concord"
    sync_database_url: str = (
        "postgresql+psycopg://concord:concord@localhost:5432/concord"
    )

    # ── Redis ─────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── JWT ───────────────────────────────────────────────────
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # ── Execution sandbox ─────────────────────────────────────
    execution_timeout_seconds: int = 30
    execution_memory_limit: str = "256m"
    execution_cpu_limit: int = 1
    execution_pids_limit: int = 64

    # ── CRDT persistence ─────────────────────────────────────
    crdt_flush_interval_seconds: int = 30
    crdt_max_update_bytes: int = 5 * 1024 * 1024
    crdt_checkpoint_ttl_seconds: int = 7 * 24 * 60 * 60


settings = Settings()
