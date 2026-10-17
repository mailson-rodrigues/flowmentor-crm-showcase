"""Configuração central da aplicação (12-factor, via variáveis de ambiente / .env)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Aplicação -----------------------------------------------------------
    app_name: str = "Flowmentor CRM"
    environment: Literal["development", "test", "production"] = "development"
    debug: bool = False
    public_base_url: str = "http://localhost:8000"
    cors_origins: list[str] = ["http://localhost:8000"]
    enable_hsts: bool = False  # ligar em produção atrás de TLS

    # --- Banco ---------------------------------------------------------------
    # Produção: postgresql+asyncpg://user:pass@host:5432/flowmentor
    database_url: str = "sqlite+aiosqlite:///./flowmentor.db"
    db_echo: bool = False

    # --- Redis / Event bus ---------------------------------------------------
    redis_url: str | None = None  # se None usa barramento em memória (dev/test)
    event_stream_prefix: str = "flowmentor"
    event_consumer_group: str = "flowmentor-workers"
    run_workers_in_process: bool = True  # em produção rodar `python -m app.worker`

    # --- Segurança -----------------------------------------------------------
    secret_key: str = "change-me-in-production-please-use-64-random-bytes"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    # Chaves KEK para envelope encryption: "kid1:base64key,kid2:base64key" (32 bytes cada).
    # A primeira é a chave ativa; as demais servem apenas para decifrar (rotação).
    kek_keys: str = ""
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15
    rate_limit_per_minute: int = 240
    rate_limit_auth_per_minute: int = 20
    password_min_length: int = 10

    # --- Meta / WhatsApp Cloud API (BSP) ------------------------------------
    meta_mode: Literal["sandbox", "live"] = "sandbox"  # sandbox = não chama a Meta de verdade
    meta_graph_version: str = "v21.0"
    meta_app_id: str = ""
    meta_app_secret: str = ""  # valida X-Hub-Signature-256 e troca code do Embedded Signup
    meta_webhook_verify_token: str = "flowmentor-verify-token"
    meta_embedded_signup_config_id: str = ""
    meta_token_refresh_days_before: int = 7

    # --- Armazenamento de mídia ---------------------------------------------
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

    # --- Regras de negócio ---------------------------------------------------
    discount_auto_approve_max_pct: float = 10.0  # acima disso exige permissão deals:discount_high
    consulting_hourly_rate_ref: float = 300.00
    survey_grace_minutes: int = 3
    survey_suppress_days: int = 7
    survey_ttl_hours: int = 48
    sla_at_risk_ratio: float = 0.8

    # --- Primeiro acesso (seed) ---------------------------------------------
    bootstrap_admin_email: str = "admin@flowmentor.com.br"
    bootstrap_admin_password: str = ""  # vazio = gerada aleatoriamente e registrada no log uma única vez (apenas fora de produção)
    demo_user_password: str = ""  # senha dos usuários de demonstração (SEED_DEMO_DATA=true); vazia = gerada e registrada no log
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
