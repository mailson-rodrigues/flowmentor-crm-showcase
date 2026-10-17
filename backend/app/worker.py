"""Dedicated worker (production): consumes the bus (Redis Streams) and runs jobs/periodic tasks.

    python -m app.worker

Scale horizontally: consumer groups distribute events across instances; jobs use FOR UPDATE SKIP LOCKED.
For Socket.IO to work across processes, configure a message manager (Redis) — see docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from app.core.events import get_bus
from app.core.scheduler import Scheduler
from app.registry import register_all

log = logging.getLogger("flowmentor.worker")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bus = get_bus()
    register_all(bus)
    await bus.start()
    sched = Scheduler()
    await sched.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    log.info("worker em execução")
    try:
        await stop.wait()
    except asyncio.CancelledError:
        pass
    await sched.stop()
    await bus.stop()


if __name__ == "__main__":
    asyncio.run(main())
