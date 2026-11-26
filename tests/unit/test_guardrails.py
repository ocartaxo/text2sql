from __future__ import annotations

import pytest

from text2sql.domain.models import AccessPolicy, SQLGuardrailError
from text2sql.guardrails.pipeline import SQLGlotGuardrailPipeline


@pytest.fixture
def analyst_policy() -> AccessPolicy:
    return AccessPolicy(
        role="analyst",
        allowed_schemas=frozenset({"public"}),
        allowed_tables=frozenset({"orders", "order_items", "customers", "products"}),
        blocked_columns=frozenset({"email", "cpf", "password_hash"}),
        max_rows=50,
    )


@pytest.fixture
def pipeline() -> SQLGlotGuardrailPipeline:
    return SQLGlotGuardrailPipeline()


class TestValidQueriesAndLimitEnforcement:
    def test_valid_select_injects_limit_when_missing(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
    ) -> None:
        sql = "SELECT id, total_amount FROM orders WHERE status = 'delivered'"
        result = pipeline.validate(sql, analyst_policy)

        assert result.limit_injected is True
        assert "LIMIT 50" in result.normalized_sql
        assert result.referenced_tables == ("orders",)

    def test_valid_select_preserves_limit_below_cap(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
    ) -> None:
        sql = "SELECT id, total_amount FROM orders ORDER BY created_at DESC LIMIT 10"
        result = pipeline.validate(sql, analyst_policy)

        assert result.limit_injected is False
        assert "LIMIT 10" in result.normalized_sql

    def test_excessive_limit_is_clamped_to_policy_max(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
    ) -> None:
        sql = "SELECT id FROM orders LIMIT 5000"
        result = pipeline.validate(sql, analyst_policy)

        assert result.limit_injected is True
        assert "LIMIT 50" in result.normalized_sql
        assert "5000" not in result.normalized_sql

    def test_allows_cte_and_count_star(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
    ) -> None:
        sql = """
        WITH monthly_orders AS (
            SELECT customer_id, total_amount
            FROM orders
            WHERE status = 'delivered'
        )
        SELECT customer_id, COUNT(*) AS order_count, SUM(total_amount) AS revenue
        FROM monthly_orders
        GROUP BY customer_id
        LIMIT 20
        """
        result = pipeline.validate(sql, analyst_policy)

        assert result.limit_injected is False
        assert result.referenced_tables == ("orders",)
        assert "COUNT(*)" in result.normalized_sql

    def test_allows_union_and_injects_limit(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
    ) -> None:
        sql = """
        SELECT id FROM orders WHERE status = 'delivered'
        UNION ALL
        SELECT id FROM orders WHERE status = 'shipped'
        """
        result = pipeline.validate(sql, analyst_policy)

        assert result.limit_injected is True
        assert result.normalized_sql.endswith("LIMIT 50")


class TestStatementAndWriteBypasses:
    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE orders",
            "DELETE FROM orders WHERE id = 1",
            "UPDATE orders SET status = 'canceled'",
            "INSERT INTO orders (id) VALUES (999)",
            "ALTER TABLE orders ADD COLUMN hacked TEXT",
            "COPY orders TO PROGRAM 'id'",
            "SET ROLE postgres",
            "GRANT ALL ON orders TO PUBLIC",
            "/* harmless comment */ DROP TABLE orders",
        ],
    )
    def test_blocks_non_select_root_statements(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
        sql: str,
    ) -> None:
        with pytest.raises(SQLGuardrailError) as exc_info:
            pipeline.validate(sql, analyst_policy)

        assert exc_info.value.code == "NON_SELECT_STATEMENT"

    @pytest.mark.parametrize(
        "sql",
        [
            # Writable CTE (PostgreSQL permite DML dentro de WITH)
            "WITH wiped AS (DELETE FROM orders RETURNING id) SELECT id FROM wiped",
            "WITH updated AS (UPDATE orders SET status = 'x' RETURNING id) SELECT id FROM updated",
            # Row-level locking em consulta SELECT
            "SELECT id FROM orders FOR UPDATE",
            # Criação implícita de tabela via SELECT INTO
            "SELECT id INTO leaked_orders FROM orders",
        ],
    )
    def test_blocks_hidden_write_or_lock_operations_inside_select(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
        sql: str,
    ) -> None:
        with pytest.raises(SQLGuardrailError) as exc_info:
            pipeline.validate(sql, analyst_policy)

        assert exc_info.value.code == "FORBIDDEN_SQL_OPERATION"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM orders; DROP TABLE orders;",
            "SELECT id FROM orders; SELECT id FROM customers",
        ],
    )
    def test_blocks_multiple_statements(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
        sql: str,
    ) -> None:
        with pytest.raises(SQLGuardrailError) as exc_info:
            pipeline.validate(sql, analyst_policy)

        assert exc_info.value.code == "MULTIPLE_STATEMENTS"

    def test_blocks_empty_and_invalid_syntax(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
    ) -> None:
        with pytest.raises(SQLGuardrailError) as empty_exc:
            pipeline.validate("   ", analyst_policy)
        assert empty_exc.value.code == "EMPTY_SQL"

        with pytest.raises(SQLGuardrailError) as syntax_exc:
            pipeline.validate("SELECT FROM WHERE", analyst_policy)
        assert syntax_exc.value.code == "SQL_SYNTAX_ERROR"


