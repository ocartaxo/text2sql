from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pydantic_ai.models
import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from text2sql.adapters.postgres import DatabaseInfraError, SafeDatabaseError
from text2sql.agent.deps import AgentDeps
from text2sql.agent.orchestrator import build_sql_agent, run_business_question
from text2sql.domain.models import (
    AccessPolicy,
    ExplainPlanResult,
    QueryExecutionResult,
)
from text2sql.domain.policies import get_policy_for_role
from text2sql.guardrails.pipeline import SQLGlotGuardrailPipeline

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def block_real_llm_calls() -> Iterator[None]:
    """Garante zero chamadas externas de rede durante os testes de contrato."""
    previous = pydantic_ai.models.ALLOW_MODEL_REQUESTS
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    try:
        yield
    finally:
        pydantic_ai.models.ALLOW_MODEL_REQUESTS = previous


class FakeSchemaCatalog:
    async def list_tables(self, policy: AccessPolicy) -> list[dict[str, str]]:
        return [
            {"schema": "public", "table": name, "description": f"Tabela {name}"}
            for name in sorted(policy.allowed_tables)
        ]

    async def describe_tables(
        self,
        table_names: list[str],
        policy: AccessPolicy,
        *,
        tenant_id: str | None = None,
    ) -> str:
        return "\n".join(
            f"CREATE TABLE public.{t} (id BIGINT, status TEXT, total_amount NUMERIC);"
            for t in table_names
        )


class FakeQueryExecutor:
    """Double determinístico que permite simular custos de EXPLAIN e falhas do Postgres."""

    def __init__(
        self,
        *,
        explain_costs: list[float] | None = None,
        execute_errors: list[Exception] | None = None,
        rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self._explain_costs = list(explain_costs or [120.0])
        self._execute_errors = list(execute_errors or [])
        self._rows = rows if rows is not None else [{"total_revenue": 4800.0}]
        self.explain_calls: list[str] = []
        self.execute_calls: list[str] = []

    async def explain(
        self,
        sql: str,
        *,
        tenant_id: str,
        policy: AccessPolicy,
    ) -> ExplainPlanResult:
        self.explain_calls.append(sql)
        cost = (
            self._explain_costs.pop(0) if len(self._explain_costs) > 1 else self._explain_costs[0]
        )
        return ExplainPlanResult(
            estimated_cost=cost,
            estimated_rows=10,
            raw_plan={"Node Type": "Index Scan", "Total Cost": cost},
        )

    async def execute(
        self,
        sql: str,
        *,
        tenant_id: str,
        policy: AccessPolicy,
    ) -> QueryExecutionResult:
        self.execute_calls.append(sql)
        if self._execute_errors:
            raise self._execute_errors.pop(0)

        columns = tuple(self._rows[0].keys()) if self._rows else ()
        return QueryExecutionResult(
            columns=columns,
            rows=self._rows,
            row_count=len(self._rows),
            truncated=False,
            elapsed_ms=4.2,
        )


def _make_deps(executor: FakeQueryExecutor | None = None) -> AgentDeps:
    return AgentDeps(
        tenant_id="tenant_a",
        policy=get_policy_for_role("analyst"),
        validator=SQLGlotGuardrailPipeline(),
        executor=executor or FakeQueryExecutor(),
        catalog=FakeSchemaCatalog(),
        glossary_text="receita_liquida: SUM(orders.total_amount) WHERE status = 'delivered'",
    )


def _retry_messages_in_last_turn(messages: list[ModelMessage]) -> list[str]:
    if not messages or not isinstance(messages[-1], ModelRequest):
        return []
    return [str(part.content) for part in messages[-1].parts if isinstance(part, RetryPromptPart)]


def _output_tool_name(info: AgentInfo) -> str:
    """Resolve o nome da tool de saída estruturada (ex.: 'final_result')."""
    assert info.output_tools, "Agent precisa declarar output_type estruturado."
    return info.output_tools[0].name


class TestAgentHappyPath:
    async def test_happy_path_full_tool_sequence(self) -> None:
        executor = FakeQueryExecutor(rows=[{"total_revenue": 4800.0}])
        deps = _make_deps(executor)
        sql = "SELECT SUM(total_amount) AS total_revenue FROM orders WHERE status = 'delivered'"
        turn = 0

        def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal turn
            turn += 1
            if turn == 1:
                return ModelResponse(parts=[ToolCallPart("list_allowed_tables", {})])
            if turn == 2:
                return ModelResponse(
                    parts=[ToolCallPart("describe_tables", {"table_names": ["orders"]})]
                )
            if turn == 3:
                return ModelResponse(parts=[ToolCallPart("validate_and_explain", {"sql": sql})])
            if turn == 4:
                return ModelResponse(parts=[ToolCallPart("execute_sql", {"sql": sql})])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        _output_tool_name(info),
                        {
                            "status": "answered",
                            "answer": "O faturamento entregue foi de R$ 4.800,00.",
                            "sql_query": deps.executed_queries[-1],
                            "assumptions": ["Considerados apenas pedidos com status delivered."],
                        },
                    )
                ]
            )

        agent = build_sql_agent(model_name="test")
        with agent.override(model=FunctionModel(scripted_model)):
            output = await run_business_question(agent, "Qual foi o faturamento entregue?", deps)

        assert output.status == "answered"
        assert "4.800,00" in output.answer
        assert output.sql_query is not None
        assert "LIMIT 100" in output.sql_query
        assert len(executor.explain_calls) == 1
        assert len(executor.execute_calls) == 1
        assert len(deps.executed_queries) == 1


