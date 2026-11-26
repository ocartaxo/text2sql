from __future__ import annotations

from text2sql.domain.models import AccessPolicy

BASE_PII_COLUMNS: frozenset[str] = frozenset(
    {
        "email",
        "cpf",
        "password_hash",
    }
)

ROLE_POLICIES: dict[str, AccessPolicy] = {
    "analyst": AccessPolicy(
        role="analyst",
        allowed_schemas=frozenset({"public"}),
        allowed_tables=frozenset({"orders", "order_items", "customers", "products"}),
        blocked_columns=BASE_PII_COLUMNS,
        max_rows=100,
        max_explain_cost=15_000.0,
        statement_timeout_ms=5_000,
    ),
    "finance": AccessPolicy(
        role="finance",
        allowed_schemas=frozenset({"public"}),
        allowed_tables=frozenset({"orders", "order_items", "customers", "products"}),
        blocked_columns=BASE_PII_COLUMNS,
        max_rows=250,
        max_explain_cost=25_000.0,
        statement_timeout_ms=8_000,
    ),
    "support": AccessPolicy(
        role="support",
        allowed_schemas=frozenset({"public"}),
        allowed_tables=frozenset({"orders", "customers"}),
        blocked_columns=BASE_PII_COLUMNS,
        max_rows=50,
        max_explain_cost=8_000.0,
        statement_timeout_ms=3_000,
    ),
}


class UnknownRoleError(ValueError):
    """Perfil não mapeado em ROLE_POLICIES (fail-closed)."""


def get_policy_for_role(role: str) -> AccessPolicy:
    normalized = role.strip().lower()
    policy = ROLE_POLICIES.get(normalized)
    if policy is None:
        raise UnknownRoleError(f"Perfil de acesso não autorizado: '{role}'.")
    return policy