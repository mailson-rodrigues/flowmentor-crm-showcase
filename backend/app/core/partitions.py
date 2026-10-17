"""Maintenance of the monthly partitions of the ``messages`` table (PostgreSQL)."""
from __future__ import annotations

import logging

import sqlalchemy as sa

from app.core.scheduler import periodic
from app.db import SessionLocal

log = logging.getLogger("flowmentor.partitions")


@periodic("db.partitions", 24 * 3600)
async def ensure_partitions() -> None:
    async with SessionLocal() as s:
        if s.bind.dialect.name != "postgresql":
            return
        await s.execute(sa.text("SELECT ensure_message_partitions(1, 6)"))
        await s.commit()
        log.info("partições de messages garantidas (6 meses à frente)")
