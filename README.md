# Text2SQL

Agente Text-to-SQL em **Python / FastAPI** e **PydanticAI** projetado para traduzir perguntas de negócio em consultas PostgreSQL seguras, auditáveis e recuperáveis a falhas — combinando **Arquitetura Hexagonal**, **guardrails em 3 níveis (defense-in-depth)**, **auto-recuperação guiada (`ModelRetry`)**, **streaming SSE** e **suíte de regressão baseada em execução real (*Execution Accuracy*) no CI/CD**.

<p align="left">
  <img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/FastAPI-async-009688" alt="FastAPI" />
  <img src="https://img.shields.io/badge/PydanticAI-tool--use-E92063" alt="PydanticAI" />
  <img src="https://img.shields.io/badge/PostgreSQL-17%20%7C%20RLS-336791" alt="PostgreSQL 17" />
  <img src="https://img.shields.io/badge/SQLGlot-AST%20Guardrails-orange" alt="SQLGlot" />
</p>

---

## Visão Geral

Protótipos Text-to-SQL convencionais costumam falhar em produção por três razões: validação superficial baseada em regex, ausência de isolamento real no banco de dados e testes frágeis que comparam strings SQL em vez de resultados.

Este projeto resolve esses pontos estruturalmente:

- **Segurança em camadas:** análise estática via AST (`sqlglot`), validação de plano/custo (`EXPLAIN (FORMAT JSON)`) e barreira física no PostgreSQL com role `READ ONLY`, `GRANT` por coluna e **Row-Level Security (RLS) *fail-closed*** por tenant.
- **Orquestração delimitada e verificável:** o agente opera com 4 ferramentas mínimas, limites rígidos de requisições/tokens (`UsageLimits`), auto-recuperação de erros via `ModelRetry` e `output_validator` que impede respostas sem execução real de SQL.
- **Desacoplamento de provedor LLM:** portas tipadas (`Protocol`) permitem alternar entre **Google AI Studio (`gemini-3.8-flash`)**, **Groq (`Qwen3` / `GPT-OSS-120B`)** ou `FunctionModel` determinístico em testes sem alterar o domínio.
- **Avaliação contínua (Eval-Driven):** pipeline no GitHub Actions validando regras AST, isolamento RLS no PostgreSQL real, *Execution Accuracy* contra queries canônicas e taxa de bloqueio de 100% em ataques adversariais.

---

## Arquitetura em Camadas

O sistema combina **Arquitetura Hexagonal (Ports & Adapters)** na aplicação FastAPI com um **loop de agente delimitado (Bounded ReAct / Tool-Use)** no PydanticAI.

```mermaid
flowchart TD
    Client["Cliente HTTP / SSE"] --> API["API Layer (FastAPI)<br/>POST /v1/query · POST /v1/query/stream"]
    API --> Policy["Domain Policies<br/>user_role -> AccessPolicy + tenant_id"]
    Policy --> Agent["Orchestrator (PydanticAI)<br/>System Prompt + Glossário YAML + UsageLimits"]

    Agent -->|1. Descobrir schema| Catalog["SchemaCatalog (pg_catalog)<br/>DDL + FKs + Amostras Mascaradas"]
    Agent -->|2. Validar SQL| Guardrails["Nível 1: SQLGlot AST Pipeline<br/>Chain of Responsibility"]
    Guardrails -->|AST inválida| Retry["ModelRetry (Self-Repair, máx. 3)"]
    Retry --> Agent

    Guardrails -->|AST normalizada + LIMIT| Explain["Nível 2: EXPLAIN (FORMAT JSON)<br/>Validação de tipos e teto de custo"]
    Explain -->|Erro ou custo acima do teto| Retry

    Explain -->|Query aprovada| Exec["Nível 3: PostgresQueryExecutor (asyncpg)<br/>TX READ ONLY · set_config RLS · timeout"]
    Exec -->|SafeDatabaseError| Retry
    Exec -->|Dados estruturados| Validator["output_validator<br/>ensure_grounded_answer()"]
    Validator --> API

    API -.-> OTel["OpenTelemetry / Grafana OTEL-LGTM"]
```

### Guardrails em 3 Níveis (Defense-in-Depth)

