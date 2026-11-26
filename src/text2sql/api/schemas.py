from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from text2sql.agent.orchestrator import AnalystResponse


class QueryRequest(BaseModel):
    question: str = Field(
        min_length=3,
        max_length=1_000,
        description="Pergunta de negócio em linguagem natural.",
    )
    tenant_id: str = Field(
        min_length=1,
        max_length=64,
        description="Identificador de isolamento multi-tenant aplicado via RLS.",
    )
    user_role: Literal["analyst", "finance", "support"] = Field(
        default="analyst",
        description="Perfil de permissão que determina políticas de tabela e PII.",
    )


class QueryResponse(BaseModel):
    status: Literal["answered", "needs_clarification", "out_of_scope"]
    answer: str
    sql_query: str | None = None
    assumptions: list[str] = Field(default_factory=list)
    executed_queries_count: int
    elapsed_ms: float

    @classmethod
    def from_agent_output(
        cls,
        output: AnalystResponse,
        *,
        executed_queries_count: int,
        elapsed_ms: float,
    ) -> QueryResponse:
        return cls(
            status=output.status,
            answer=output.answer,
            sql_query=output.sql_query,
            assumptions=output.assumptions,
            executed_queries_count=executed_queries_count,
            elapsed_ms=elapsed_ms,
        )


class SSEEvent(BaseModel):
    event: Literal["step", "tool_call", "tool_retry", "tool_result", "final", "error"]
    data: dict[str, Any]

    def to_wire(self) -> str:
        return f"event: {self.event}\ndata: {self.model_dump_json(include={'data'})}\n\n"