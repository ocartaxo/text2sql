# src/text2sql/config.py
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPTS_DIR = PACKAGE_DIR / "agent" / "prompts"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # PostgreSQL (conexão exclusiva da aplicação com role READ ONLY)
    ro_database_url: str = Field(
        default="postgresql://text2sql_ro:change_me_in_prod_ro@127.0.0.1:5432/text2sql_ci",
    )
    db_pool_min_size: int = Field(default=2, ge=1)
    db_pool_max_size: int = Field(default=10, ge=1)
    db_command_timeout_s: float = Field(default=10.0, gt=0)
    db_work_mem: str = Field(default="16MB")

    # Modelo LLM e orçamento do agente
    llm_model: str = Field(default="google-gla:gemini-3.8-flash")
    agent_request_limit: int = Field(default=8, ge=1)
    agent_total_tokens_limit: int = Field(default=24_000, ge=1_000)

    # Artefatos versionados de prompt
    system_prompt_path: Path = Field(default=DEFAULT_PROMPTS_DIR / "system_v1.md")
    glossary_path: Path = Field(default=DEFAULT_PROMPTS_DIR / "glossary.yaml")

    # OpenTelemetry / Grafana OTEL-LGTM / Langfuse (via OTLP HTTP)
    enable_otel: bool = Field(default=False)
    otel_service_name: str = Field(default="text2sql-api")
    otel_exporter_otlp_endpoint: str = Field(default="http://127.0.0.1:4318")
    otel_exporter_otlp_headers: str | None = Field(default=None)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()