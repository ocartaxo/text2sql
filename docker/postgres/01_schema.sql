CREATE TABLE IF NOT EXISTS public.customers (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    city          TEXT NOT NULL,
    state         CHAR(2) NOT NULL,
    email         TEXT NOT NULL,
    cpf           TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.products (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    unit_price  NUMERIC(12, 2) NOT NULL CHECK (unit_price >= 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.orders (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    order_code   TEXT NOT NULL UNIQUE,
    customer_id  BIGINT NOT NULL REFERENCES public.customers (id),
    status       TEXT NOT NULL CHECK (status IN ('processing', 'shipped', 'delivered', 'canceled')),
    total_amount NUMERIC(12, 2) NOT NULL CHECK (total_amount >= 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.order_items (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    order_id    BIGINT NOT NULL REFERENCES public.orders (id),
    product_id  BIGINT NOT NULL REFERENCES public.products (id),
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    unit_price  NUMERIC(12, 2) NOT NULL CHECK (unit_price >= 0)
);

-- Tabela interna restrita (não recebe GRANT para text2sql_ro) usada para provar bloqueio de permissão
CREATE TABLE IF NOT EXISTS public.payroll (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    employee_name TEXT NOT NULL,
    salary        NUMERIC(12, 2) NOT NULL,
    paid_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_customers_tenant_state
    ON public.customers (tenant_id, state);

CREATE INDEX IF NOT EXISTS idx_orders_tenant_status
    ON public.orders (tenant_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_orders_tenant_customer
    ON public.orders (tenant_id, customer_id);

CREATE INDEX IF NOT EXISTS idx_order_items_tenant_order
    ON public.order_items (tenant_id, order_id);