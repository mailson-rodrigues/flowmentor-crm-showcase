"""Barramento de eventos assíncrono (Ports & Adapters).

* ``InMemoryBus``     → dev/test e instalações single-node.
* ``RedisStreamsBus`` → produção: Redis Streams + consumer groups (ordenação, persistência, escala horizontal).
  A porta ``EventBus`` permite trocar por RabbitMQ/Kafka sem tocar nos módulos de negócio.

Convenção de nomes:  ``<entidade>.<acao>``  (ex.: ``ticket.created``, ``deal.won``, ``sla.breached``).
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import socket
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import after_commit

log = logging.getLogger("flowmentor.events")

Handler = Callable[["Event"], Awaitable[None]]


@dataclass
class Event:
    type: str
    org_id: str | None
    payload: dict[str, Any]
    actor_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_json(cls, raw: str) -> "Event":
        return cls(**json.loads(raw))


class EventBus:
    """Porta do barramento."""

    def __init__(self) -> None:
        self._subs: list[tuple[str, str, Handler]] = []

    def subscribe(self, pattern: str, handler: Handler, name: str | None = None) -> None:
        self._subs.append((pattern, name or getattr(handler, "__name__", "handler"), handler))

    async def publish(self, event: Event) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def start(self) -> None:  # pragma: no cover
        pass

    async def stop(self) -> None:  # pragma: no cover
        pass

    async def drain(self, timeout: float = 5.0) -> None:
        """Aguarda o processamento dos eventos pendentes (útil em testes)."""

    async def _dispatch(self, event: Event) -> None:
        for pattern, name, handler in self._subs:
            if fnmatch.fnmatch(event.type, pattern):
                try:
                    await handler(event)
                except Exception:
                    log.exception("handler %s falhou para evento %s (%s)", name, event.type, event.id)
                    await self._dead_letter(event, name)

    async def _dead_letter(self, event: Event, handler_name: str) -> None:
        pass


class InMemoryBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.dead: list[tuple[Event, str]] = []

    async def publish(self, event: Event) -> None:
        await self._queue.put(event)

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="event-bus-memory")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._dispatch(event)
            finally:
                self._queue.task_done()

    async def drain(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._queue.join(), timeout)

    async def _dead_letter(self, event: Event, handler_name: str) -> None:
        self.dead.append((event, handler_name))


class RedisStreamsBus(EventBus):
    def __init__(self, url: str) -> None:
        super().__init__()
        import redis.asyncio as aioredis

        self.redis = aioredis.from_url(url, decode_responses=True)
        self.stream = f"{settings.event_stream_prefix}:events"
        self.dlq = f"{settings.event_stream_prefix}:events:dlq"
        self.group = settings.event_consumer_group
        self.consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def publish(self, event: Event) -> None:
        await self.redis.xadd(self.stream, {"data": event.to_json()}, maxlen=200_000, approximate=True)

    async def start(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="$", mkstream=True)
        except Exception as exc:  # BUSYGROUP: já existe
            if "BUSYGROUP" not in str(exc):
                raise
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="event-bus-redis")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self.redis.aclose()

    async def _run(self) -> None:
        last_claim = 0.0
        loop = asyncio.get_running_loop()
        while not self._stopping:
            try:
                if loop.time() - last_claim > 30:
                    last_claim = loop.time()
                    await self._claim_stale()
                resp = await self.redis.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=50, block=2000)
                for _, messages in resp or []:
                    for msg_id, fields in messages:
                        await self._handle(msg_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("erro no loop do Redis Streams; tentando novamente em 2s")
                await asyncio.sleep(2)

    async def _handle(self, msg_id: str, fields: dict) -> None:
        try:
            event = Event.from_json(fields["data"])
            await self._dispatch(event)
        except Exception:
            log.exception("evento inválido %s", msg_id)
            await self.redis.xadd(self.dlq, {"data": fields.get("data", ""), "error": "parse"})
        finally:
            await self.redis.xack(self.stream, self.group, msg_id)

    async def _claim_stale(self) -> None:
        """Reivindica mensagens de consumidores que morreram sem confirmar (> 60s)."""
        try:
            res = await self.redis.xautoclaim(self.stream, self.group, self.consumer, min_idle_time=60_000, count=20)
            for msg_id, fields in res[1]:
                if fields:
                    await self._handle(msg_id, fields)
        except Exception:
            log.debug("xautoclaim indisponível", exc_info=True)

    async def _dead_letter(self, event: Event, handler_name: str) -> None:
        await self.redis.xadd(self.dlq, {"data": event.to_json(), "handler": handler_name})


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = RedisStreamsBus(settings.redis_url) if settings.redis_url else InMemoryBus()
    return _bus


def set_bus(bus: EventBus | None) -> None:
    global _bus
    _bus = bus


async def publish(type_: str, org_id: Any, payload: dict[str, Any], actor_id: Any = None) -> Event:
    event = Event(type=type_, org_id=str(org_id) if org_id else None, payload=payload, actor_id=str(actor_id) if actor_id else None)
    await get_bus().publish(event)
    return event


def emit(session: AsyncSession, type_: str, org_id: Any, payload: dict[str, Any], actor_id: Any = None) -> None:
    """Publica o evento SOMENTE após o commit da transação corrente (evita consumidores lerem estado inexistente)."""

    async def _go() -> None:
        await publish(type_, org_id, payload, actor_id)

    after_commit(session, _go)
