# tests/contract/test_api_routes.py
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import asyncpg
import pydantic_ai.models
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from text2sql.adapters.catalog import PostgresSchemaCatalog
from text2sql.adapters.postgres import DatabaseInfraError, PostgresQueryExecutor
from text2sql.agent.orchestrator import build_sql_agent
from text2sql.api.main import AppContainer, create_app
from text2sql.config import Settings
from text2sql.domain.models import AccessPolicy, ExplainPlanResult, QueryExecutionResult
from text2sql.guardrails.pipeline import SQLGlotGuardrailPipeline

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def block_real_llm_calls() -> Iterator[None]:
    previous = pydantic_ai.models.ALLOW_MODEL_REQUESTS
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    try:
        yield
    finally:
        pydantic_ai.models.ALLOW_MODEL_REQUESTS = previous


class FakeConnection:
    async def fetchval(self, query: str) -> int:
        return 1


class _AcquireContext:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, *args: object) -> None:
        return None


class FakePool:
    def __init__(self) -> None:
        self._conn = FakeConnection()
        self.closed = False

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self._conn)

    async def close(self) -> None:
        self.closed = True


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
    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        explain_error: Exception | None = None,
        execute_error: Exception | None = None,
    ) -> None:
        self._rows = rows if rows is not None else [{"total_revenue": 4800.0}]
        self._explain_error = explain_error
        self._execute_error = execute_error

    async def explain(
        self,
        sql: str,
        *,
        tenant_id: str,
        policy: AccessPolicy,
    ) -> ExplainPlanResult:
        if self._explain_error is not None:
            raise self._explain_error
        return ExplainPlanResult(
            estimated_cost=95.0,
            estimated_rows=5,
            raw_plan={"Node Type": "Index Scan", "Total Cost": 95.0},
        )

    async def execute(
        self,
        sql: str,
        *,
        tenant_id: str,
        policy: AccessPolicy,
    ) -> QueryExecutionResult:
        if self._execute_error is not None:
            raise self._execute_error
        columns = tuple(self._rows[0].keys()) if self._rows else ()
        return QueryExecutionResult(
            columns=columns,
            rows=self._rows,
            row_count=len(self._rows),
            truncated=False,
            elapsed_ms=3.5,
        )


def _output_tool_name(info: AgentInfo) -> str:
    assert info.output_tools, "Agent precisa declarar output_type estruturado."
    return info.output_tools[0].name


def _parse_sse_events(raw_body: str) -> list[dict[str, Any]]:
    """Faz parse do wire format `event: <tipo>\\ndata: {"data": {...}}\\n\\n`."""
    events: list[dict[str, Any]] = []
    for block in raw_body.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        payload: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                decoded = json.loads(line.removeprefix("data: "))
                payload = decoded.get("data", decoded)
        events.append({"event": event_name, "data": payload})
    return events


def _build_test_app(executor: FakeQueryExecutor | None = None) -> FastAPI:
    cfg = Settings(enable_otel=False)
    app = create_app(cfg)
    fake_pool = FakePool()
    app.state.container = AppContainer(
        settings=cfg,
        pool=cast("asyncpg.Pool", fake_pool),
        agent=build_sql_agent(model_name="test"),
        validator=SQLGlotGuardrailPipeline(),
        executor=cast("PostgresQueryExecutor", executor or FakeQueryExecutor()),
        catalog=cast("PostgresSchemaCatalog", FakeSchemaCatalog()),
        glossary_text="faturamento_entregue: SUM(orders.total_amount) WHERE status = 'delivered'",
    )
    return app


@pytest.fixture
async def api_client() -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    app = _build_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, app


