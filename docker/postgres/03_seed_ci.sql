TRUNCATE TABLE
    public.order_items,
    public.orders,
    public.products,
    public.customers,
    public.payroll
RESTART IDENTITY CASCADE;

-- Produtos (catálogo compartilhado)
INSERT INTO public.products (sku, name, category, unit_price, created_at) VALUES
    ('SKU-001', 'Teclado Mecânico 75%',   'perifericos', 600.00,  '2026-01-05 10:00:00+00'),
    ('SKU-002', 'Monitor 27" QHD',        'monitores',   1500.00, '2026-01-05 10:00:00+00'),
    ('SKU-003', 'Dock USB-C 100W',        'acessorios',  400.00,  '2026-01-05 10:00:00+00'),
    ('SKU-004', 'Mouse Ergonomico Pro',   'perifericos', 350.00,  '2026-01-05 10:00:00+00'),
    ('SKU-005', 'Headset Cancelamento',   'audio',       950.00,  '2026-01-05 10:00:00+00');

-- Clientes tenant_a (IDs 1..4) e tenant_b (ID 5)
INSERT INTO public.customers (tenant_id, full_name, city, state, email, cpf, password_hash, created_at) VALUES
    ('tenant_a', 'Ana Clara Silva', 'Salvador',       'BA', 'ana.silva@tenant-a.test',   '123.456.789-00', 'sha256:hash_ana',    '2026-01-10 09:00:00+00'),
    ('tenant_a', 'Bruno Costa',     'São Paulo',      'SP', 'bruno.costa@tenant-a.test', '987.654.321-11', 'sha256:hash_bruno',  '2026-01-12 11:30:00+00'),
    ('tenant_a', 'Carla Mendes',    'Feira de Santana','BA','carla.mendes@tenant-a.test','222.333.444-55', 'sha256:hash_carla',  '2026-01-15 14:20:00+00'),
    ('tenant_a', 'Diego Souza',     'Rio de Janeiro', 'RJ', 'diego.souza@tenant-a.test', '333.444.555-66', 'sha256:hash_diego',  '2026-01-18 16:45:00+00'),
    ('tenant_b', 'Empresa B Ltda',  'Campinas',       'SP', 'financeiro@empresab.test',  '555.666.777-88', 'sha256:hash_emp_b',  '2026-01-20 08:15:00+00');

-- Pedidos tenant_a (IDs 1..7) e tenant_b (IDs 8..9)
INSERT INTO public.orders (tenant_id, order_code, customer_id, status, total_amount, created_at) VALUES
    ('tenant_a', 'PED-TENANT-A-001', 1, 'delivered', 1200.00, '2026-02-01 10:00:00+00'),
    ('tenant_a', 'PED-TENANT-A-002', 1, 'delivered',  800.00, '2026-02-10 15:00:00+00'),
    ('tenant_a', 'PED-TENANT-A-003', 2, 'delivered', 1500.00, '2026-02-12 12:30:00+00'),
    ('tenant_a', 'PED-TENANT-A-004', 3, 'delivered',  950.00, '2026-02-14 09:45:00+00'),
    ('tenant_a', 'PED-TENANT-A-005', 4, 'delivered',  350.00, '2026-02-18 18:10:00+00'),
    ('tenant_a', 'PED-TENANT-A-006', 4, 'shipped',    600.00, '2026-02-20 11:00:00+00'),
    ('tenant_a', 'PED-TENANT-A-007', 2, 'canceled',   400.00, '2026-02-21 17:00:00+00'),
    ('tenant_b', 'PED-TENANT-B-001', 5, 'delivered', 9900.00, '2026-02-05 13:00:00+00'),
    ('tenant_b', 'PED-TENANT-B-002', 5, 'delivered', 4500.00, '2026-02-19 16:00:00+00');

-- Itens dos pedidos
INSERT INTO public.order_items (tenant_id, order_id, product_id, quantity, unit_price) VALUES
    ('tenant_a', 1, 1, 2,  600.00),
    ('tenant_a', 2, 3, 2,  400.00),
    ('tenant_a', 3, 2, 1, 1500.00),
    ('tenant_a', 4, 5, 1,  950.00),
    ('tenant_a', 5, 4, 1,  350.00),
    ('tenant_a', 6, 1, 1,  600.00),
    ('tenant_a', 7, 3, 1,  400.00),
    ('tenant_b', 8, 2, 6, 1500.00),
    ('tenant_b', 8, 5, 1,  900.00),
    ('tenant_b', 9, 2, 3, 1500.00);

-- Tabela restrita de folha salarial (inacessível para text2sql_ro)
INSERT INTO public.payroll (tenant_id, employee_name, salary, paid_at) VALUES
    ('tenant_a', 'Diretor Financeiro A', 28000.00, '2026-02-25 00:00:00+00'),
    ('tenant_b', 'Diretor Financeiro B', 32000.00, '2026-02-25 00:00:00+00');