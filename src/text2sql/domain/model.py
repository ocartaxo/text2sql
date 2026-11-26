from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class SQLGuardrailError(Exception):
    """Erro estruturado retornado ao agente para orientar o self-repair."""

    def __init__(self, code: str, message: str, hint: str | None = None) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(self.to_agent_feedback())

    def to_agent_feedback(self) -> str:
        base = f"[{self.code}] {self.message}"
        return f"{base} Sugestão: {self.hint}" if self.hint else base


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    role: str
    allowed_schemas: frozenset[str] = field(default_factory=lambda: frozenset({"public"}))
    allowed_tables: frozenset[str] = field(default_factory=frozenset)
    blocked_columns: frozenset[str] = field(default_factory=frozenset)
    max_rows: int = 100
    max_explain_cost: float = 15_000.0
    statement_timeout_ms: int = 5_000


@dataclass(frozen=True, slots=True)
class ValidationResult:
    original_sql: str
    normalized_sql: str
    referenced_tables: tuple[str, ...]
    limit_injected: bool = False


@dataclass(frozen=True, slots=True)
class ExplainPlanResult:
    estimated_cost: float
    estimated_rows: int
    raw_plan: dict[str, Any]


@dataclass(frozen=True, slots=True)
class QueryExecutionResult:
    columns: tuple[str, ...]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float