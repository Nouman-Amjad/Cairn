"""Async engine and session plumbing.

One engine per process, created lazily. Sessions are per-request and never
shared across tasks. `statement_timeout` is set on every connection: an agent
that hangs on a lock is indistinguishable from one that is thinking, and the
whole loop has a 180-second wall clock to respect.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from cairn_core.config import DatabaseSettings


@lru_cache(maxsize=1)
def engine(cfg: DatabaseSettings | None = None) -> AsyncEngine:
    cfg = cfg or DatabaseSettings()
    # pool_size=0 means "no pool". Tests set it: an asyncpg connection is
    # bound to the event loop that opened it, and a pooled connection reused
    # across per-test loops fails at teardown with "event loop is closed".
    pool_args: dict[str, object] = (
        {"poolclass": NullPool}
        if cfg.pool_size == 0
        else {
            "pool_size": cfg.pool_size,
            "max_overflow": cfg.max_overflow,
            "pool_timeout": cfg.pool_timeout_s,
            "pool_pre_ping": True,
        }
    )
    return create_async_engine(
        cfg.dsn.get_secret_value(),
        echo=cfg.echo,
        **pool_args,
        connect_args={
            "server_settings": {
                "statement_timeout": str(cfg.statement_timeout_ms),
                "application_name": "cairn",
            }
        },
    )


@lru_cache(maxsize=1)
def sessionmaker(cfg: DatabaseSettings | None = None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine(cfg), expire_on_commit=False, autoflush=False, class_=AsyncSession
    )


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """Commit on clean exit, roll back on exception. No implicit flushes in
    between, so a partially-written step never becomes visible."""
    async with sessionmaker()() as sess:
        try:
            yield sess
            await sess.commit()
        except Exception:
            await sess.rollback()
            raise


async def healthcheck() -> bool:
    from sqlalchemy import text

    try:
        async with engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def dispose() -> None:
    await engine().dispose()