1. **Nível 1 — Análise Estática via AST (`src/text2sql/guardrails/`):**
   Pipeline *Chain of Responsibility* sobre a árvore sintática PostgreSQL do `sqlglot`:
   - `ReadOnlySelectRule`: permite apenas `SELECT` / operações de conjunto (`UNION`, `INTERSECT`, `EXCEPT`) e varre toda a AST (`walk()`) para barrar DML/DDL oculto (como *writable CTEs* `WITH d AS (DELETE ...)`, `SELECT INTO` e `FOR UPDATE`).
   - `AllowedTablesRule`: impõe *allowlist* de schemas e tabelas por perfil, bloqueando `pg_catalog` e `information_schema`.
   - `BlockedFunctionsRule`: bloqueia funções de DoS, leitura de arquivos, SSRF, manipulação de sessão e serialização de linha inteira (`pg_sleep`, `pg_read_file`, `dblink`, `set_config`, `current_setting`, `row_to_json`, `to_jsonb`).
   - `BlockedColumnsAndStarRule`: proíbe `SELECT *` (preservando `COUNT(*)`) e bloqueia colunas PII (`email`, `cpf`, `password_hash`) em projeções, filtros, CTEs e ordenações.
   - `EnforceLimitRule`: injeta ou reduz `LIMIT` automaticamente para o teto da política ativa.

2. **Nível 2 — Validação de Plano (`EXPLAIN (FORMAT JSON)`):**
   Confirma a existência real de tabelas, colunas e tipos no PostgreSQL sem executar a consulta e rejeita planos cujo `Total Cost` ultrapasse `max_explain_cost`.

3. **Nível 3 — Barreira Física no PostgreSQL (`docker/postgres/02_roles_and_rls.sql` + `adapters/postgres.py`):**
   - Conexão exclusiva com a role `text2sql_ro` (`NOSUPERUSER`, `NOBYPASSRLS`, `default_transaction_read_only = on`, `statement_timeout`, `work_mem = 16MB`).
   - `GRANT SELECT` explícito por coluna em `public.customers` (omitindo `email`, `cpf` e `password_hash` no próprio motor do banco) e nenhum acesso à tabela sensível `public.payroll`.
   - Transações `conn.transaction(readonly=True)` configuradas via `SELECT set_config('app.current_tenant', $1, true)` (escopo local à transação, sem vazamento entre conexões do pool).
   - **RLS *fail-closed*:** sem `app.current_tenant` definido na transação, consultas retornam zero linhas.

### Ferramentas do Agente (ACI Enxuto)

| Ferramenta | Responsabilidade | Política de Falha |
| :--- | :--- | :--- |
| `list_allowed_tables()` | Lista tabelas autorizadas para o perfil ativo via `pg_catalog`. | Somente leitura. |
| `describe_tables(table_names)` | Retorna DDL, chaves estrangeiras e até 3 linhas mascaradas sob RLS. | `ModelRetry` se pedir tabela fora da *allowlist*. |
| `validate_and_explain(sql)` | Roda o pipeline `sqlglot` + `EXPLAIN (FORMAT JSON)` e registra a query validada. | `ModelRetry` com feedback acionável em erros de AST, schema ou custo. |
| `execute_sql(sql)` | Revalida a AST, exige aprovação prévia em `validate_and_explain` e executa no Postgres. | `ModelRetry` em `SafeDatabaseError`; aborta (HTTP 503) em `DatabaseInfraError`. |

### Perfis de Acesso (`src/text2sql/domain/policies.py`)

| Perfil (`user_role`) | Tabelas Permitidas | Colunas PII Bloqueadas | Máx. Linhas | Custo Máx. (`EXPLAIN`) | Timeout |
| :--- | :--- | :--- | :---: | :---: | :---: |
| `analyst` | `orders`, `order_items`, `customers`, `products` | `email`, `cpf`, `password_hash` | 100 | 15.000 | 5s |
| `finance` | `orders`, `order_items`, `customers`, `products` | `email`, `cpf`, `password_hash` | 250 | 25.000 | 8s |
| `support` | `orders`, `customers` | `email`, `cpf`, `password_hash` | 50 | 8.000 | 3s |

---

## Stack Tecnológica

