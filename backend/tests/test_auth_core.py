import sqlalchemy as sa

from tests.conftest import PASSWORD


async def test_login_refresh_rotation_and_reuse(client):
    r = await client.post("/api/v1/auth/login", json={"email": "atendente1@flowmentor.com.br", "password": PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["role"] == "agent" and "pii:view" not in body["permissions"]
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r2.status_code == 200
    # reutilizar o refresh antigo = suspeita de roubo → revoga a família inteira
    r3 = await client.post("/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r3.status_code == 401
    r4 = await client.post("/api/v1/auth/refresh", json={"refresh_token": r2.json()["refresh_token"]})
    assert r4.status_code == 401


async def test_lockout_after_failed_attempts(client):
    for _ in range(5):
        r = await client.post("/api/v1/auth/login", json={"email": "atendente2@flowmentor.com.br", "password": "errada"})
        assert r.status_code == 401
    r = await client.post("/api/v1/auth/login", json={"email": "atendente2@flowmentor.com.br", "password": PASSWORD})
    assert r.status_code == 423


async def test_requires_auth_and_rbac(client, agent1, admin):
    assert (await client.get("/api/v1/deals")).status_code == 401
    assert (await client.get("/api/v1/users", headers=agent1)).status_code == 200
    assert (await client.post("/api/v1/users", headers=agent1, json={"name": "X Y", "email": "x@y.com"})).status_code == 403
    assert (await client.get("/api/v1/lgpd/audit-logs", headers=agent1)).status_code == 403
    r = await client.post("/api/v1/users", headers=admin, json={"name": "Novo Agente", "email": "novo@flowmentor.com.br", "password": "fraca"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "weak_password"


async def test_security_headers(client):
    r = await client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_account_tax_id_encrypted_and_masked(client, admin, agent1):
    r = await client.post("/api/v1/accounts", headers=admin, json={"corporate_name": "Lab Teste Ltda", "tax_id": "98.765.432/0001-10", "account_type": "laboratorio"})
    assert r.status_code == 201 and r.json()["tax_id"] == "98765432000110"
    aid = r.json()["id"]
    assert (await client.get(f"/api/v1/accounts/{aid}", headers=agent1)).json()["tax_id"] == "**.***.432/****-10"
    dup = await client.post("/api/v1/accounts", headers=admin, json={"corporate_name": "Outro", "tax_id": "98765432000110"})
    assert dup.status_code == 409
    from app.db import SessionLocal

    async with SessionLocal() as s:
        raw = (await s.execute(sa.text("select tax_id from accounts where id = :i"), {"i": aid.replace("-", "")})).scalar_one()
    assert raw.startswith("enc:v1:") and "98765432000110" not in raw
    found = await client.get("/api/v1/accounts", headers=admin, params={"q": "98765432000110"})
    assert found.json()["total"] == 1


async def test_contact_masking_and_audited_reveal(client, agent1, auditor):
    r = await client.post("/api/v1/contacts", headers=agent1, json={"wa_id": "(62) 99999-1234", "name": "Fulano"})
    assert r.status_code == 201
    c = r.json()
    assert c["masked"] and "****" in c["wa_id"]
    rev = await client.post(f"/api/v1/contacts/{c['id']}/reveal", headers=agent1)
    assert rev.json()["wa_id"] == "5562999991234"
    logs = (await client.get("/api/v1/lgpd/audit-logs", headers=auditor, params={"action": "VIEW_PHONE"})).json()
    assert logs["total"] == 1
