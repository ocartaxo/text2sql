from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest

from text2sql.adapters.catalog import PostgresSchemaCatalog
from text2sql.adapters.postgres import PostgresQueryExecutor, SafeDatabaseError
from text2sql.domain.models import AccessPolicy
from text2sql.domain.policies import get_policy_for_role

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(scope="module")
async def ro_pool() -> AsyncIterator[asyncpg.Pool]:
    dsn = os.getenv("RO_DATABASE_URL")
    if not dsn:
        if os.getenv("CI"):
            pytest.fail("RO_DATABASE_URL não definida no ambiente de CI.")
        pytest.skip("RO_DATABASE_URL não definida; pulando testes de integração PostgreSQL.")

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
def executor(ro_pool: asyncpg.Pool) -> PostgresQueryExecutor:
    return PostgresQueryExecutor(ro_pool)


@pytest.fixture
def catalog(ro_pool: asyncpg.Pool) -> PostgresSchemaCatalog:
    return PostgresSchemaCatalog(ro_pool)


@pytest.fixture
def analyst_policy() -> AccessPolicy:
    return get_policy_for_role("analyst")


class TestReadOnlyRoleEnforcement:
    @pytest.mark.parametrize(
        "write_sql",
        [
            "DELETE FROM public.orders WHERE id = 1",
            "UPDATE public.orders SET status = 'canceled' WHERE id = 1",
            "INSERT INTO public.products (sku, name, category, unit_price) VALUES ('HACK', 'x', 'y', 1.00)",
            "DROP TABLE public.orders",
            "TRUNCATE TABLE public.orders",
            "CREATE TABLE public.hacked (id INT)",
            "WITH wiped AS (DELETE FROM public.orders WHERE id = 1 RETURNING id) SELECT id FROM wiped",
        ],
    )
    async def test_role_blocks_all_write_and_ddl_statements(
        self,
        ro_pool: asyncpg.Pool,
        write_sql: str,
    ) -> None:
        async with ro_pool.acquire() as conn:
            with pytest.raises(
                (asyncpg.ReadOnlySQLTransactionError, asyncpg.InsufficientPrivilegeError)
            ):
                await conn.execute(write_sql)


class TestRowLevelSecurityIsolation:
    @pytest.mark.parametrize("table_name", ["orders", "order_items", "customers"])
    async def test_rls_is_fail_closed_without_tenant_setting(
        self,
        ro_pool: asyncpg.Pool,
        table_name: str,
    ) -> None:
        async with ro_pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                count = await conn.fetchval(f"SELECT COUNT(*) FROM public.{table_name}")

        assert count == 0

    async def test_tenant_a_sees_only_own_orders_and_customers(
        self,
        executor: PostgresQueryExecutor,
        analyst_policy: AccessPolicy,
    ) -> None:
        orders_res = await executor.execute(
            "SELECT order_code, status, total_amount FROM public.orders ORDER BY order_code",
            tenant_id="tenant_a",
            policy=analyst_policy,
        )
        codes = [row["order_code"] for row in orders_res.rows]

        assert len(codes) == 7
        assert all(code.startswith("PED-TENANT-A-") for code in codes)
        assert not any("TENANT-B" in code for code in codes)

        revenue_res = await executor.execute(
            "SELECT SUM(total_amount) AS revenue FROM public.orders WHERE status = 'delivered'",
            tenant_id="tenant_a",
            policy=analyst_policy,
        )
        assert revenue_res.rows == [{"revenue": 4800.0}]

    async def test_tenant_b_sees_only_own_records(
        self,
        executor: PostgresQueryExecutor,
        analyst_policy: AccessPolicy,
    ) -> None:
        customers_res = await executor.execute(
            "SELECT full_name FROM public.customers",
            tenant_id="tenant_b",
            policy=analyst_policy,
        )
        assert customers_res.rows == [{"full_name": "Empresa B Ltda"}]

        orders_res = await executor.execute(
            "SELECT order_code FROM public.orders ORDER BY order_code",
            tenant_id="tenant_b",
            policy=analyst_policy,
        )
        assert [r["order_code"] for r in orders_res.rows] == [
            "PED-TENANT-B-001",
            "PED-TENANT-B-002",
        ]

    async def test_set_config_does_not_leak_across_pooled_transactions(
        self,
        ro_pool: asyncpg.Pool,
    ) -> None:
        async with ro_pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                await conn.execute("SELECT set_config('app.current_tenant', 'tenant_a', true)")
                visible_inside = await conn.fetchval("SELECT COUNT(*) FROM public.orders")
            assert visible_inside == 7

            # Mesma conexão física reutilizada fora da transação anterior deve voltar a 0 linhas
            async with conn.transaction(readonly=True):
                visible_after = await conn.fetchval("SELECT COUNT(*) FROM public.orders")
            assert visible_after == 0

    async def test_explain_plan_succeeds_under_rls(
        self,
        executor: PostgresQueryExecutor,
        analyst_policy: AccessPolicy,
    ) -> None:
        plan = await executor.explain(
            "SELECT id, total_amount FROM public.orders WHERE status = 'delivered' LIMIT 10",
            tenant_id="tenant_a",
            policy=analyst_policy,
        )
        assert plan.estimated_cost > 0.0
        assert isinstance(plan.raw_plan, dict)


