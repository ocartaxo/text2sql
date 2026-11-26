from __future__ import annotations

import json
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from text2sql.domain.models import (
    AccessPolicy,
    ExplainPlanResult,
    QueryExecutionResult,
)

# Erros seguros para expor ao agente no loop de ModelRetry (sem vazar detalhes internos)
RECOVERABLE_PG_ERRORS = (
    asyncpg.UndefinedColumnError,
    asyncpg.UndefinedTableError,
    asyncpg.UndefinedFunctionError,
    asyncpg.GroupingError,
    asyncpg.AmbiguousColumnError,
    asyncpg.DatatypeMismatchError,
    asyncpg.InvalidTextRepresentationError,
    asyncpg.DivisionByZeroError,
    asyncpg.PostgresSyntaxError,
    asyncpg.QueryCanceledError,
    asyncpg.InsufficientPrivilegeError,
)


class SafeDatabaseError(Exception):
    """Erro sanitizado de consulta/permissão seguro para retornar ao LLM."""


class DatabaseInfraError(Exception):
    """Falha de infraestrutura/conexão que não deve disparar retry do agente."""


def _serialize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return "<binary>"
    return value


class PostgresQueryExecutor:
    """Implementa QueryExecutor sobre asyncpg com transação READ ONLY, RLS e EXPLAIN JSON."""

    def __init__(self, pool: asyncpg.Pool, *, work_mem: str = "16MB") -> None:
        self._pool = pool
        self._work_mem = work_mem

    async def _configure_transaction(
        self,
        conn: asyncpg.Connection[Any] | PoolConnectionProxy[Any],
        *,
        tenant_id: str,
        policy: AccessPolicy,
    ) -> None:
        if not tenant_id or not tenant_id.strip():
            raise SafeDatabaseError("Contexto de tenant ausente para execução com RLS.")

        await conn.execute(
            """
            SELECT
                set_config('app.current_tenant', $1, true),
                set_config('statement_timeout', $2, true),
                set_config('lock_timeout', '2000', true),
                set_config('work_mem', $3, true)
            """,
            tenant_id.strip(),
            str(policy.statement_timeout_ms),
            self._work_mem,
        )

    async def explain(
        self,
        sql: str,
        *,
        tenant_id: str,
        policy: AccessPolicy,
    ) -> ExplainPlanResult:
        try:
            async with self._pool.acquire() as conn, conn.transaction(readonly=True):
                await self._configure_transaction(
                    conn,
                    tenant_id=tenant_id,
                    policy=policy,
                )
                raw_plan = await conn.fetchval(f"EXPLAIN (FORMAT JSON) {sql}")
        except RECOVERABLE_PG_ERRORS as exc:
            raise SafeDatabaseError(self._format_pg_error(exc)) from exc
        except asyncpg.PostgresError as exc:
            raise DatabaseInfraError("Erro interno ao planejar consulta no banco.") from exc

        parsed = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
        root_plan: dict[str, Any] = parsed[0]["Plan"]

        return ExplainPlanResult(
            estimated_cost=float(root_plan.get("Total Cost", 0.0)),
            estimated_rows=int(root_plan.get("Plan Rows", 0)),
            raw_plan=root_plan,
        )

    async def execute(
        self,
        sql: str,
        *,
        tenant_id: str,
        policy: AccessPolicy,
    ) -> QueryExecutionResult:
        started = time.perf_counter()
        try:
            async with self._pool.acquire() as conn, conn.transaction(readonly=True):
                await self._configure_transaction(
                    conn,
                    tenant_id=tenant_id,
                    policy=policy,
                )
                records = await conn.fetch(sql)
        except RECOVERABLE_PG_ERRORS as exc:
            raise SafeDatabaseError(self._format_pg_error(exc)) from exc
        except asyncpg.PostgresError as exc:
            raise DatabaseInfraError("Erro interno ao executar consulta no banco.") from exc

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)

        truncated = len(records) > policy.max_rows
        limited_records = records[: policy.max_rows]
        columns = tuple(limited_records[0].keys()) if limited_records else ()
        rows = [
            {key: _serialize_value(val) for key, val in dict(record).items()}
            for record in limited_records
        ]

        return QueryExecutionResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=elapsed_ms,
        )

    @staticmethod
    def _format_pg_error(exc: asyncpg.PostgresError) -> str:
        if isinstance(exc, asyncpg.QueryCanceledError):
            return (
                "Consulta cancelada por timeout (statement_timeout). "
                "Reduza o volume escaneado com filtros de data ou chaves indexadas."
            )
        if isinstance(exc, asyncpg.InsufficientPrivilegeError):
            return "Permissão negada no banco para a tabela ou coluna solicitada."
        # Mensagem primária do PostgreSQL sem stack trace ou dados de conexão
        return str(exc).splitlines()[0]
