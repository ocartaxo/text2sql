# src/text2sql/guardrails/rules.py
from __future__ import annotations

from sqlglot import exp

from text2sql.domain.models import SQLGuardrailError
from text2sql.guardrails.base import GuardrailContext

ALLOWED_ROOT_NODES = (exp.Select, exp.Union, exp.Intersect, exp.Except)

FORBIDDEN_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.Copy,
    exp.Command,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Set,
    exp.Into,
    exp.Lock,
    exp.Grant,
)

BLOCKED_FUNCTIONS = frozenset(
    {
        # DoS, sistema de arquivos, rede e manipulação de sessão/RLS
        "pg_sleep",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "lo_import",
        "lo_export",
        "query_to_xml",
        "set_config",
        "current_setting",
        "inet_server_addr",
        # Serialização de registro inteiro (evita bypass de colunas PII por alias de tabela)
        "row_to_json",
        "to_json",
        "to_jsonb",
        "array_to_json",
        "json_build_object",
        "jsonb_build_object",
    }
)


class ReadOnlySelectRule:
    """Garante apenas consultas de leitura e bloqueia DML/DDL em qualquer nível da AST."""

    def apply(self, ctx: GuardrailContext) -> None:
        if not isinstance(ctx.ast, ALLOWED_ROOT_NODES):
            raise SQLGuardrailError(
                code="NON_SELECT_STATEMENT",
                message=f"Apenas consultas SELECT são permitidas (recebido: {type(ctx.ast).__name__}).",
                hint="Reescreva a consulta usando exclusivamente SELECT ou CTE (WITH ... SELECT).",
            )

        for node in ctx.ast.walk():
            if isinstance(node, FORBIDDEN_NODES):
                raise SQLGuardrailError(
                    code="FORBIDDEN_SQL_OPERATION",
                    message=f"Operação proibida detectada na AST: {type(node).__name__}.",
                    hint="Remova qualquer cláusula de escrita, bloqueio (FOR UPDATE) ou comando administrativo.",
                )


class AllowedTablesRule:
    """Impõe allowlist de tabelas e bloqueia catálogos internos (pg_catalog, information_schema)."""

    def apply(self, ctx: GuardrailContext) -> None:
        cte_names = {
            cte.alias_or_name.lower() for cte in ctx.ast.find_all(exp.CTE) if cte.alias_or_name
        }
        allowed_tables = {t.lower() for t in ctx.policy.allowed_tables}
        allowed_schemas = {s.lower() for s in ctx.policy.allowed_schemas}

        for table in ctx.ast.find_all(exp.Table):
            table_name = table.name.lower()
            schema_name = table.db.lower() if table.db else ""

            if not schema_name and table_name in cte_names:
                continue

            if schema_name and schema_name not in allowed_schemas:
                raise SQLGuardrailError(
                    code="FORBIDDEN_SCHEMA",
                    message=f"Acesso ao schema '{schema_name}' não é permitido.",
                    hint=f"Use apenas tabelas dos schemas autorizados: {sorted(allowed_schemas)}.",
                )

            if table_name not in allowed_tables:
                raise SQLGuardrailError(
                    code="TABLE_NOT_ALLOWED",
                    message=(
                        f"A tabela '{table_name}' não existe ou não está autorizada "
                        f"para o perfil '{ctx.policy.role}'."
                    ),
                    hint="Consulte list_allowed_tables() para verificar os nomes exatos disponíveis.",
                )

            ctx.referenced_tables.add(table_name)


class BlockedFunctionsRule:
    """Bloqueia funções PostgreSQL perigosas (DoS, leitura de arquivos, SSRF, config e PII)."""

    def apply(self, ctx: GuardrailContext) -> None:
        for node in ctx.ast.walk():
            if isinstance(node, exp.Anonymous):
                func_name = node.name.lower()
            elif isinstance(node, exp.Func):
                func_name = node.sql_name().lower()
            else:
                continue

            if func_name in BLOCKED_FUNCTIONS:
                raise SQLGuardrailError(
                    code="BLOCKED_FUNCTION",
                    message=f"Função '{func_name}' bloqueada por política de segurança.",
                    hint="Use apenas funções padrão de agregação, data e string.",
                )


class BlockedColumnsAndStarRule:
    """Impede vazamento de colunas PII e bloqueia SELECT * fora de COUNT(*)."""

    def apply(self, ctx: GuardrailContext) -> None:
        blocked_cols = {c.lower() for c in ctx.policy.blocked_columns}

        for star in ctx.ast.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                raise SQLGuardrailError(
                    code="SELECT_STAR_FORBIDDEN",
                    message="SELECT * não é permitido para evitar exposição acidental de dados sensíveis.",
                    hint="Liste explicitamente apenas as colunas necessárias para responder à pergunta.",
                )

        for col in ctx.ast.find_all(exp.Column):
            col_name = col.name.lower()
            if col_name in blocked_cols:
                raise SQLGuardrailError(
                    code="BLOCKED_COLUMN",
                    message=f"A coluna '{col_name}' contém dados restritos (PII) e não pode ser consultada.",
                    hint="Remova essa coluna da projeção, filtros e ordenações.",
                )


class EnforceLimitRule:
    """Injeta ou limita automaticamente o teto de linhas na consulta raiz."""

    def apply(self, ctx: GuardrailContext) -> None:
        max_rows = ctx.policy.max_rows
        limit_node = ctx.ast.args.get("limit")

        if isinstance(limit_node, exp.Limit):
            limit_expr = limit_node.expression
            if (
                isinstance(limit_expr, exp.Literal)
                and limit_expr.is_int
                and int(limit_expr.this) <= max_rows
            ):
                return

        ctx.ast = ctx.ast.limit(max_rows)
        ctx.limit_injected = True
