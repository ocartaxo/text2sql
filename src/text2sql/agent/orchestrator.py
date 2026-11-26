# src/text2sql/agent/orchestrator.py
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.usage import UsageLimits

from text2sql.adapters.postgres import SafeDatabaseError
from text2sql.agent.deps import AgentDeps
from text2sql.config import DEFAULT_PROMPTS_DIR
from text2sql.domain.models import SQLGuardrailError

DEFAULT_SYSTEM_PROMPT_PATH = DEFAULT_PROMPTS_DIR / "system_v1.md"


class AnalystResponse(BaseModel):
    status: Literal["answered", "needs_clarification", "out_of_scope"]
    answer: str = Field(
        description="Resposta objetiva em português baseada exclusivamente nos dados retornados."
    )
    sql_query: str | None = Field(
        default=None,
        description="SQL normalizado efetivamente executado via execute_sql.",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Premissas aplicadas (período, filtros, definição de métrica).",
    )


def load_system_prompt(path: Path = DEFAULT_SYSTEM_PROMPT_PATH) -> str:
    return path.read_text(encoding="utf-8").strip()


def build_sql_agent(
    model_name: str = "google-gla:gemini-3.8-flash",
    *,
    system_prompt_path: Path = DEFAULT_SYSTEM_PROMPT_PATH,
) -> Agent[AgentDeps, AnalystResponse]:
    agent: Agent[AgentDeps, AnalystResponse] = Agent(
        model=model_name,
        deps_type=AgentDeps,
        output_type=AnalystResponse,
        system_prompt=load_system_prompt(system_prompt_path),
        retries=3,
    )

    @agent.system_prompt
    def inject_business_glossary(ctx: RunContext[AgentDeps]) -> str:
        return (
            f"Perfil ativo: {ctx.deps.policy.role} | "
            f"Limite máximo de linhas: {ctx.deps.policy.max_rows}\n\n"
            f"Glossário de métricas de negócio:\n{ctx.deps.glossary_text}"
        )

    @agent.tool
    async def list_allowed_tables(ctx: RunContext[AgentDeps]) -> list[dict[str, str]]:
        """Lista todas as tabelas autorizadas para o usuário atual com breves descrições."""
        return await ctx.deps.catalog.list_tables(ctx.deps.policy)

    @agent.tool(retries=2)
    async def describe_tables(
        ctx: RunContext[AgentDeps],
        table_names: list[str],
    ) -> str:
        """Retorna DDL simplificado, chaves estrangeiras e amostras mascaradas das tabelas solicitadas."""
        allowed = {t.lower() for t in ctx.deps.policy.allowed_tables}
        invalid = [t for t in table_names if t.lower() not in allowed]
        if invalid:
            raise ModelRetry(
                f"Tabelas não autorizadas ou inexistentes: {invalid}. "
                "Chame list_allowed_tables() e peça apenas nomes exatos da lista."
            )
        return await ctx.deps.catalog.describe_tables(
            table_names,
            ctx.deps.policy,
            tenant_id=ctx.deps.tenant_id,
        )

    @agent.tool(retries=3)
    async def validate_and_explain(
        ctx: RunContext[AgentDeps],
        sql: str,
    ) -> dict[str, object]:
        """Valida sintaxe, permissões via AST (sqlglot) e custo estimado via EXPLAIN no PostgreSQL."""
        try:
            validation = ctx.deps.validator.validate(sql, ctx.deps.policy)
        except SQLGuardrailError as exc:
            raise ModelRetry(exc.to_agent_feedback()) from exc

        try:
            plan = await ctx.deps.executor.explain(
                validation.normalized_sql,
                tenant_id=ctx.deps.tenant_id,
                policy=ctx.deps.policy,
            )
        except SafeDatabaseError as exc:
            raise ModelRetry(
                f"Falha no EXPLAIN do PostgreSQL: {exc}. "
                "Verifique nomes de colunas, aliases, tipos e cláusulas GROUP BY usando describe_tables()."
            ) from exc

        if plan.estimated_cost > ctx.deps.policy.max_explain_cost:
            raise ModelRetry(
                f"Custo estimado muito alto ({plan.estimated_cost:.1f} excede o teto "
                f"{ctx.deps.policy.max_explain_cost:.1f}). Adicione filtros seletivos, "
                "evite produtos cartesianos e filtre antes de fazer JOIN."
            )

        ctx.deps.validated_queries.add(validation.normalized_sql)
        return {
            "status": "valid",
            "normalized_sql": validation.normalized_sql,
            "estimated_cost": plan.estimated_cost,
            "estimated_rows": plan.estimated_rows,
            "limit_injected": validation.limit_injected,
        }

    @agent.tool(retries=3)
    async def execute_sql(
        ctx: RunContext[AgentDeps],
        sql: str,
    ) -> dict[str, object]:
        """Executa a consulta validada em transação READ ONLY com timeout e RLS."""
        try:
            validation = ctx.deps.validator.validate(sql, ctx.deps.policy)
        except SQLGuardrailError as exc:
            raise ModelRetry(exc.to_agent_feedback()) from exc

        if validation.normalized_sql not in ctx.deps.validated_queries:
            raise ModelRetry(
                "Esta consulta ainda não passou por validate_and_explain(). "
                "Chame validate_and_explain() primeiro com o SQL exato."
            )

        try:
            result = await ctx.deps.executor.execute(
                validation.normalized_sql,
                tenant_id=ctx.deps.tenant_id,
                policy=ctx.deps.policy,
            )
        except SafeDatabaseError as exc:
            raise ModelRetry(
                f"Erro na execução da query no PostgreSQL: {exc}. "
                "Corrija a consulta (ex.: divisão por zero com NULLIF, cast de tipos ou timeout) "
                "e passe novamente por validate_and_explain()."
            ) from exc

        ctx.deps.executed_queries.append(validation.normalized_sql)
        return {
            "normalized_sql": validation.normalized_sql,
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
            "elapsed_ms": result.elapsed_ms,
        }

    @agent.output_validator
    def ensure_grounded_answer(
        ctx: RunContext[AgentDeps],
        output: AnalystResponse,
    ) -> AnalystResponse:
        if output.status == "answered":
            if not ctx.deps.executed_queries:
                raise ModelRetry(
                    "Você respondeu com status='answered' sem executar nenhuma consulta. "
                    "Execute a query via execute_sql() antes de apresentar os números."
                )
            if output.sql_query not in ctx.deps.executed_queries:
                output.sql_query = ctx.deps.executed_queries[-1]
        return output

    return agent


async def run_business_question(
    agent: Agent[AgentDeps, AnalystResponse],
    question: str,
    deps: AgentDeps,
    *,
    request_limit: int = 8,
    total_tokens_limit: int = 24_000,
) -> AnalystResponse:
    """Ponto de entrada com teto rígido de requisições/tokens (Circuit Breaker)."""
    result = await agent.run(
        question,
        deps=deps,
        usage_limits=UsageLimits(
            request_limit=request_limit,
            total_tokens_limit=total_tokens_limit,
        ),
    )
    return result.output
