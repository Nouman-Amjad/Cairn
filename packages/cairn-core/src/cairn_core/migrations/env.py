"""Alembic environment.

Migrations are forward-only and expand-contract. The rule that matters:
no migration may hold a lock on `trajectory_steps`. Add columns nullable,
backfill in a job, tighten in a later release.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from cairn_core.config import DatabaseSettings
from cairn_core.models import Base

config = context.config
target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", DatabaseSettings().dsn.get_secret_value())


def _include_object(obj, name, type_, reflected, compare_to) -> bool:  # type: ignore[no-untyped-def]
    # pgvector and pg_trgm create their own catalog objects; autogenerate
    # must not try to drop them.
    return not (type_ == "table" and name in {"alembic_version"})


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=_include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
