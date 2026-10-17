"""Central application configuration (12-factor, via environment variables / .env)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application ---------------------------------------------------------
    app_name: str = "Flowmentor CRM"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    public_base_url: str = "http://localhost:8000"
    cors_origins: list[str] = ["http://localhost:8000"]
    enable_hsts: bool = False  # enable in production behind TLS

    # --- Banco ---------------------------------------------------------------
    # Production: postgresql+asyncpg://user:pass@host:5432/flowmentor
    database_url: str = "sqlite+aiosqlite:///./flowmentor.db"
    db_echo: bool = False

    # --- Redis / Event bus ---------------------------------------------------
    redis_url: str | None = None  # if None, use the in-memory bus (dev/test)
    event_stream_prefix: str = "flowmentor"
    event_consumer_group: str = "flowmentor-workers"
    run_workers_in_process: bool = True  # in production run `python -m app.worker`

    # --- Security ------------------------------------------------------------
    secret_key: str = "change-me-in-production-please-use-64-random-bytes"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    # KEK keys for envelope encryption: "kid1:base64key,kid2:base64key" (32 bytes each).
    # The first is the active key; the others are only used to decrypt (rotation).
    kek_keys: str = ""
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15
    rate_limit_per_minute: int = 240
    rate_limit_auth_per_minute: int = 20
    password_min_length: int = 10

    # --- Meta / WhatsApp Cloud API (BSP) ------------------------------------
    meta_mode: Literal["sandbox", "live"] = "sandbox"  # sandbox = does not actually call Meta
    meta_graph_version: str = "v21.0"
    meta_app_id: str = ""
    meta_app_secret: str = ""  # validates X-Hub-Signature-256 and exchanges the Embedded Signup code
    meta_webhook_verify_token: str = "flowmentor-verify-token"
    meta_embedded_signup_config_id: str = ""
    meta_token_refresh_days_before: int = 7

    # --- Media storage -------------------------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_path: str = "./storage"
    storage_bucket: str = ""
    storage_s3_endpoint: str | None = None
    storage_s3_region: str = "us-east-1"
    media_url_ttl_seconds: int = 900
    media_retention_days: int = 365

    # --- IA / LLM (opcional) -------------------------------------------------
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-5-5"

    # --- Business rules ------------------------------------------------------
    discount_auto_approve_max_pct: float = 10.0  # above this, requires the deals:discount_high permission
    consulting_hourly_rate_ref: float = 300.00
    survey_grace_minutes: int = 3
    survey_suppress_days: int = 7
    survey_ttl_hours: int = 48
    sla_at_risk_ratio: float = 0.8

    # --- Primeiro acesso (seed) ---------------------------------------------
    bootstrap_admin_email: str = "admin@flowmentor.com.br"
    bootstrap_admin_password: str = ""  # empty = randomly generated and logged once (non-production only)
    demo_user_password: str = ""  # password of the demo users (SEED_DEMO_DATA=true); empty = generated and logged
    bootstrap_org_name: str = "Flowmentor"
    seed_demo_data: bool = False

    @property
    def is_prod(self) -> bool:
        return self.environment == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
