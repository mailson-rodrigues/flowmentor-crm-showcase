"""RedisStreamsBus contra um Redis real. Só roda com TEST_REDIS_URL (ex.: redis://localhost:6379/15)."""
import asyncio
import os
import uuid

import pytest

from app.core.events import Event, RedisStreamsBus

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_REDIS_URL"), reason="defina TEST_REDIS_URL para testar o Redis Streams")


async def _wait_for(cond, timeout: float = 8.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "timeout aguardando o barramento"
        await asyncio.sleep(0.05)


@pytest.fixture
async def bus():
    b = RedisStreamsBus(os.environ["TEST_REDIS_URL"])
    b.stream = f"test-{uuid.uuid4().hex[:8]}:events"
    b.dlq = b.stream + ":dlq"
    yield b
    await b.redis.delete(b.stream, b.dlq)
    await b.stop()


async def test_publish_consume_and_pattern_match(bus):
    got: list[Event] = []

    async def h(ev: Event) -> None:
        got.append(ev)

    bus.subscribe("ticket.*", h)
    await bus.start()
    await bus.publish(Event(type="ticket.created", org_id="org1", payload={"n": 1}))
    await bus.publish(Event(type="deal.won", org_id="org1", payload={}))
    await bus.publish(Event(type="ticket.closed", org_id="org1", payload={"n": 2}, actor_id="u1"))
    await _wait_for(lambda: len(got) == 2)
    assert [e.type for e in got] == ["ticket.created", "ticket.closed"]
    assert got[1].payload == {"n": 2} and got[1].actor_id == "u1"
    assert await bus.redis.xpending(bus.stream, bus.group) == {"pending": 0, "min": None, "max": None, "consumers": []}


async def test_failing_handler_goes_to_dlq(bus):
    async def boom(ev: Event) -> None:
        raise RuntimeError("falhou")

    bus.subscribe("x.*", boom, name="boom")
    await bus.start()
    await bus.publish(Event(type="x.y", org_id=None, payload={"a": 1}))
    for _ in range(80):
        if await bus.redis.xlen(bus.dlq):
            break
        await asyncio.sleep(0.1)
    entries = await bus.redis.xrange(bus.dlq)
    assert len(entries) == 1 and entries[0][1]["handler"] == "boom"