class TestColumnTableAndResourceProtections:
    @pytest.mark.parametrize(
        "forbidden_sql",
        [
            "SELECT email FROM public.customers",
            "SELECT cpf FROM public.customers",
            "SELECT password_hash FROM public.customers",
            "SELECT * FROM public.customers",
            "SELECT salary FROM public.payroll",
            "SELECT pg_sleep(1)",
        ],
    )
    async def test_database_blocks_pii_columns_restricted_tables_and_pg_sleep(
        self,
        executor: PostgresQueryExecutor,
        analyst_policy: AccessPolicy,
        forbidden_sql: str,
    ) -> None:
        with pytest.raises(SafeDatabaseError, match="Permissão negada"):
            await executor.execute(
                forbidden_sql,
                tenant_id="tenant_a",
                policy=analyst_policy,
            )

    async def test_statement_timeout_cancels_expensive_query(
        self,
        executor: PostgresQueryExecutor,
    ) -> None:
        tight_policy = AccessPolicy(
            role="analyst",
            allowed_tables=frozenset({"orders"}),
            statement_timeout_ms=50,
        )
        heavy_sql = """
            SELECT COUNT(*) AS total
            FROM generate_series(1, 50000) a
            CROSS JOIN generate_series(1, 50000) b
        """
        with pytest.raises(SafeDatabaseError, match="timeout"):
            await executor.execute(
                heavy_sql,
                tenant_id="tenant_a",
                policy=tight_policy,
            )


class TestCatalogIntrospectionSecurity:
    async def test_catalog_lists_only_permitted_tables_and_omits_payroll(
        self,
        catalog: PostgresSchemaCatalog,
        analyst_policy: AccessPolicy,
    ) -> None:
        tables = await catalog.list_tables(analyst_policy)
        names = {item["table"] for item in tables}

        assert names == {"customers", "order_items", "orders", "products"}
        assert "payroll" not in names

    async def test_catalog_masks_samples_and_omits_pii_and_other_tenants(
        self,
        catalog: PostgresSchemaCatalog,
        analyst_policy: AccessPolicy,
    ) -> None:
        ddl = await catalog.describe_tables(
            ["customers", "payroll"],
            analyst_policy,
            tenant_id="tenant_a",
        )

        # Nenhuma coluna PII ou tabela proibida no DDL
        assert "email" not in ddl.lower()
        assert "cpf" not in ddl.lower()
        assert "password_hash" not in ddl.lower()
        assert "payroll" not in ddl.lower()

        # Dados de tenant_b jamais aparecem nas amostras
        assert "Empresa B" not in ddl

        # Nome do cliente de tenant_a deve aparecer mascarado (não em texto pleno)
        assert "Ana Clara Silva" not in ddl
        assert "***" in ddl