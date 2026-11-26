# src/text2sql/domain/ports.py
from __future__ import annotations

from typing import Protocol

from text2sql.domain.models import (
    AccessPolicy,
    ExplainPlanResult,
    QueryExecutionResult,
    ValidationResult,
)


class SQLValidator(Protocol):
    def validate(self, sql: str, policy: AccessPolicy) -> ValidationResult: ...


class QueryExecutor(Protocol):
    async def explain(
        self,
        sql: str,
        *,
        tenant_id: str,
        policy: AccessPolicy,
    ) -> ExplainPlanResult: ...

    async def execute(
        self,
        sql: str,
        *,
        tenant_id: str,
        policy: AccessPolicy,
    ) -> QueryExecutionResult: ...


class SchemaCatalog(Protocol):
    async def list_tables(self, policy: AccessPolicy) -> list[dict[str, str]]: ...

    async def describe_tables(
        self,
        table_names: list[str],
        policy: AccessPolicy,
        *,
        tenant_id: str | None = None,
    ) -> str: ...