class TestModelRetrySelfRepair:
    async def test_self_repair_after_sqlglot_guardrail_error(self) -> None:
        executor = FakeQueryExecutor(rows=[{"id": 1, "full_name": "Ana Clara Silva"}])
        deps = _make_deps(executor)
        captured_retries: list[str] = []
        turn = 0

        safe_sql = "SELECT id, full_name FROM customers LIMIT 10"

        def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal turn
            turn += 1
            captured_retries.extend(_retry_messages_in_last_turn(messages))

            if turn == 1:
                # Tenta SELECT * (bloqueado por BlockedColumnsAndStarRule)
                return ModelResponse(
                    parts=[ToolCallPart("validate_and_explain", {"sql": "SELECT * FROM customers"})]
                )
            if turn == 2:
                # Recebeu RetryPromptPart e corrige para projeção explícita
                return ModelResponse(
                    parts=[ToolCallPart("validate_and_explain", {"sql": safe_sql})]
                )
            if turn == 3:
                return ModelResponse(parts=[ToolCallPart("execute_sql", {"sql": safe_sql})])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        _output_tool_name(info),
                        {
                            "status": "answered",
                            "answer": "Cliente encontrada: Ana Clara Silva.",
                            "sql_query": safe_sql,
                            "assumptions": [],
                        },
                    )
                ]
            )

        agent = build_sql_agent(model_name="test")
        with agent.override(model=FunctionModel(scripted_model)):
            output = await run_business_question(agent, "Liste os clientes", deps)

        assert output.status == "answered"
        assert any("SELECT_STAR_FORBIDDEN" in msg for msg in captured_retries)
        # EXPLAIN só foi chamado depois que a AST passou nos guardrails
        assert len(executor.explain_calls) == 1
        assert len(executor.execute_calls) == 1

    async def test_self_repair_after_high_explain_cost(self) -> None:
        executor = FakeQueryExecutor(
            explain_costs=[42_000.0, 350.0],
            rows=[{"id": 1, "total_amount": 1200.0}],
        )
        deps = _make_deps(executor)
        captured_retries: list[str] = []
        turn = 0

        expensive_sql = "SELECT id, total_amount FROM orders"
        optimized_sql = "SELECT id, total_amount FROM orders WHERE status = 'delivered' LIMIT 10"

        def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal turn
            turn += 1
            captured_retries.extend(_retry_messages_in_last_turn(messages))

            if turn == 1:
                return ModelResponse(
                    parts=[ToolCallPart("validate_and_explain", {"sql": expensive_sql})]
                )
            if turn == 2:
                return ModelResponse(
                    parts=[ToolCallPart("validate_and_explain", {"sql": optimized_sql})]
                )
            if turn == 3:
                return ModelResponse(parts=[ToolCallPart("execute_sql", {"sql": optimized_sql})])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        _output_tool_name(info),
                        {
                            "status": "answered",
                            "answer": "Pedido 1 totalizou R$ 1.200,00.",
                            "sql_query": optimized_sql,
                            "assumptions": [],
                        },
                    )
                ]
            )

        agent = build_sql_agent(model_name="test")
        with agent.override(model=FunctionModel(scripted_model)):
            output = await run_business_question(agent, "Mostre pedidos entregues", deps)

        assert output.status == "answered"
        assert any("Custo estimado muito alto" in msg for msg in captured_retries)
        assert len(executor.explain_calls) == 2
        assert len(executor.execute_calls) == 1

    async def test_execute_sql_forces_prior_validation_and_recovers_from_db_error(self) -> None:
        executor = FakeQueryExecutor(
            execute_errors=[SafeDatabaseError("division by zero")],
            rows=[{"avg_ticket": 960.0}],
        )
        deps = _make_deps(executor)
        captured_retries: list[str] = []
        turn = 0

        first_sql = "SELECT SUM(total_amount) / COUNT(id) AS avg_ticket FROM orders"
        fixed_sql = "SELECT SUM(total_amount) / NULLIF(COUNT(id), 0) AS avg_ticket FROM orders"

        def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal turn
            turn += 1
            captured_retries.extend(_retry_messages_in_last_turn(messages))

            if turn == 1:
                # Tenta pular validate_and_explain -> bloqueado por ModelRetry
                return ModelResponse(parts=[ToolCallPart("execute_sql", {"sql": first_sql})])
            if turn == 2:
                return ModelResponse(
                    parts=[ToolCallPart("validate_and_explain", {"sql": first_sql})]
                )
            if turn == 3:
                # Dispara SafeDatabaseError("division by zero") -> ModelRetry
                return ModelResponse(parts=[ToolCallPart("execute_sql", {"sql": first_sql})])
            if turn == 4:
                return ModelResponse(
                    parts=[ToolCallPart("validate_and_explain", {"sql": fixed_sql})]
                )
            if turn == 5:
                return ModelResponse(parts=[ToolCallPart("execute_sql", {"sql": fixed_sql})])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        _output_tool_name(info),
                        {
                            "status": "answered",
                            "answer": "O ticket médio é de R$ 960,00.",
                            "sql_query": fixed_sql,
                            "assumptions": [],
                        },
                    )
                ]
            )

        agent = build_sql_agent(model_name="test")
        with agent.override(model=FunctionModel(scripted_model)):
            output = await run_business_question(agent, "Qual o ticket médio?", deps)

        assert output.status == "answered"
        assert any("validate_and_explain()" in msg for msg in captured_retries)
        assert any("division by zero" in msg for msg in captured_retries)
        assert len(deps.executed_queries) == 1
        assert "NULLIF" in deps.executed_queries[0]


