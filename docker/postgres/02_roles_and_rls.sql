-- 1. Endurece privilégios padrão do schema public e bloqueia pg_sleep para PUBLIC
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION pg_catalog.pg_sleep(double precision) FROM PUBLIC;

-- 2. Cria role dedicada somente leitura (idempotente) sem bypass de RLS
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'text2sql_ro') THEN
        CREATE ROLE text2sql_ro
            LOGIN
            PASSWORD 'change_me_in_prod_ro'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOREPLICATION
            NOBYPASSRLS;
    END IF;
END
$$;

-- 3. Limites rígidos no nível da role (defesa independente da aplicação)
ALTER ROLE text2sql_ro SET default_transaction_read_only = on;
ALTER ROLE text2sql_ro SET statement_timeout = '5s';
ALTER ROLE text2sql_ro SET lock_timeout = '2s';
ALTER ROLE text2sql_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE text2sql_ro SET work_mem = '16MB';
ALTER ROLE text2sql_ro SET search_path = 'public';

-- 4. Permissões mínimas explícitas (least privilege)
GRANT USAGE ON SCHEMA public TO text2sql_ro;

GRANT SELECT ON TABLE
    public.orders,
    public.order_items,
    public.products
TO text2sql_ro;

-- Permissão por coluna em customers: omite email, cpf e password_hash no próprio Postgres
GRANT SELECT (id, tenant_id, full_name, city, state, created_at)
ON TABLE public.customers
TO text2sql_ro;

-- Nenhuma permissão em tabelas internas/sensíveis (ex.: payroll, audit_logs)
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM text2sql_ro;

-- 5. Row-Level Security (RLS) obrigatório em tabelas multi-tenant
ALTER TABLE public.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.orders FORCE ROW LEVEL SECURITY;

ALTER TABLE public.order_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.order_items FORCE ROW LEVEL SECURITY;

ALTER TABLE public.customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.customers FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS orders_tenant_isolation ON public.orders;
CREATE POLICY orders_tenant_isolation ON public.orders
    FOR SELECT
    TO text2sql_ro
    USING (
        tenant_id = NULLIF(current_setting('app.current_tenant', true), '')
    );

DROP POLICY IF EXISTS order_items_tenant_isolation ON public.order_items;
CREATE POLICY order_items_tenant_isolation ON public.order_items
    FOR SELECT
    TO text2sql_ro
    USING (
        tenant_id = NULLIF(current_setting('app.current_tenant', true), '')
    );

DROP POLICY IF EXISTS customers_tenant_isolation ON public.customers;
CREATE POLICY customers_tenant_isolation ON public.customers
    FOR SELECT
    TO text2sql_ro
    USING (
        tenant_id = NULLIF(current_setting('app.current_tenant', true), '')
    );