class TestTableSchemaAndFunctionBypasses:
    @pytest.mark.parametrize(
        ("sql", "expected_code"),
        [
            ("SELECT salary FROM payroll", "TABLE_NOT_ALLOWED"),
            # Tabela proibida escondida dentro de CTE
            ("WITH hidden AS (SELECT salary FROM payroll) SELECT salary FROM hidden", "TABLE_NOT_ALLOWED"),
            # Tabela proibida em subconsulta escalar
            ("SELECT id, (SELECT MAX(salary) FROM payroll) FROM orders", "TABLE_NOT_ALLOWED"),
            # Catálogos internos do PostgreSQL
            ("SELECT tablename FROM pg_catalog.pg_tables", "FORBIDDEN_SCHEMA"),
            ("SELECT column_name FROM information_schema.columns", "FORBIDDEN_SCHEMA"),
            ("SELECT id FROM private.orders", "FORBIDDEN_SCHEMA"),
        ],
    )
    def test_enforces_table_and_schema_allowlist(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
        sql: str,
        expected_code: str,
    ) -> None:
        with pytest.raises(SQLGuardrailError) as exc_info:
            pipeline.validate(sql, analyst_policy)

        assert exc_info.value.code == expected_code

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT pg_sleep(10) FROM orders",
            "SELECT /* bypass */ PG_SLEEP(5) FROM orders",
            "SELECT pg_read_file('/etc/passwd') FROM orders",
            "SELECT set_config('app.current_tenant', 'other_tenant', true) FROM orders",
            "SELECT current_setting('app.current_tenant') FROM orders",
            "SELECT id FROM orders WHERE pg_sleep(2) IS NULL",
            "SELECT dblink('host=attacker', 'SELECT 1') FROM orders",
        ],
    )
    def test_blocks_dangerous_postgres_functions(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
        sql: str,
    ) -> None:
        with pytest.raises(SQLGuardrailError) as exc_info:
            pipeline.validate(sql, analyst_policy)

        assert exc_info.value.code == "BLOCKED_FUNCTION"


class TestPIIAndSelectStarBypasses:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM customers",
            "SELECT c.* FROM customers c",
            "WITH cte AS (SELECT * FROM customers) SELECT id FROM cte",
        ],
    )
    def test_blocks_select_star_variations(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
        sql: str,
    ) -> None:
        with pytest.raises(SQLGuardrailError) as exc_info:
            pipeline.validate(sql, analyst_policy)

        assert exc_info.value.code == "SELECT_STAR_FORBIDDEN"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id, email FROM customers",
            "SELECT c.CPF FROM customers c",
            # Inferência lateral via filtro WHERE sem projetar a coluna
            "SELECT id FROM customers WHERE email LIKE '%@empresa.com'",
            # Inferência via ORDER BY
            "SELECT id FROM customers ORDER BY cpf ASC",
            # Coluna bloqueada escondida dentro de CTE
            "WITH c AS (SELECT id, password_hash FROM customers) SELECT id FROM c",
        ],
    )
    def test_blocks_pii_columns_in_any_clause(
        self,
        pipeline: SQLGlotGuardrailPipeline,
        analyst_policy: AccessPolicy,
        sql: str,
    ) -> None:
        with pytest.raises(SQLGuardrailError) as exc_info:
            pipeline.validate(sql, analyst_policy)

        assert exc_info.value.code == "BLOCKED_COLUMN"