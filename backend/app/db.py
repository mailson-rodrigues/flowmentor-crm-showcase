"""Async engine, session and portable types (PostgreSQL in production, SQLite in dev/tests)."""
from __future__ import annotations

import enum
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(sa.types.TypeDecorator):
    """Timezone-aware DateTime that always returns aware UTC datetimes (including on SQLite)."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


def enum_col(py_enum: type[enum.Enum], name: str) -> sa.Enum:
    """Native enum on PostgreSQL (as in the reference DDL) and VARCHAR+CHECK on SQLite."""
    return sa.Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e], native_enum=True)


class Base(DeclarativeBase):
    pass


class UUIDPk:
    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow)


def _make_engine() -> AsyncEngine:
    kwargs: dict[str, Any] = {"echo": settings.db_echo}
    if settings.is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in settings.database_url:
            kwargs["poolclass"] = StaticPool
    else:
        kwargs.update(pool_size=20, max_overflow=20, pool_pre_ping=True)
    return create_async_engine(settings.database_url, **kwargs)


engine: AsyncEngine = _make_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _run_after_commit(session: AsyncSession) -> None:
    hooks = session.info.pop("after_commit", [])
    for hook in hooks:
        try:
            await hook()
        except Exception:  # hooks (e.g. publishing an event) must never take the request down
            import logging

            logging.getLogger("flowmentor.db").exception("after_commit hook falhou")


def after_commit(session: AsyncSession, hook) -> None:
    """Schedules a coroutine to run only after a successful commit (simplified outbox)."""
    session.info.setdefault("after_commit", []).append(hook)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        await _run_after_commit(session)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Unit of work for workers/jobs (outside the request/response cycle)."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        await _run_after_commit(session)


async def init_models() -> None:
    """Creates the schema (dev/test). In production use `alembic upgrade head`."""
    import app.models  # noqa: F401  (registra os mapeamentos)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