| Camada | Tecnologia | Papel no Projeto |
| :--- | :--- | :--- |
| **API Web** | FastAPI + Uvicorn | Endpoints REST síncrono (`/v1/query`), streaming SSE (`/v1/query/stream`) e `/health`. |
| **Orquestração de Agente** | PydanticAI (`pydantic-ai-slim`) | Tool calling tipado, injeção de `AgentDeps`, `ModelRetry`, `output_validator` e `UsageLimits`. |
| **Validação SQL** | `sqlglot` | Parser e inspeção de AST no dialeto PostgreSQL. |
| **Banco Relacional & Driver** | PostgreSQL 17 + `asyncpg` | Pool assíncrono, transações `READ ONLY`, RLS e `EXPLAIN (FORMAT JSON)`. |
| **Modelos LLM** | Google Gemini (`gemini-3.8-flash`) / Groq | Inferência principal e provedor secundário *model-agnostic*. |
| **Observabilidade** | OpenTelemetry + Grafana OTEL-LGTM | Tracing distribuído de requisições, spans do agente e consultas. |
| **Qualidade & CI/CD** | Poetry, Ruff, Mypy (`strict`), Pytest, GitHub Actions | Lint, checagem estática, testes determinísticos, RLS e regressão de prompts. |

---

## Estrutura de Diretórios

- **`text-to-sql-agent/`**
  - **`.github/workflows/`**
    - `ci.yml` — Pipeline de CI/CD: lint/mypy, testes unitários/contrato, RLS no PostgreSQL e *prompt regression*.
  - **`docker/`**
    - `Dockerfile` — Build multi-stage com Poetry e execução com usuário não-root (`appuser`).
    - **`postgres/`**
      - `01_schema.sql` — DDL do domínio (`customers`, `products`, `orders`, `order_items`, `payroll`) e índices por tenant.
      - `02_roles_and_rls.sql` — Role `text2sql_ro`, `GRANT` por coluna e políticas de RLS *fail-closed*.
      - `03_seed_ci.sql` — Seed determinístico multi-tenant (`tenant_a` e `tenant_b`) com iscas adversariais.
  - `docker-compose.yml` — Orquestração local: PostgreSQL 17, FastAPI e perfil opcional `observability` (Grafana OTEL-LGTM).
  - `pyproject.toml` — Dependências (Poetry / PEP 621) e configurações de Ruff, Mypy e Pytest.
  - **`src/text2sql/`**
    - `__init__.py` — Inicialização do pacote.
    - `config.py` — Configurações via `pydantic-settings` (DSNs, modelo LLM, limites do pool e OTel).
    - **`domain/`** — Núcleo de negócio e contratos hexagonais.
      - `models.py` — `AccessPolicy`, `ValidationResult`, `ExplainPlanResult`, `QueryExecutionResult` e `SQLGuardrailError`.
      - `policies.py` — Políticas por perfil (`analyst`, `finance`, `support`) e `UnknownRoleError`.
      - `ports.py` — Protocols: `SQLValidator`, `QueryExecutor` e `SchemaCatalog`.
    - **`guardrails/`** — Validação estática via AST (`sqlglot`).
      - `base.py` — `GuardrailContext` e `Protocol` `GuardrailRule`.
      - `rules.py` — Regras de AST (somente leitura, tabelas, funções, PII/`SELECT *` e `LIMIT`).
      - `pipeline.py` — `SQLGlotGuardrailPipeline` (*Chain of Responsibility*).
    - **`agent/`** — Orquestração do agente e prompts versionados.
      - `deps.py` — `AgentDeps` (injeção de dependências e trilha de auditoria de queries executadas).
      - **`prompts/`**
        - `system_v1.md` — System prompt versionado.
        - `glossary.yaml` — Definições canônicas de métricas de negócio.
      - `tools.py` — Registro das 4 ferramentas com `ModelRetry`.
      - `orchestrator.py` — `build_sql_agent`, `AnalystResponse`, `ensure_grounded_answer` e `run_business_question`.
    - **`adapters/`** — Implementações de infraestrutura.
      - `postgres.py` — `PostgresQueryExecutor` (`asyncpg`, `READ ONLY`, `set_config`, `EXPLAIN JSON`, `SafeDatabaseError` vs `DatabaseInfraError`).
      - `catalog.py` — `PostgresSchemaCatalog` (`pg_catalog` com amostras mascaradas sob RLS).
      - `telemetry.py` — Configuração do `TracerProvider`, exportador OTLP e instrumentação do agente.
    - **`api/`** — Camada HTTP FastAPI.
      - `main.py` — App factory, *lifespan* do pool `asyncpg` e rota `/health`.
      - `schemas.py` — DTOs `QueryRequest`, `QueryResponse` e envelope `SSEEvent`.
      - `routes.py` — Endpoints `POST /v1/query` e `POST /v1/query/stream`.
  - **`tests/`** — Suíte determinística (custo zero de LLM).
    - `conftest.py` — Fixtures compartilhadas de políticas e mocks.
    - **`unit/`**
      - `test_guardrails.py` — Testes de AST e vetores de bypass.
    - **`integration/`**
      - `test_postgres_security.py` — Validação de `READ ONLY`, RLS *fail-closed*, PII, timeout e catálogo no Postgres real.
    - **`contract/`**
      - `test_agent_loop.py` — Loop completo do agente, `ModelRetry`, `output_validator` e circuit breakers via `FunctionModel`.
  - **`evals/`** — Regressão de prompts com LLM real.
    - `golden_dataset.yaml` — Perguntas de negócio e SQL canônico esperado (`smoke` e `full`).
    - `adversarial_dataset.yaml` — Prompt injections, exfiltração PII, fuga de tenant e DoS.
    - `test_regression.py` — Avaliador de *Execution Accuracy* e taxa de bloqueio adversarial.

