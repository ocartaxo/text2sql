# src/text2sql/api/routes.py
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.usage import UsageLimits

from text2sql.adapters.postgres import DatabaseInfraError
from text2sql.agent.deps import AgentDeps
from text2sql.agent.orchestrator import run_business_question
from text2sql.api.schemas import QueryRequest, QueryResponse, SSEEvent
from text2sql.domain.policies import get_policy_for_role

if TYPE_CHECKING:
    from text2sql.api.main import AppContainer

router = APIRouter(tags=["query"])


def _build_deps(container: AppContainer, payload: QueryRequest) -> AgentDeps:
    policy = get_policy_for_role(payload.user_role)
    return AgentDeps(
        tenant_id=payload.tenant_id,
        policy=policy,
        validator=container.validator,
        executor=container.executor,
        catalog=container.catalog,
        glossary_text=container.glossary_text,
    )


@router.post("/query", response_model=QueryResponse)
async def post_query(payload: QueryRequest, request: Request) -> QueryResponse:
    container: AppContainer = request.app.state.container
    deps = _build_deps(container, payload)
    started = time.perf_counter()

    try:
        output = await run_business_question(
            container.agent,
            payload.question,
            deps,
            request_limit=container.settings.agent_request_limit,
            total_tokens_limit=container.settings.agent_total_tokens_limit,
        )
    except (UsageLimitExceeded, UnexpectedModelBehavior) as exc:
        raise HTTPException(
            status_code=422,
            detail="Limite de iterações ou orçamento de tokens excedido ao processar a pergunta.",
        ) from exc
    except DatabaseInfraError as exc:
        raise HTTPException(
            status_code=503,
            detail="Banco de dados temporariamente indisponível.",
        ) from exc

    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
    return QueryResponse.from_agent_output(
        output,
        executed_queries_count=len(deps.executed_queries),
        elapsed_ms=elapsed_ms,
    )


@router.post("/query/stream")
async def post_query_stream(payload: QueryRequest, request: Request) -> StreamingResponse:
    container: AppContainer = request.app.state.container
    deps = _build_deps(container, payload)

    async def event_generator() -> AsyncIterator[str]:
        started = time.perf_counter()
        yield SSEEvent(event="step", data={"phase": "starting"}).to_wire()

        try:
            async with container.agent.iter(
                payload.question,
                deps=deps,
                usage_limits=UsageLimits(
                    request_limit=container.settings.agent_request_limit,
                    total_tokens_limit=container.settings.agent_total_tokens_limit,
                ),
            ) as agent_run:
                async for node in agent_run:
                    if Agent.is_call_tools_node(node):
                        for part in node.model_response.parts:
                            if isinstance(part, ToolCallPart):
                                yield SSEEvent(
                                    event="tool_call",
                                    data={
                                        "tool": part.tool_name,
                                        "args": part.args_as_dict(),
                                    },
                                ).to_wire()
                    elif Agent.is_model_request_node(node):
                        for req_part in node.request.parts:
                            if isinstance(req_part, ToolReturnPart):
                                if req_part.outcome == "retried":
                                    yield SSEEvent(
                                        event="tool_retry",
                                        data={
                                            "tool": req_part.tool_name,
                                            "reason": str(req_part.content),
                                        },
                                    ).to_wire()
                                else:
                                    yield SSEEvent(
                                        event="tool_result",
                                        data={"tool": req_part.tool_name},
                                    ).to_wire()
                            elif isinstance(req_part, RetryPromptPart):
                                yield SSEEvent(
                                    event="tool_retry",
                                    data={
                                        "tool": req_part.tool_name or "output_validator",
                                        "reason": str(req_part.content),
                                    },
                                ).to_wire()

                if agent_run.result is None:
                    raise UnexpectedModelBehavior(
                        "Execução do agente encerrou sem resultado final."
                    )

                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
                final_payload = QueryResponse.from_agent_output(
                    agent_run.result.output,
                    executed_queries_count=len(deps.executed_queries),
                    elapsed_ms=elapsed_ms,
                )
                yield SSEEvent(event="final", data=final_payload.model_dump()).to_wire()
        except DatabaseInfraError:
            yield SSEEvent(
                event="error",
                data={
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "Banco de dados temporariamente indisponível.",
                },
            ).to_wire()
        except (UsageLimitExceeded, UnexpectedModelBehavior):
            yield SSEEvent(
                event="error",
                data={
                    "code": "USAGE_LIMIT_EXCEEDED",
                    "message": "Limite de iterações ou orçamento de tokens excedido.",
                },
            ).to_wire()

    return StreamingResponse(event_generator(), media_type="text/event-stream")
