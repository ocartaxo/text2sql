# src/text2sql/api/main.py
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg
from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider
from pydantic_ai import Agent

from text2sql.adapters.catalog import PostgresSchemaCatalog
from text2sql.adapters.postgres import PostgresQueryExecutor
from text2sql.adapters.telemetry import configure_telemetry, shutdown_telemetry
from text2sql.agent.deps import AgentDeps
from text2sql.agent.orchestrator import AnalystResponse, build_sql_agent
from text2sql.api.routes import router as v1_router
from text2sql.config import Settings, get_settings
from text2sql.guardrails.pipeline import SQLGlotGuardrailPipeline


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    pool: asyncpg.Pool
    agent: Agent[AgentDeps, AnalystResponse]
    validator: SQLGlotGuardrailPipeline
    executor: PostgresQueryExecutor
    catalog: PostgresSchemaCatalog
    glossary_text: str


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()
    provider: TracerProvider | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = await asyncpg.create_pool(
            dsn=cfg.ro_database_url,
            min_size=cfg.db_pool_min_size,
            max_size=cfg.db_pool_max_size,
            command_timeout=cfg.db_command_timeout_s,
        )
        glossary_text = (
            cfg.glossary_path.read_text(encoding="utf-8") if cfg.glossary_path.exists() else ""
        )

        app.state.container = AppContainer(
            settings=cfg,
            pool=pool,
            agent=build_sql_agent(
                model_name=cfg.llm_model,
                system_prompt_path=cfg.system_prompt_path,
            ),
            validator=SQLGlotGuardrailPipeline(),
            executor=PostgresQueryExecutor(pool, work_mem=cfg.db_work_mem),
            catalog=PostgresSchemaCatalog(pool),
            glossary_text=glossary_text,
        )
        try:
            yield
        finally:
            try:
                await pool.close()
            finally:
                shutdown_telemetry(provider)

    app = FastAPI(
        title="Text-to-SQL Agent API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(v1_router, prefix="/v1")

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        container: AppContainer = app.state.container
        async with container.pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok"}

    provider = configure_telemetry(cfg, app)
    return app


app = create_app()
