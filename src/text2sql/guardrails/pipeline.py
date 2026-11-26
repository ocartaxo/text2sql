from __future__ import annotations

import sqlglot
from sqlglot.errors import ParseError

from text2sql.domain.models import AccessPolicy, SQLGuardrailError, ValidationResult
from text2sql.guardrails.base import GuardrailContext, GuardrailRule
from text2sql.guardrails.rules import (
    AllowedTablesRule,
    BlockedColumnsAndStarRule,
    BlockedFunctionsRule,
    EnforceLimitRule,
    ReadOnlySelectRule,
)


class SQLGlotGuardrailPipeline:
    """Implementa SQLValidator via Chain of Responsibility sobre a AST do PostgreSQL."""

    def __init__(self, rules: tuple[GuardrailRule, ...] | None = None) -> None:
        self._rules: tuple[GuardrailRule, ...] = rules or (
            ReadOnlySelectRule(),
            AllowedTablesRule(),
            BlockedFunctionsRule(),
            BlockedColumnsAndStarRule(),
            EnforceLimitRule(),
        )

    def validate(self, sql: str, policy: AccessPolicy) -> ValidationResult:
        cleaned = sql.strip()
        if not cleaned:
            raise SQLGuardrailError(
                code="EMPTY_SQL",
                message="A consulta SQL está vazia.",
                hint="Forneça uma instrução SELECT válida em PostgreSQL.",
            )

        try:
            statements = [
                stmt for stmt in sqlglot.parse(cleaned, read="postgres") if stmt is not None
            ]
        except ParseError as exc:
            raise SQLGuardrailError(
                code="SQL_SYNTAX_ERROR",
                message=f"Erro de sintaxe PostgreSQL: {exc}",
                hint="Corrija a sintaxe SQL respeitando o dialeto PostgreSQL.",
            ) from exc

        if len(statements) != 1:
            raise SQLGuardrailError(
                code="MULTIPLE_STATEMENTS",
                message=f"Esperada exatamente 1 instrução SQL, recebidas {len(statements)}.",
                hint="Envie uma única query SELECT sem ponto e vírgula encadeando comandos.",
            )

        ctx = GuardrailContext(ast=statements[0], policy=policy)
        for rule in self._rules:
            rule.apply(ctx)

        normalized_sql = ctx.ast.sql(dialect="postgres")
        return ValidationResult(
            original_sql=sql,
            normalized_sql=normalized_sql,
            referenced_tables=tuple(sorted(ctx.referenced_tables)),
            limit_injected=ctx.limit_injected,
        )