---

## Configuração e Execução

### Pré-requisitos

- **Docker** e **Docker Compose** (v2+)
- **Python 3.12+** e **Poetry 2+** (para desenvolvimento local e testes fora do container)
- Uma chave de API gratuita do **Google AI Studio (`GEMINI_API_KEY`)** ou **GroqCloud (`GROQ_API_KEY`)**

### Variáveis de Ambiente

Crie um arquivo `.env` na raiz do projeto (baseado em `.env.example`):

```env
# Provedor LLM (padrão: Google AI Studio)
LLM_MODEL=google-gla:gemini-3.8-flash
GEMINI_API_KEY=sua_chave_gemini_aqui

# Opcional: fallback/benchmark via Groq
# LLM_MODEL=groq:qwen/qwen3-32b
# GROQ_API_KEY=sua_chave_groq_aqui

# Banco de dados (usado pela aplicação com role restrita)
POSTGRES_USER=postgres
POSTGRES_PASSWORD=dev_admin_password
POSTGRES_DB=text2sql_ci
RO_DB_PASSWORD=change_me_in_prod_ro
RO_DATABASE_URL=postgresql://text2sql_ro:change_me_in_prod_ro@127.0.0.1:5432/text2sql_ci

# Observabilidade (habilite junto ao perfil observability do Docker Compose)
ENABLE_OTEL=false
```

### Opção 1: Subir Tudo com Docker Compose (Recomendado)

Na primeira inicialização, o container PostgreSQL executa automaticamente `01_schema.sql`, `02_roles_and_rls.sql` e `03_seed_ci.sql`.

```bash
# Subir PostgreSQL 17 + API FastAPI ([http://127.0.0.1:8000](http://127.0.0.1:8000))
docker compose up --build -d

# Verificar saúde da aplicação e conectividade com o banco
curl [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)
```

Para inspecionar traces OpenTelemetry no **Grafana OTEL-LGTM** (`http://127.0.0.1:3000`, login `admin`/`admin`):

```bash
ENABLE_OTEL=true docker compose --profile observability up --build -d
```

> **Dica:** se alterar qualquer script em `docker/postgres/`, recrie o volume do banco com `docker compose down -v && docker compose up --build -d` para reexecutar o `docker-entrypoint-initdb.d`.

### Opção 2: Desenvolvimento Local com Poetry

```bash
# 1. Instalar dependências de produção e desenvolvimento
poetry install --with dev

# 2. Subir apenas o PostgreSQL 17 via Docker
docker compose up -d postgres

# 3. Rodar a API FastAPI com reload automático
poetry run uvicorn text2sql.api.main:app --reload --host 0.0.0.0 --port 8000
```

A documentação interativa OpenAPI fica disponível em `http://127.0.0.1:8000/docs`.

---

## Uso da API

### Consulta Síncrona (`POST /v1/query`)

```bash
curl -X POST [http://127.0.0.1:8000/v1/query](http://127.0.0.1:8000/v1/query) \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Qual foi o faturamento total dos pedidos entregues?",
    "tenant_id": "tenant_a",
    "user_role": "analyst"
  }'
```

Exemplo de resposta:

```json
{
  "status": "answered",
  "answer": "O faturamento total dos pedidos entregues do tenant_a foi de R$ 4.800,00.",
  "sql_query": "SELECT SUM(total_amount) AS total_revenue FROM orders WHERE status = 'delivered' LIMIT 100",
  "assumptions": ["Considerados apenas pedidos com status 'delivered'."],
  "executed_queries_count": 1,
  "elapsed_ms": 1420.5
}
```

