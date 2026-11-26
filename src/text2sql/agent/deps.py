from __future__ import annotations

from dataclasses import dataclass, field

from text2sql.domain.models import AccessPolicy
from text2sql.domain.ports import QueryExecutor, SchemaCatalog, SQLValidator


@dataclass(slots=True)
class AgentDeps:
    tenant_id: str
    policy: AccessPolicy
    validator: SQLValidator
    executor: QueryExecutor
    catalog: SchemaCatalog
    glossary_text: str
    validated_queries: set[str] = field(default_factory=set)
    executed_queries: list[str] = field(default_factory=list)