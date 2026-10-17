"""Jobs duráveis (tabela ``scheduled_jobs``) + tarefas periódicas.

* ``schedule_job``: agenda execução futura com deduplicação (ex.: pesquisa de satisfação 3 min após fechar).
* ``Scheduler.run``: laço que executa jobs vencidos e tarefas periódicas (SLA monitor, expiração de mídia...).
Em múltiplos workers, os jobs são reivindicados com ``FOR UPDATE SKIP LOCKED`` (PostgreSQL).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, session_scope, utcnow
from app.models import ScheduledJob

log = logging.getLogger("flowmentor.scheduler")

JobHandler = Callable[[dict[str, Any]], Awaitable[None]]
_job_handlers: dict[str, JobHandler] = {}
_periodic: list[tuple[str, float, Callable[[], Awaitable[None]]]] = []


def job(job_type: str):
    def deco(fn: JobHandler) -> JobHandler:
        _job_handlers[job_type] = fn
        return fn

    return deco


def periodic(name: str, every_seconds: float):
    def deco(fn: Callable[[], Awaitable[None]]):
        _periodic.append((name, every_seconds, fn))
        return fn

    return deco


async def schedule_job(
    session: AsyncSession, job_type: str, payload: dict, run_at: datetime, org_id: uuid.UUID | None = None, dedupe_key: str | None = None
) -> ScheduledJob | None:
    if dedupe_key:
        exists = (await session.execute(sa.select(ScheduledJob.id).where(ScheduledJob.dedupe_key == dedupe_key))).first()
        if exists:
            return None
    j = ScheduledJob(job_type=job_type, payload=payload, run_at=run_at, organization_id=org_id, dedupe_key=dedupe_key)
    session.add(j)
    await session.flush()
    return j


async def cancel_jobs(session: AsyncSession, dedupe_key: str) -> None:
    await session.execute(
        sa.update(ScheduledJob).where(ScheduledJob.dedupe_key == dedupe_key, ScheduledJob.status == "pending").values(status="cancelled")
    )


async def run_due_jobs(limit: int = 25) -> int:
    """Executa jobs vencidos. Retorna quantos foram processados."""
    async with SessionLocal() as s:
        q = sa.select(ScheduledJob).where(ScheduledJob.status == "pending", ScheduledJob.run_at <= utcnow()).order_by(ScheduledJob.run_at).limit(limit)
        if s.bind.dialect.name == "postgresql":
            q = q.with_for_update(skip_locked=True)
        jobs = (await s.execute(q)).scalars().all()
        for j in jobs:
            j.status = "running"
            j.attempts += 1
        await s.commit()
        batch = [(j.id, j.job_type, dict(j.payload or {}), j.attempts) for j in jobs]
    for jid, jtype, payload, attempts in batch:
        handler = _job_handlers.get(jtype)
        err = None
        try:
            if handler is None:
                raise RuntimeError(f"job '{jtype}' sem handler")
            await handler(payload)
        except Exception as exc:  # noqa: BLE001
            log.exception("job %s (%s) falhou", jtype, jid)
            err = str(exc)[:500]
        async with session_scope() as s:
            j = await s.get(ScheduledJob, jid)
            if err is None:
                j.status, j.finished_at = "done", utcnow()
            elif attempts >= 3:
                j.status, j.last_error, j.finished_at = "failed", err, utcnow()
            else:
                j.status, j.last_error = "pending", err
                from datetime import timedelta

                j.run_at = utcnow() + timedelta(seconds=30 * attempts)
    return len(batch)


class Scheduler:
    def __init__(self, tick: float = 2.0) -> None:
        self.tick = tick
        self._task: asyncio.Task | None = None
        self._last: dict[str, float] = {}

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await run_due_jobs()
                now = loop.time()
                for name, every, fn in _periodic:
                    if now - self._last.get(name, 0) >= every:
                        self._last[name] = now
                        try:
                            await fn()
                        except Exception:  # noqa: BLE001
                            log.exception("tarefa periódica %s falhou", name)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("erro no scheduler")
            await asyncio.sleep(self.tick)