class TestOutputValidatorGrounding:
    async def test_output_validator_blocks_ungrounded_answer_and_normalizes_sql(self) -> None:
        executor = FakeQueryExecutor(rows=[{"total_revenue": 4800.0}])
        deps = _make_deps(executor)
        captured_retries: list[str] = []
        turn = 0

        real_sql = (
            "SELECT SUM(total_amount) AS total_revenue FROM orders WHERE status = 'delivered'"
        )

        def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal turn
            turn += 1
            captured_retries.extend(_retry_messages_in_last_turn(messages))
            out_tool = _output_tool_name(info)

            if turn == 1:
                # Alucinação: tenta responder sem executar nenhuma query
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            out_tool,
                            {
                                "status": "answered",
                                "answer": "O faturamento foi de R$ 999.999,00.",
                                "sql_query": "SELECT 999999",
                                "assumptions": [],
                            },
                        )
                    ]
                )
            if turn == 2:
                return ModelResponse(
                    parts=[ToolCallPart("validate_and_explain", {"sql": real_sql})]
                )
            if turn == 3:
                return ModelResponse(parts=[ToolCallPart("execute_sql", {"sql": real_sql})])
            # Retorna sql_query divergente para validar que ensure_grounded_answer corrige pelo audit trail
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        out_tool,
                        {
                            "status": "answered",
                            "answer": "O faturamento entregue foi de R$ 4.800,00.",
                            "sql_query": "SELECT 'forged_sql'",
                            "assumptions": [],
                        },
                    )
                ]
            )

        agent = build_sql_agent(model_name="test")
        with agent.override(model=FunctionModel(scripted_model)):
            output = await run_business_question(agent, "Qual o faturamento?", deps)

        assert output.status == "answered"
        assert any("sem executar nenhuma consulta" in msg for msg in captured_retries)
        assert output.sql_query == deps.executed_queries[-1]
        assert "forged_sql" not in (output.sql_query or "")

    async def test_needs_clarification_does_not_require_sql_execution(self) -> None:
        deps = _make_deps()

        def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        _output_tool_name(info),
                        {
                            "status": "needs_clarification",
                            "answer": "Qual período e critério (faturamento ou volume de pedidos) definem 'melhores clientes'?",
                            "sql_query": None,
                            "assumptions": [],
                        },
                    )
                ]
            )

        agent = build_sql_agent(model_name="test")
        with agent.override(model=FunctionModel(scripted_model)):
            output = await run_business_question(agent, "Quais os melhores clientes?", deps)

        assert output.status == "needs_clarification"
        assert output.sql_query is None
        assert deps.executed_queries == []


