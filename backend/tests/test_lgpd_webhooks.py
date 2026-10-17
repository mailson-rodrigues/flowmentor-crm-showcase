import sqlalchemy as sa

from app.db import session_scope
from app.models import AuditLog, Contact, Message
from app.modules.webhooks import router as wh
from tests.conftest import meta_payload


async def _conversation(client, env, admin, consultor, text="Meu CPF é 123.456.789-09 e e-mail joao@x.com"):
    await client.put("/api/v1/org/settings", headers=admin, json={"settings": {"bot": {"enabled": False}}})
    await client.put("/api/v1/auth/me/status", headers=consultor, json={"status": "available"})
    await env.drain()
    await client.post("/webhooks/whatsapp", json=meta_payload(text=text, wa_id="5562977776666", name="João Titular"))
    await env.drain()
    return (await client.get("/api/v1/tickets", headers=consultor, params={"view": "mine"})).json()["items"][0]


async def test_dsr_access_anonymization_and_audit_chain(client, env, admin, consultor, auditor):
    t = await _conversation(client, env, admin, consultor)
    cid = t["contact"]["id"]

    # acesso (art. 18, II) devolve exportação completa
    req = (await client.post("/api/v1/lgpd/requests", headers=auditor, json={"contact_id": cid, "request_type": "access"})).json()
    assert req["status"] == "open" and req["due_at"]
    done = (await client.post(f"/api/v1/lgpd/requests/{req['id']}/process", headers=auditor)).json()
    assert done["status"] == "completed" and done["result"]["summary"]["messages"] >= 1
    assert "123.456.789-09" in str(done["result"]["export"])

    # anonimização: apaga PII, mantém estatísticas
    req2 = (await client.post("/api/v1/lgpd/requests", headers=auditor, json={"contact_id": cid, "request_type": "anonymization"})).json()
    res = (await client.post(f"/api/v1/lgpd/requests/{req2['id']}/process", headers=auditor)).json()
    assert res["status"] == "completed" and res["result"]["messages"] >= 1
    async with session_scope() as s:
        c = await s.get(Contact, __import__("uuid").UUID(cid))
        assert c.anonymized_at and c.wa_id.startswith("anon-") and c.name == "Titular anonimizado"
        bodies = (await s.execute(sa.select(Message.body))).scalars().all()
        assert all("123.456.789-09" not in (b or "") for b in bodies)
    # não é possível enviar mensagens a titular anonimizado
    t_after = await client.post(f"/api/v1/tickets/{t['id']}/messages", headers=consultor, json={"body": "oi"})
    assert t_after.status_code == 409

    # trilha de auditoria imutável com cadeia de hashes
    ok = (await client.get("/api/v1/lgpd/audit-logs/verify", headers=auditor)).json()
    assert ok["valid"] is True and ok["checked"] >= 3
    async with session_scope() as s:  # adulteração manual é detectada
        row = (await s.execute(sa.select(AuditLog).order_by(AuditLog.seq).limit(1))).scalar_one()
        row.action = "ADULTERADO"
    bad = (await client.get("/api/v1/lgpd/audit-logs/verify", headers=auditor)).json()
    assert bad["valid"] is False and bad["broken_at_seq"] == 1


async def test_deletion_and_consent_revocation(client, env, admin, consultor, auditor):
    t = await _conversation(client, env, admin, consultor, text="Quero ser esquecido")
    cid = t["contact"]["id"]
    consents = (await client.get("/api/v1/lgpd/consents", headers=auditor, params={"contact_id": cid})).json()["items"]
    assert consents and consents[0]["consent_source"] == "whatsapp_optin" and consents[0]["revoked_at"] is None
    r = (await client.post("/api/v1/lgpd/requests", headers=auditor, json={"contact_id": cid, "request_type": "revoke_consent"})).json()
    assert (await client.post(f"/api/v1/lgpd/requests/{r['id']}/process", headers=auditor)).json()["result"]["opted_out"] is True
    d = (await client.post("/api/v1/lgpd/requests", headers=auditor, json={"contact_id": cid, "request_type": "deletion"})).json()
    out = (await client.post(f"/api/v1/lgpd/requests/{d['id']}/process", headers=auditor)).json()
    assert out["result"]["tickets"] == 1
    assert (await client.get(f"/api/v1/tickets/{t['id']}", headers=consultor)).status_code == 404
    summary = (await client.get("/api/v1/lgpd/summary", headers=auditor)).json()
    assert summary["anonymized_contacts"] == 1 and summary["open_requests"] == 0


