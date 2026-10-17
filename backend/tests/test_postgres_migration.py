"""Validates the schema created by Alembic on a real PostgreSQL (`messages` partitions, native enums, append-only audit_logs).

Only runs with TEST_DATABASE_URL pointing to a disposable PostgreSQL."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from app.core.partitions import ensure_partitions
from app.core.events import InMemoryBus, set_bus
from app.db import Base, SessionLocal, engine
from app.main import fastapi_app
from app.registry import register_all
from app.seed import bootstrap

from .conftest import login, meta_payload

pytestmark = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="requer PostgreSQL (TEST_DATABASE_URL)")
BACKEND = Path(__file__).resolve().parents[1]


def alembic(*args: str) -> None:
    r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, capture_output=True, text=True, env=os.environ.copy())
    assert r.returncode == 0, r.stderr[-2000:]


async def wipe() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))


async def test_alembic_schema_and_app_on_postgres():
    await wipe()
    alembic("upgrade", "head")
    bus = InMemoryBus()
    try:
        async with engine.connect() as conn:
            kinds = dict((await conn.execute(sa.text("select relname, relkind::text from pg_class where relname like 'messages%' and relkind in ('r','p')"))).all())
            assert kinds["messages"] == "p" and "messages_default" in kinds and sum(k.startswith("messages_20") for k in kinds) >= 6
            enums = {r[0] for r in (await conn.execute(sa.text("select typname from pg_type where typtype='e'"))).all()}
            assert {"ticket_status", "message_status", "deal_stage"} <= enums

        # daily partition job: idempotent and extends the window (today -1 .. +6 months = 8 monthly partitions)
        async def monthly() -> int:
            async with engine.connect() as conn:
                return (await conn.execute(sa.text("select count(*) from pg_tables where tablename ~ '^messages_[0-9]{4}_[0-9]{2}$'"))).scalar()

        before = await monthly()
        await ensure_partitions()
        assert await monthly() == before
        async with engine.begin() as conn:
            await conn.execute(sa.text("select ensure_message_partitions(1, 12)"))
        assert await monthly() == before + 6

        set_bus(bus)
        register_all(bus)
        await bus.start()
        await bootstrap()
        async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as c:
            for i in range(2):
                r = await c.post("/webhooks/whatsapp", json=meta_payload(text=f"Olá {i}", wa_id="5562911112222"))
                assert r.status_code == 200, r.text
            await bus.drain()
            h = await login(c, "atendente1@flowmentor.com.br")
            cid = (await c.post("/api/v1/contacts", headers=h, json={"wa_id": "(62) 99999-1234", "name": "Fulano"})).json()["id"]
            assert (await c.post(f"/api/v1/contacts/{cid}/reveal", headers=h)).status_code == 200  # grava audit_logs

        async with engine.connect() as conn:
            rows = (await conn.execute(sa.text("select tableoid::regclass::text, count(*) from messages group by 1"))).all()
            assert rows and sum(n for _, n in rows) >= 2 and all(t.startswith("messages_") for t, _ in rows)

        # audit_logs: append-only (UPDATE/DELETE/TRUNCATE blocked by trigger)
        async with SessionLocal() as s:
            assert (await s.execute(sa.text("select count(*) from audit_logs"))).scalar() > 0
        for stmt in ("update audit_logs set action='x'", "delete from audit_logs", "truncate audit_logs"):
            async with engine.connect() as conn:
                with pytest.raises(sa.exc.DBAPIError, match="append-only"):
                    await conn.execute(sa.text(stmt))
    finally:
        await bus.stop()
        set_bus(None)
        await engine.dispose()
        alembic("downgrade", "base")
        async with engine.connect() as conn:
            assert (await conn.execute(sa.text("select count(*) from pg_type where typtype='e'"))).scalar() == 0
            left = {r[0] for r in (await conn.execute(sa.text("select tablename from pg_tables where schemaname='public'"))).all()}
            assert left <= {"alembic_version"}
