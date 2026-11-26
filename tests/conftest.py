# tests/conftest.py
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest

from text2sql.config import DEFAULT_PROMPTS_DIR
from text2sql.domain.models import AccessPolicy
from text2sql.domain.policies import get_policy_for_role
from text2sql.guardrails.pipeline import SQLGlotGuardrailPipeline


@pytest.fixture
def analyst_policy() -> AccessPolicy:
    return get_policy_for_role("analyst")


@pytest.fixture
def finance_policy() -> AccessPolicy:
    return get_policy_for_role("finance")


@pytest.fixture
def support_policy() -> AccessPolicy:
    return get_policy_for_role("support")


@pytest.fixture
def pipeline() -> SQLGlotGuardrailPipeline:
    return SQLGlotGuardrailPipeline()


@pytest.fixture(scope="session")
def glossary_text() -> str:
    path = DEFAULT_PROMPTS_DIR / "glossary.yaml"
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture(scope="module")
async def ro_pool() -> AsyncIterator[asyncpg.Pool]:
    dsn = os.getenv("RO_DATABASE_URL")
    if not dsn:
        if os.getenv("CI"):
            pytest.fail("RO_DATABASE_URL não definida no ambiente de CI.")
        pytest.skip("RO_DATABASE_URL não definida; pulando testes de integração PostgreSQL.")

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()