class TestHealthEndpoint:
    async def test_health_returns_ok(
        self,
        api_client: tuple[AsyncClient, FastAPI],
    ) -> None:
        client, _ = api_client
        response = await client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestSyncQueryEndpoint:
    async def test_post_query_happy_path_returns_structured_response(
        self,
        api_client: tuple[AsyncClient, FastAPI],
    ) -> None:
        client, app = api_client
        container: AppContainer = app.state.container
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
                            "answer": "O faturamento total entregue foi de R$ 4.800,00.",
                            "sql_query": sql,
                            "assumptions": ["Considerados apenas pedidos entregues."],
                        },
                    )
                ]
            )

        with container.agent.override(model=FunctionModel(scripted_model)):
            response = await client.post(
                "/v1/query",
                json={
                    "question": "Qual foi o faturamento total dos pedidos entregues?",
                    "tenant_id": "tenant_a",
                    "user_role": "analyst",
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "answered"
        assert "4.800,00" in body["answer"]
        assert body["sql_query"] is not None
        assert "LIMIT 100" in body["sql_query"]
        assert body["executed_queries_count"] == 1
        assert body["elapsed_ms"] >= 0.0

    @pytest.mark.parametrize(
        "invalid_payload",
        [
            {"question": "oi", "tenant_id": "tenant_a", "user_role": "analyst"},
            {"question": "Qual o faturamento?", "tenant_id": "", "user_role": "analyst"},
            {"question": "Qual o faturamento?", "tenant_id": "tenant_a", "user_role": "root"},
        ],
    )
    async def test_post_query_rejects_invalid_request_schema_with_422(
        self,
        api_client: tuple[AsyncClient, FastAPI],
        invalid_payload: dict[str, Any],
    ) -> None:
        client, _ = api_client
        response = await client.post("/v1/query", json=invalid_payload)

        assert response.status_code == 422

    async def test_post_query_returns_422_when_usage_limit_exceeded(
        self,
        api_client: tuple[AsyncClient, FastAPI],
    ) -> None:
        client, app = api_client
        container: AppContainer = app.state.container

        def endless_loop(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[ToolCallPart("list_allowed_tables", {})])

        with container.agent.override(model=FunctionModel(endless_loop)):
            response = await client.post(
                "/v1/query",
                json={
                    "question": "Pergunta que estoura o limite de iterações",
                    "tenant_id": "tenant_a",
                    "user_role": "analyst",
                },
            )

        assert response.status_code == 422
        assert "Limite de iterações" in response.json()["detail"]

    async def test_post_query_returns_503_on_database_infra_error(self) -> None:
        failing_executor = FakeQueryExecutor(
            execute_error=DatabaseInfraError("connection refused")
        )
        app = _build_test_app(executor=failing_executor)
        container: AppContainer = app.state.container
        sql = "SELECT id FROM orders LIMIT 5"
        turn = 0

        def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal turn
            turn += 1
            if turn == 1:
                return ModelResponse(parts=[ToolCallPart("validate_and_explain", {"sql": sql})])
            return ModelResponse(parts=[ToolCallPart("execute_sql", {"sql": sql})])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with container.agent.override(model=FunctionModel(scripted_model)):
                response = await client.post(
                    "/v1/query",
                    json={
                        "question": "Liste 5 pedidos",
                        "tenant_id": "tenant_a",
                        "user_role": "analyst",
                    },
                )

        assert response.status_code == 503
        assert "indisponível" in response.json()["detail"]


class TestStreamingSSEEndpoint:
    async def test_stream_emits_steps_tool_retry_and_final_event(
        self,
        api_client: tuple[AsyncClient, FastAPI],
    ) -> None:
        client, app = api_client
        container: AppContainer = app.state.container
        valid_sql = "SELECT SUM(total_amount) AS total_revenue FROM orders WHERE status = 'delivered'"
        turn = 0

        def scripted_model_with_retry(
            messages: list[ModelMessage],
            info: AgentInfo,
        ) -> ModelResponse:
            nonlocal turn
            turn += 1
            if turn == 1:
                # 1ª tentativa viola SELECT * -> gera evento SSE `tool_retry`
                return ModelResponse(
                    parts=[ToolCallPart("validate_and_explain", {"sql": "SELECT * FROM orders"})]
                )
            if turn == 2:
                # 2ª tentativa corrige a AST -> gera `tool_result`
                return ModelResponse(
                    parts=[ToolCallPart("validate_and_explain", {"sql": valid_sql})]
                )
            if turn == 3:
                return ModelResponse(parts=[ToolCallPart("execute_sql", {"sql": valid_sql})])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        _output_tool_name(info),
                        {
                            "status": "answered",
                            "answer": "Faturamento entregue: R$ 4.800,00.",
                            "sql_query": valid_sql,
                            "assumptions": [],
                        },
                    )
                ]
            )

        with container.agent.override(model=FunctionModel(scripted_model_with_retry)):
            response = await client.post(
                "/v1/query/stream",
                json={
                    "question": "Qual o faturamento entregue?",
                    "tenant_id": "tenant_a",
                    "user_role": "analyst",
                },
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        events = _parse_sse_events(response.text)
        event_types = [item["event"] for item in events]

        assert event_types[0] == "step"
        assert "tool_call" in event_types
        assert "tool_retry" in event_types
        assert "tool_result" in event_types
        assert event_types[-1] == "final"

        retry_events = [item for item in events if item["event"] == "tool_retry"]
        assert len(retry_events) == 1
        assert retry_events[0]["data"]["tool"] == "validate_and_explain"
        assert "SELECT_STAR_FORBIDDEN" in retry_events[0]["data"]["reason"]

        final_data = events[-1]["data"]
        assert final_data["status"] == "answered"
        assert final_data["executed_queries_count"] == 1
        assert "LIMIT 100" in final_data["sql_query"]

    async def test_stream_emits_error_event_on_database_infra_failure(self) -> None:
        failing_executor = FakeQueryExecutor(
            explain_error=DatabaseInfraError("pool timeout")
        )
        app = _build_test_app(executor=failing_executor)
        container: AppContainer = app.state.container
        sql = "SELECT id FROM orders LIMIT 5"

        def scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[ToolCallPart("validate_and_explain", {"sql": sql})])

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with container.agent.override(model=FunctionModel(scripted_model)):
                response = await client.post(
                    "/v1/query/stream",
                    json={
                        "question": "Liste 5 pedidos",
                        "tenant_id": "tenant_a",
                        "user_role": "analyst",
                    },
                )

        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["code"] == "DATABASE_UNAVAILABLE"

    async def test_stream_emits_error_event_on_usage_limit_exceeded(
        self,
        api_client: tuple[AsyncClient, FastAPI],
    ) -> None:
        client, app = api_client
        container: AppContainer = app.state.container

        def endless_loop(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[ToolCallPart("list_allowed_tables", {})])

        with container.agent.override(model=FunctionModel(endless_loop)):
            response = await client.post(
                "/v1/query/stream",
                json={
                    "question": "Loop infinito no stream",
                    "tenant_id": "tenant_a",
                    "user_role": "analyst",
                },
            )

        assert response.status_code == 200
        events = _parse_sse_events(response.text)
        assert events[-1]["event"] == "error"
        assert events[-1]["data"]["code"] == "USAGE_LIMIT_EXCEEDED"