class TestCircuitBreakersAndInfraErrors:
    async def test_tool_retry_budget_exhaustion_raises_unexpected_behavior(self) -> None:
        deps = _make_deps()

        def always_invalid_sql(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(
                parts=[ToolCallPart("validate_and_explain", {"sql": "DROP TABLE orders"})]
            )

        agent = build_sql_agent(model_name="test")
        with agent.override(model=FunctionModel(always_invalid_sql)):
            with pytest.raises(UnexpectedModelBehavior):
                await run_business_question(agent, "Apague a tabela orders", deps)

    async def test_request_limit_stops_endless_exploration_loop(self) -> None:
        deps = _make_deps()

        def endless_listing(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[ToolCallPart("list_allowed_tables", {})])

        agent = build_sql_agent(model_name="test")
        with agent.override(model=FunctionModel(endless_listing)):
            with pytest.raises(UsageLimitExceeded):
                await run_business_question(agent, "Pergunta em loop infinito", deps)

    async def test_infra_error_aborts_immediately_without_retrying_llm(self) -> None:
        """
        Requer que validate_and_explain e execute_sql capturem apenas SafeDatabaseError
        (deixando DatabaseInfraError propagar direto para HTTP 503).
        """
        executor = FakeQueryExecutor(execute_errors=[DatabaseInfraError("connection refused")])
        deps = _make_deps(executor)
        sql = "SELECT id FROM orders LIMIT 5"
        turn = 0

        def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal turn
            turn += 1
            if turn == 1:
                return ModelResponse(parts=[ToolCallPart("validate_and_explain", {"sql": sql})])
            return ModelResponse(parts=[ToolCallPart("execute_sql", {"sql": sql})])

        agent = build_sql_agent(model_name="test")
        with agent.override(model=FunctionModel(scripted_model)):
            with pytest.raises(DatabaseInfraError, match="connection refused"):
                await run_business_question(agent, "Liste 5 pedidos", deps)

        assert turn == 2
        assert len(executor.execute_calls) == 1