### Streaming em Tempo Real (`POST /v1/query/stream`)

Emite eventos **Server-Sent Events (SSE)** acompanhando cada iteração do loop do agente:

```bash
curl -N -X POST [http://127.0.0.1:8000/v1/query/stream](http://127.0.0.1:8000/v1/query/stream) \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Liste os 3 clientes que mais gastaram em pedidos entregues.",
    "tenant_id": "tenant_a",
    "user_role": "analyst"
  }'
```

Eventos emitidos: `step` (raciocínio do modelo), `tool_call` (chamada de ferramenta e argumentos), `tool_retry` (auto-recuperação após erro de guardrail/EXPLAIN/SQL), `tool_result` (conclusão da ferramenta), `final` (`QueryResponse` completo) e `error` (falha de limite ou infraestrutura).

---

## Como Rodar Testes e Evals

### 1. Lint e Checagem Estática

```bash
poetry run ruff check src tests evals
poetry run ruff format --check src tests evals
poetry run mypy src
```

### 2. Testes Unitários e de Contrato (Custo Zero de LLM)

Validam todas as regras de bypass na AST (`sqlglot`) e o loop completo do agente (`ModelRetry`, `output_validator`, circuit breakers) com `FunctionModel` e `ALLOW_MODEL_REQUESTS = False`:

```bash
poetry run pytest tests/unit tests/contract -v
```

### 3. Testes de Integração de Segurança e RLS (PostgreSQL Real)

Conectam diretamente com a role `text2sql_ro` para provar que o banco bloqueia escrita, leitura cruzada de tenants, colunas PII, `payroll`, `pg_sleep` e consultas acima do `statement_timeout`:

```bash
docker compose up -d postgres
export RO_DATABASE_URL="postgresql://text2sql_ro:change_me_in_prod_ro@127.0.0.1:5432/text2sql_ci"
poetry run pytest tests/integration/test_postgres_security.py -v
```

### 4. Suíte de Regressão de Prompts (`evals/`)

Executa as perguntas de `evals/golden_dataset.yaml` (comparando o conjunto de resultados do SQL gerado contra o `golden_sql` no mesmo Postgres, de forma independente de aliases) e os ataques de `evals/adversarial_dataset.yaml`:

```bash
export EVAL_DATABASE_URL="postgresql://text2sql_ro:change_me_in_prod_ro@127.0.0.1:5432/text2sql_ci"
export GEMINI_API_KEY="sua_chave_gemini_aqui"
export EVAL_MODEL="google-gla:gemini-3.8-flash"

# Smoke suite (rápida, usada em PRs de prompt/guardrails)
EVAL_TIER=smoke EVAL_MIN_ACCURACY=0.85 EVAL_PACING_SECONDS=2.0 \
  poetry run pytest evals/test_regression.py -v -s

# Full suite (todos os casos, usada no agendamento nightly)
EVAL_TIER=full EVAL_MIN_ACCURACY=0.85 EVAL_PACING_SECONDS=2.0 \
  poetry run pytest evals/test_regression.py -v -s
```

Ao final da execução, o relatório estruturado é salvo em `eval-report.json`.

---

## Pipeline CI/CD (GitHub Actions)

O workflow `.github/workflows/ci.yml` executa quatro jobs encadeados:

| Job | Gatilho | O que valida | Gate de Aprovação |
| :--- | :--- | :--- | :--- |
| `changes` | Push / PR | Detecta mudanças em `agent/`, `guardrails/`, `policies.py`, `evals/` e `docker/postgres/`. | — |
| `static-and-unit` | Todo Push / PR | Ruff (lint + format), Mypy (`strict`), `tests/unit` e `tests/contract`. | 100% pass |
| `postgres-security` | Após `static-and-unit` | Sobe PostgreSQL 17, aplica DDL/RLS/seed e roda `test_postgres_security.py`. | 100% pass |
| `prompt-regression` | PRs de agente/prompt (`smoke`), cron diário às 06:00 UTC (`full`) ou `workflow_dispatch` | Executa `evals/test_regression.py`, gera tabela no `$GITHUB_STEP_SUMMARY` e publica `eval-report.json`. | `Execution Accuracy >= 85%` e **100%** bloqueio adversarial |

Para habilitar o job `prompt-regression` no repositório, configure o secret `GEMINI_API_KEY` em **Settings → Secrets and variables → Actions**.