async def test_outbound_webhooks_signature_retry_and_ssrf(client, env, admin, monkeypatch):
    calls = []

    async def fake_deliver(endpoint, delivery):
        body = __import__("json").dumps(delivery.payload, separators=(",", ":"), default=str)
        calls.append((delivery.event_type, wh.sign(endpoint.secret, 1, body), delivery.payload))
        delivery.attempts += 1
        delivery.status = "success"
        return True

    monkeypatch.setattr(wh, "deliver", fake_deliver)
    r = await client.post("/api/v1/webhooks/endpoints", headers=admin, json={"name": "ERP", "url": "https://erp.exemplo.com/hook", "events": ["deal.*", "sla.breached"]})
    assert r.status_code == 201 and r.json()["secret"].startswith("whsec_")
    assert (await client.post("/api/v1/webhooks/endpoints", headers=admin, json={"name": "xx", "url": "https://a.com", "events": ["evento.inexistente"]})).status_code == 400

    prods = {p["sku"]: p for p in (await client.get("/api/v1/products", headers=admin)).json()["items"]}
    acc = (await client.get("/api/v1/accounts", headers=admin)).json()["items"][0]
    deal = (await client.post("/api/v1/deals", headers=admin, json={"account_id": acc["id"], "title": "Evento", "items": [{"product_service_id": prods["CONS-HORA"]["id"], "quantity": 1}]})).json()
    await client.put(f"/api/v1/deals/{deal['id']}/stage", headers=admin, json={"stage": "fechado_ganho"})
    await env.drain()
    types = [c[0] for c in calls]
    assert "deal.created" in types and "deal.won" in types and "deal.stage_changed" in types
    assert "case.created" not in types  # fora das assinaturas do endpoint
    won = next(c for c in calls if c[0] == "deal.won")
    assert won[2]["data"]["deal_id"] == deal["id"] and won[2]["type"] == "deal.won"

    # assinatura HMAC verificável pelo receptor
    assert wh.sign("segredo", 123, "{}") == wh.sign("segredo", 123, "{}") != wh.sign("outro", 123, "{}")
    # SSRF: em produção bloqueia hosts privados/loopback
    from app.config import settings
    monkeypatch.setattr(settings, "environment", "production")
    import pytest
    from app.core.errors import AppError

    with pytest.raises(AppError):
        wh.validate_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(AppError):
        wh.validate_url("https://localhost/hook")


async def test_open_api_key_lead_capture(client, admin):
    k = (await client.post("/api/v1/api-keys", headers=admin, json={"name": "Site", "scopes": ["contacts:write", "deals:write"]})).json()
    h = {"X-API-Key": k["key"]}
    no_consent = await client.post("/api/v1/public/leads", headers=h, json={"name": "Maria Silva", "phone": "62 99999-0001", "consent": False})
    assert no_consent.status_code == 400
    ok = await client.post("/api/v1/public/leads", headers=h, json={"name": "Maria Silva", "phone": "62 99999-0001", "email": "maria@lab.com", "company": "Lab Maria",
                                                                      "interest": "Consultoria", "message": "<script>x</script>Gostaria de uma proposta", "consent": True})
    assert ok.status_code == 201
    deal = (await client.get(f"/api/v1/deals/{ok.json()['deal_id']}", headers=admin)).json()
    assert deal["stage"] == "lead_inbound" and deal["source"] == "site" and "<script" not in deal["notes"]
    # chave sem o escopo não acessa outras rotas
    assert (await client.get("/api/v1/users", headers=h)).status_code == 403
    bad = await client.post("/api/v1/public/leads", headers={"X-API-Key": "fm_invalida"}, json={"name": "A B", "phone": "62999990002", "consent": True})
    assert bad.status_code == 401
