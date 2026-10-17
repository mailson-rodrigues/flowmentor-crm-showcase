"""Fixtures: in-memory SQLite (default), in-memory bus, Meta in sandbox and cheap Argon2.

Environment-variable overrides (e.g. to validate against real PostgreSQL/Redis):
  TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/flowmentor_test  (the schema is recreated/dropped on every test!)
  TEST_REDIS_URL=redis://localhost:6379/15  (enables the RedisStreamsBus tests)
"""
import os
import secrets

PASSWORD = secrets.token_urlsafe(16)  # ephemeral password of the demo users (generated on every test run)
ADMIN_PASSWORD = secrets.token_urlsafe(16)

os.environ.update({
    "ENVIRONMENT": "test", "DATABASE_URL": os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///:memory:", "META_MODE": "sandbox", "SEED_DEMO_DATA": "true",
    "SECRET_KEY": "test-secret-key-test-secret-key-test-secret-key-1234", "RATE_LIMIT_PER_MINUTE": "100000", "RATE_LIMIT_AUTH_PER_MINUTE": "100000",
    "STORAGE_LOCAL_PATH": "./.test-storage", "RUN_WORKERS_IN_PROCESS": "false", "REDIS_URL": os.environ.get("TEST_REDIS_URL", ""), "META_APP_SECRET": "",
    "DEMO_USER_PASSWORD": PASSWORD, "BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD,
})

import pytest_asyncio  # noqa: E402
from argon2 import PasswordHasher  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import app.security.passwords as passwords  # noqa: E402

passwords._hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)  # acelera testes

from app.core.events import InMemoryBus, set_bus  # noqa: E402
from app.core.ratelimit import limiter  # noqa: E402
from app.db import Base, engine, init_models  # noqa: E402
from app.main import fastapi_app  # noqa: E402
from app.registry import register_all  # noqa: E402
from app.seed import bootstrap  # noqa: E402


@pytest_asyncio.fixture
async def env():
    await init_models()
    await bootstrap()
    bus = InMemoryBus()
    set_bus(bus)
    register_all(bus)
    await bus.start()
    limiter.reset()
    yield bus
    await bus.stop()
    set_bus(None)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client(env):
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as c:
        yield c


async def login(client, email: str, password: str = PASSWORD) -> dict:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest_asyncio.fixture
async def admin(client):
    return await login(client, "admin@flowmentor.com.br", ADMIN_PASSWORD)


@pytest_asyncio.fixture
async def supervisor(client):
    return await login(client, "supervisor@flowmentor.com.br")


@pytest_asyncio.fixture
async def consultor(client):
    return await login(client, "consultor@flowmentor.com.br")


@pytest_asyncio.fixture
async def agent1(client):
    return await login(client, "atendente1@flowmentor.com.br")


@pytest_asyncio.fixture
async def agent2(client):
    return await login(client, "atendente2@flowmentor.com.br")


@pytest_asyncio.fixture
async def auditor(client):
    return await login(client, "auditor@flowmentor.com.br")


def meta_payload(wa_id: str = "5562988887777", name: str = "Dra. Teste", text: str | None = None, selection: tuple[str, str] | None = None,
                 status: tuple[str, str] | None = None, wamid: str | None = None, phone_number_id: str = "100000000000002") -> dict:
    import uuid

    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "556299990000", "phone_number_id": phone_number_id}}
    if status:
        value["statuses"] = [{"id": status[0], "status": status[1], "timestamp": "1700000000", "recipient_id": wa_id}]
    else:
        msg = {"from": wa_id, "id": wamid or f"wamid.T{uuid.uuid4().hex[:16]}", "timestamp": "1700000000"}
        if selection:
            msg.update(type="interactive", interactive={"type": "list_reply", "list_reply": {"id": selection[0], "title": selection[1]}})
        else:
            msg.update(type="text", text={"body": text or "Olá"})
        value["contacts"] = [{"profile": {"name": name}, "wa_id": wa_id}]
        value["messages"] = [msg]
    return {"object": "whatsapp_business_account", "entry": [{"id": "100000000000001", "changes": [{"field": "messages", "value": value}]}]}
