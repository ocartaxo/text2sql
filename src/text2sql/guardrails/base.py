# src/text2sql/guardrails/base.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sqlglot import exp

from text2sql.domain.models import AccessPolicy


@dataclass(slots=True)
class GuardrailContext:
    ast: exp.Expression
    policy: AccessPolicy
    referenced_tables: set[str] = field(default_factory=set)
    limit_injected: bool = False


class GuardrailRule(Protocol):
    def apply(self, ctx: GuardrailContext) -> None: ...