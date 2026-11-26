from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from text2sql.domain.models import AccessPolicy

DEFAULT_TABLE_DESCRIPTIONS: dict[str, str] = {
    "orders": "Pedidos realizados por clientes (status, valor total e data de criação).",
    "order_items": "Itens individuais de cada pedido com quantidade e preço unitário.",
    "customers": "Cadastro de clientes sem colunas PII (cidade, estado e data de cadastro).",
    "products": "Catálogo de produtos (SKU, nome, categoria e preço unitário).",
}

PARTIAL_MASK_COLUMNS: frozenset[str] = frozenset({"full_name", "name", "customer_name"})
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")


def _quote_ident(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def _mask_sample_value(column_name: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary>"

    if isinstance(value, str):
        sanitized = EMAIL_RE.sub("[REDACTED_EMAIL]", value)
        sanitized = CPF_RE.sub("[REDACTED_CPF]", sanitized)

        if column_name.lower() in PARTIAL_MASK_COLUMNS and len(sanitized) > 3:
            tokens = sanitized.split()
            masked_tokens = [f"{token[:2]}***" if len(token) > 2 else "***" for token in tokens]
            return " ".join(masked_tokens)

        return sanitized[:60] + ("..." if len(sanitized) > 60 else "")

    return value


class PostgresSchemaCatalog:
    """Implementa SchemaCatalog via pg_catalog com amostras mascaradas e isoladas por RLS."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        sample_limit: int = 3,
        table_descriptions: dict[str, str] | None = None,
    ) -> None:
        self._pool = pool
        self._sample_limit = sample_limit
        self._descriptions = {
            **DEFAULT_TABLE_DESCRIPTIONS,
            **(table_descriptions or {}),
        }

    async def list_tables(self, policy: AccessPolicy) -> list[dict[str, str]]:
        allowed_schemas = sorted(s.lower() for s in policy.allowed_schemas)
        allowed_tables = sorted(t.lower() for t in policy.allowed_tables)
        if not allowed_tables:
            return []

        query = """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                obj_description(c.oid, 'pg_class') AS comment
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'v', 'm', 'p')
              AND lower(n.nspname) = ANY($1::text[])
              AND lower(c.relname) = ANY($2::text[])
              AND has_any_column_privilege(current_user, c.oid, 'SELECT')
            ORDER BY n.nspname, c.relname
        """

        async with self._pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                rows = await conn.fetch(query, allowed_schemas, allowed_tables)

        result: list[dict[str, str]] = []
        for row in rows:
            table_name = str(row["table_name"])
            description = (
                str(row["comment"])
                if row["comment"]
                else self._descriptions.get(table_name.lower(), "Tabela de negócio autorizada.")
            )
            result.append(
                {
                    "schema": str(row["schema_name"]),
                    "table": table_name,
                    "description": description,
                }
            )
        return result

    async def describe_tables(
        self,
        table_names: list[str],
        policy: AccessPolicy,
        *,
        tenant_id: str | None = None,
    ) -> str:
        allowed_schemas = sorted(s.lower() for s in policy.allowed_schemas)
        allowed_tables = {t.lower() for t in policy.allowed_tables}
        blocked_cols = {c.lower() for c in policy.blocked_columns}

        requested = sorted(
            {t.strip().lower() for t in table_names if t.strip().lower() in allowed_tables}
        )
        if not requested:
            return "Nenhuma tabela autorizada encontrada para descrever."

        sections: list[str] = []
        async with self._pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                await conn.execute(
                    "SELECT set_config('statement_timeout', $1, true)",
                    str(policy.statement_timeout_ms),
                )
                if tenant_id and tenant_id.strip():
                    await conn.execute(
                        "SELECT set_config('app.current_tenant', $1, true)",
                        tenant_id.strip(),
                    )

                for table_name in requested:
                    section = await self._describe_single_table(
                        conn,
                        table_name=table_name,
                        allowed_schemas=allowed_schemas,
                        blocked_cols=blocked_cols,
                    )
                    if section:
                        sections.append(section)

        return "\n\n".join(sections) if sections else "Nenhuma tabela acessível encontrada."

    async def _describe_single_table(
        self,
        conn: asyncpg.Connection,
        *,
        table_name: str,
        allowed_schemas: list[str],
        blocked_cols: set[str],
    ) -> str | None:
        columns_rows = await conn.fetch(
            """
            SELECT
                n.nspname AS schema_name,
                c.relname AS table_name,
                a.attname AS column_name,
                format_type(a.atttypid, a.atttypmod) AS data_type,
                a.attnotnull AS not_null
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
            WHERE lower(n.nspname) = ANY($1::text[])
              AND lower(c.relname) = $2
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND has_column_privilege(current_user, c.oid, a.attnum, 'SELECT')
            ORDER BY a.attnum
            """,
            allowed_schemas,
            table_name,
        )

        visible_columns = [
            row for row in columns_rows if str(row["column_name"]).lower() not in blocked_cols
        ]
        if not visible_columns:
            return None

        schema_name = str(visible_columns[0]["schema_name"])
        actual_table = str(visible_columns[0]["table_name"])

        fk_rows = await conn.fetch(
            """
            SELECT
                src_col.attname AS column_name,
                ref_ns.nspname AS ref_schema,
                ref_tbl.relname AS ref_table,
                ref_col.attname AS ref_column
            FROM pg_catalog.pg_constraint con
            JOIN pg_catalog.pg_class src_tbl ON src_tbl.oid = con.conrelid
            JOIN pg_catalog.pg_namespace src_ns ON src_ns.oid = src_tbl.relnamespace
            JOIN pg_catalog.pg_class ref_tbl ON ref_tbl.oid = con.confrelid
            JOIN pg_catalog.pg_namespace ref_ns ON ref_ns.oid = ref_tbl.relnamespace
            JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS cols(src_attnum, ref_attnum, ord) ON true
            JOIN pg_catalog.pg_attribute src_col
                ON src_col.attrelid = src_tbl.oid AND src_col.attnum = cols.src_attnum
            JOIN pg_catalog.pg_attribute ref_col
                ON ref_col.attrelid = ref_tbl.oid AND ref_col.attnum = cols.ref_attnum
            WHERE con.contype = 'f'
              AND src_ns.nspname = $1
              AND src_tbl.relname = $2
            ORDER BY con.conname, cols.ord
            """,
            schema_name,
            actual_table,
        )

        col_defs = [
            f"  {row['column_name']} {row['data_type']}" + (" NOT NULL" if row["not_null"] else "")
            for row in visible_columns
        ]
        ddl_lines = [f"CREATE TABLE {schema_name}.{actual_table} ("]
        ddl_lines.append(",\n".join(col_defs))
        ddl_lines.append(");")

        if fk_rows:
            ddl_lines.append("-- Foreign Keys:")
            for fk in fk_rows:
                ddl_lines.append(
                    f"--   {fk['column_name']} -> {fk['ref_schema']}.{fk['ref_table']}.{fk['ref_column']}"
                )

        # Projeção explícita apenas das colunas autorizadas e presentes no pg_catalog
        safe_col_names = [str(row["column_name"]) for row in visible_columns]
        quoted_cols = ", ".join(_quote_ident(col) for col in safe_col_names)
        sample_sql = (
            f"SELECT {quoted_cols} "
            f"FROM {_quote_ident(schema_name)}.{_quote_ident(actual_table)} "
            f"LIMIT $1"
        )
        sample_records = await conn.fetch(sample_sql, self._sample_limit)

        if sample_records:
            ddl_lines.append(f"-- Sample rows (masked, max {self._sample_limit}):")
            for record in sample_records:
                masked = {col: _mask_sample_value(col, record[col]) for col in safe_col_names}
                ddl_lines.append(f"--   {masked}")
        else:
            ddl_lines.append("-- Sample rows: (sem linhas visíveis no contexto atual)")

        return "\n".join(ddl_lines)
