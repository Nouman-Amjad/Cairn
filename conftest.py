"""Test-wide defaults.

Set before any Cairn module is imported: `settings()` is cached for the life
of the process, so an env var applied later has no effect and produces a very
confusing hour.
"""

from __future__ import annotations

import os

os.environ.setdefault("CAIRN_ENV", "test")
os.environ.setdefault("CAIRN_S3_BUCKET", ":memory:")
os.environ.setdefault("CAIRN_POLICY_ENABLED", "false")
os.environ.setdefault("CAIRN_OTEL_ENDPOINT", "")
os.environ.setdefault("CAIRN_BACKEND_KUBE_API_IN_CLUSTER", "false")
os.environ.setdefault("CAIRN_AUTH_INTERNAL_JWT_KEY", "test-key-that-is-long-enough-32b!!")

import pytest

#: Integration tests need a real Postgres with pgvector: the schema uses HNSW
#: indexes, generated tsvector columns, partial indexes and LISTEN/NOTIFY, and
#: a SQLite stand-in would test none of the behaviour that matters.
#:   docker run -d --name cairn-pg -e POSTGRES_PASSWORD=cairn \
#:     -e POSTGRES_USER=cairn -e POSTGRES_DB=cairn -p 55432:5432 \
#:     pgvector/pgvector:pg16
TEST_DSN = os.environ.get(
    "CAIRN_TEST_DB_DSN", "postgresql+asyncpg://cairn:cairn@localhost:55432/cairn"
)
os.environ.setdefault("CAIRN_DB_DSN", TEST_DSN)
os.environ.setdefault("CAIRN_DB_POOL_SIZE", "0")  # NullPool: see cairn_core.db


def pytest_configure(config) -> None:  # type: ignore[no-untyped-def]
    config.addinivalue_line("markers", "requires_db: needs a live Postgres+pgvector")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def db_available() -> bool:
    import asyncio

    from cairn_core.db import healthcheck

    return asyncio.run(healthcheck())


@pytest.fixture
async def db(db_available):  # type: ignore[no-untyped-def]
    """A clean database for one test.

    Truncate rather than recreate: the migration is slow, and truncating
    every table proves the FK cascade wiring works on every run.
    """
    if not db_available:
        pytest.skip("no Postgres available (see conftest.TEST_DSN)")

    from sqlalchemy import text

    from cairn_core.db import session

    async with session() as sess:
        await sess.execute(
            text(
                "TRUNCATE trajectories, trajectory_steps, approvals, artifacts, "
                "runbook_chunks, trajectory_embeddings RESTART IDENTITY CASCADE"
            )
        )
        # audit_events has an append-only trigger; TRUNCATE bypasses it, which
        # is exactly why the trigger is on UPDATE/DELETE and not on TRUNCATE.
        await sess.execute(text("TRUNCATE audit_events RESTART IDENTITY"))
    yield
