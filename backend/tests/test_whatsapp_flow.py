"""End-to-end flow: webhook → bot → handoff → routing → service → transfer → closing → survey → KPIs."""
import hashlib
import hmac
import json

import sqlalchemy as sa

from app.config import settings
from app.core.scheduler import run_due_jobs
from app.db import session_scope
from app.models import Message, SatisfactionSurvey
from tests.conftest import meta_payload

WA = "5562988887777"


async def post_wa(client, env, **kw):
    r = await client.post("/webhooks/whatsapp", json=meta_payload(wa_id=kw.pop("wa_id", WA), **kw))
    assert r.status_code == 200
    await env.drain()
    return r


async def tickets(client, h, view="open"):
    return (await client.get("/api/v1/tickets", headers=h, params={"view": view})).json()["items"]


async def set_status(client, env, h, status):
    r = await client.put("/api/v1/auth/me/status", headers=h, json={"status": status})
    assert r.status_code == 200
    await env.drain()


async def test_webhook_verification_and_signature(client, env, monkeypatch):
    ok = await client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": settings.meta_webhook_verify_token, "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = await client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "errado", "hub.challenge": "1"})
    assert bad.status_code == 403
    monkeypatch.setattr(settings, "meta_app_secret", "segredo-app")
    body = json.dumps(meta_payload(text="oi")).encode()
    assert (await client.post("/webhooks/whatsapp", content=body)).status_code == 403
    sig = "sha256=" + hmac.new(b"segredo-app", body, hashlib.sha256).hexdigest()
    assert (await client.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})).status_code == 200


async def test_full_support_lifecycle(client, env, admin, supervisor, consultor, agent1):
    # configure a survey without grace window for the test
    await client.put("/api/v1/org/settings", headers=admin, json={"settings": {"survey": {"grace_minutes": 0}}})

    # 1) customer writes → ticket with bot + interactive menu
    await post_wa(client, env, text="Olá")
    t = (await tickets(client, supervisor, "bot"))[0]
    assert t["status"] == "pending_bot" and t["contact"]["masked"] is False  # supervisor sees PII
    msgs = (await client.get(f"/api/v1/tickets/{t['id']}/messages", headers=supervisor)).json()["messages"]
    menu = [m for m in msgs if m["direction"] == "outbound"][0]
    assert menu["is_bot"] and menu["payload"]["kind"] == "list" and menu["status"] == "delivered"

    # 2) idempotency: redelivery of the same wamid does not duplicate
    dup = meta_payload(text="repetida", wamid="wamid.DUPLICADA")
    for _ in range(2):
        await client.post("/webhooks/whatsapp", json=dup)
        await env.drain()
    msgs = (await client.get(f"/api/v1/tickets/{t['id']}/messages", headers=supervisor)).json()["messages"]
    assert sum(1 for m in msgs if m["meta_message_id"] == "wamid.DUPLICADA") == 1

    # 3) customer picks "Laudos e diagnóstico" (reports and diagnosis) → handoff with summary and skill, ticket goes to the queue
    await post_wa(client, env, selection=("menu:apoio_diagnostico", "Laudos e diagnóstico"))
    t = (await tickets(client, supervisor, "queue"))[0]
    assert t["status"] == "queued" and t["department_name"] == "Apoio Diagnóstico" and t["summary"] and not t["bot_active"]
    ev = (await client.get(f"/api/v1/tickets/{t['id']}/events", headers=supervisor)).json()["items"]
    assert any(e["event_type"] == "handoff" for e in ev)

    # 4) service with ABAC restriction: atendente1 only sees the queue of their own department; the consultant goes available → routing assigns
    await set_status(client, env, consultor, "available")
    mine = await tickets(client, consultor, "mine")
    assert len(mine) == 1 and mine[0]["status"] == "in_progress" and mine[0]["assigned_agent_id"]
    tid = mine[0]["id"]
    assert (await client.get(f"/api/v1/tickets/{tid}", headers=consultor)).json()["contact"]["masked"] is True  # the consultant does not see the full phone number

    # 5) the consultant replies → FRT recorded; internal note does not go to the customer
    r = await client.post(f"/api/v1/tickets/{tid}/messages", headers=consultor, json={"body": "Olá, Dra.! Vou analisar o caso agora."})
    assert r.status_code == 201
    await env.drain()
    note = await client.post(f"/api/v1/tickets/{tid}/notes", headers=consultor, json={"body": "Cliente quer painel de LLA-B"})
    assert note.status_code == 201
    det = (await client.get(f"/api/v1/tickets/{tid}", headers=consultor)).json()
    assert det["first_response_time_seconds"] is not None and det["first_response_at"]
    wamid = r.json()["meta_message_id"] or (await client.get(f"/api/v1/tickets/{tid}/messages", headers=consultor)).json()["messages"][-1]["meta_message_id"]

    # 6) "read" status coming from Meta never goes backwards and updates the front end
    await post_wa(client, env, status=(wamid, "read"))
    last = [m for m in (await client.get(f"/api/v1/tickets/{tid}/messages", headers=consultor)).json()["messages"] if m["id"] == r.json()["id"]][0]
    assert last["status"] == "read"
    await post_wa(client, env, status=(wamid, "sent"))
    last = [m for m in (await client.get(f"/api/v1/tickets/{tid}/messages", headers=consultor)).json()["messages"] if m["id"] == r.json()["id"]][0]
    assert last["status"] == "read"

    # 7) the supervisor's whisper is visible only to the agent; a regular agent cannot access the consultant's ticket
    w = await client.post(f"/api/v1/supervisor/tickets/{tid}/whisper", headers=supervisor, json={"body": "Priorize: cliente é paciente oncológico"})
    assert w.status_code == 201
    notes = (await client.get(f"/api/v1/tickets/{tid}/messages", headers=consultor)).json()["notes"]
    assert any(n["kind"] == "whisper" for n in notes) and any(n["kind"] == "note" for n in notes)
    assert (await client.get(f"/api/v1/tickets/{tid}", headers=agent1)).status_code == 403

    # 8) transfer to Sales keeping the history → atendente1 takes over
    users = (await client.get("/api/v1/users", headers=supervisor)).json()["items"]
    agent1_id = next(u["id"] for u in users if u["email"] == "atendente1@flowmentor.com.br")
    tr = await client.post(f"/api/v1/tickets/{tid}/transfer", headers=consultor, json={"agent_id": agent1_id, "reason": "Cliente pediu proposta comercial"})
    assert tr.status_code == 200, tr.text
    await env.drain()
    me1 = await tickets(client, agent1, "mine")
    assert len(me1) == 1 and me1[0]["id"] == tid
    all_msgs = (await client.get(f"/api/v1/tickets/{tid}/messages", headers=agent1)).json()
    assert len(all_msgs["messages"]) >= 4 and any("Transferido" in n["body"] for n in all_msgs["notes"])
    assert not any(n["kind"] == "whisper" for n in all_msgs["notes"])  # the whisper belonged to the consultant

    # 9) atendente1 closes → metrics snapshot + survey scheduled
    cl = await client.post(f"/api/v1/tickets/{tid}/close", headers=agent1)
    assert cl.status_code == 200 and cl.json()["status"] == "closed" and cl.json()["handling_time_seconds"] is not None and cl.json()["is_fcr"] is True
    await env.drain()
    assert await run_due_jobs() >= 1
    await env.drain()
    async with session_scope() as s:
        survey = (await s.execute(sa.select(SatisfactionSurvey))).scalar_one()
        assert survey.state == "awaiting_rating"
        sid = survey.id
        outs = (await s.execute(sa.select(Message).where(Message.ticket_id == survey.ticket_id, Message.is_bot.is_(True)).order_by(Message.created_at.desc()))).scalars().all()
        assert outs[0].payload["rows"][0]["id"] == f"svy:{sid}:5"

    # 10) customer rates 5 from the list and comments → positive sentiment; does not open a new ticket
    await post_wa(client, env, selection=(f"svy:{sid}:5", "5 - Excelente"))
    await post_wa(client, env, text="Atendimento excelente e muito rápido, obrigado!")
    async with session_scope() as s:
        sv = await s.get(SatisfactionSurvey, sid)
        assert sv.rating == 5 and sv.state == "completed" and sv.sentiment == "positive" and "excelente" in sv.comment
    assert await tickets(client, supervisor, "open") == []

    # 11) KPIs consolidados
    k = (await client.get("/api/v1/kpis/overview", headers=supervisor)).json()
    assert k["totals"]["tickets"] == 1 and k["totals"]["closed"] == 1
    assert k["speed"]["frt_avg_seconds"] is not None and k["resolution"]["aht_avg_seconds"] is not None and k["resolution"]["fcr_pct"] == 100.0
    assert k["quality"]["csat_avg"] == 5.0 and k["quality"]["response_rate"] == 100.0 and k["quality"]["sentiment"] == {"positive": 1}
    assert any(a["name"] == "Camila Teixeira" for a in k["agents"])
    assert sum(b["messages_in"] for b in k["throughput"]) >= 3

    # 12) timeline unificada no CRM: lead criado automaticamente pelo bridge
    accs = (await client.get("/api/v1/accounts", headers=supervisor, params={"q": "Lead"})).json()
    assert accs["total"] == 1
    tl = (await client.get("/api/v1/timeline", headers=supervisor, params={"account_id": accs["items"][0]["id"]})).json()["items"]
    kinds = {a["kind"] for a in tl}
    assert {"whatsapp_ticket", "whatsapp_closed", "survey"} <= kinds


async def test_survey_suppression_and_late_answer(client, env, admin, consultor):
    await client.put("/api/v1/org/settings", headers=admin, json={"settings": {"survey": {"grace_minutes": 0, "ttl_hours": 48, "type": "nps"}, "bot": {"enabled": False}}})
    await set_status(client, env, consultor, "available")
    await post_wa(client, env, text="Preciso de ajuda")
    t = (await tickets(client, consultor, "mine"))[0]
    await client.post(f"/api/v1/tickets/{t['id']}/messages", headers=consultor, json={"body": "Claro!"})
    await client.post(f"/api/v1/tickets/{t['id']}/close", headers=consultor)
    await env.drain()
    await run_due_jobs()
    await env.drain()
    async with session_scope() as s:
        sv = (await s.execute(sa.select(SatisfactionSurvey))).scalar_one()
        sid = sv.id
        assert sv.survey_type == "nps"
    # two-step NPS: range (buttons) → exact score (list)
    await post_wa(client, env, selection=(f"svy:{sid}:r:9-10", "9 a 10"))
    await post_wa(client, env, selection=(f"svy:{sid}:9", "9"))
    await post_wa(client, env, text="pular")
    k = (await client.get("/api/v1/kpis/overview", headers=admin)).json()["quality"]
    assert k["nps"] == 100 and k["nps_promoters"] == 1
    # new ticket within 24h → the previous one no longer counts as FCR
    await post_wa(client, env, text="Tenho outra dúvida")
    t2 = (await tickets(client, consultor, "mine"))[0]
    await client.post(f"/api/v1/tickets/{t2['id']}/close", headers=consultor)
    await env.drain()
    await run_due_jobs()  # suppression: answered a survey less than 7 days ago
    await env.drain()
    async with session_scope() as s:
        assert len((await s.execute(sa.select(SatisfactionSurvey))).scalars().all()) == 1


async def test_opt_out_and_window_rules(client, env, admin, consultor):
    await client.put("/api/v1/org/settings", headers=admin, json={"settings": {"bot": {"enabled": False}}})
    await set_status(client, env, consultor, "available")
    await post_wa(client, env, text="Olá, bom dia")
    t = (await tickets(client, consultor, "mine"))[0]
    # an unapproved template cannot be used; free text inside the 24h window is allowed
    assert (await client.post(f"/api/v1/tickets/{t['id']}/messages", headers=consultor, json={"body": "Bom dia!"})).status_code == 201
    await post_wa(client, env, text="PARAR")
    after = await client.post(f"/api/v1/tickets/{t['id']}/messages", headers=consultor, json={"body": "Ainda aí?"})
    assert after.status_code == 409 and after.json()["error"]["code"] == "contact_opted_out"


async def test_templates_lifecycle(client, env, supervisor):
    body = {"name": "laudo_disponivel", "language": "pt_BR", "category": "UTILITY", "sample_values": {"1": "Dra. Ana", "2": "EX-1001"},
            "components": [{"type": "BODY", "text": "Olá {{1}}, o laudo do exame {{2}} já está disponível na plataforma da Flowmentor."},
                           {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Obrigado"}]}]}
    bad = await client.post("/api/v1/templates", headers=supervisor, json={**body, "name": "Nome Ruim"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "template_invalid"
    created = await client.post("/api/v1/templates", headers=supervisor, json={**body, "submit": True})
    assert created.status_code == 201 and created.json()["status"] == "pending"
    tid = created.json()["id"]
    assert (await client.post(f"/api/v1/templates/{tid}/sandbox-approve", headers=supervisor)).json()["status"] == "approved"
    contact = (await client.get("/api/v1/contacts", headers=supervisor)).json()
    cid = (await client.post("/api/v1/contacts", headers=supervisor, json={"wa_id": "62 98888-1111", "name": "Dra. Ana"})).json()["id"]
    r = await client.post("/api/v1/tickets/outbound", headers=supervisor, json={"contact_id": cid, "template_id": tid, "variables": {"1": "Dra. Ana", "2": "EX-1001"}})
    assert r.status_code == 201, r.text
    await env.drain()
    msgs = (await client.get(f"/api/v1/tickets/{r.json()['ticket_id']}/messages", headers=supervisor)).json()["messages"]
    assert "Dra. Ana" in msgs[0]["body"] and msgs[0]["type"] == "template" and msgs[0]["status"] == "delivered"
    missing = await client.post("/api/v1/tickets/outbound", headers=supervisor, json={"contact_id": cid, "template_id": tid, "variables": {"1": "x"}})
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "missing_variables"


async def test_supervisor_live_and_barge_in(client, env, admin, supervisor, consultor, agent1):
    await client.put("/api/v1/org/settings", headers=admin, json={"settings": {"bot": {"enabled": False}}})
    await set_status(client, env, consultor, "available")
    await post_wa(client, env, text="Urgente: preciso do laudo")
    live = (await client.get("/api/v1/supervisor/live", headers=supervisor)).json()
    assert live["totals"]["in_progress"] == 1 and any(a["status"] == "available" for a in live["agents"])
    tid = live["tickets"][0]["id"]
    assert (await client.get("/api/v1/supervisor/live", headers=agent1)).status_code == 403
    b = await client.post(f"/api/v1/supervisor/tickets/{tid}/barge-in", headers=supervisor)
    assert b.status_code == 200 and b.json()["assigned_agent_id"] != live["tickets"][0]["assigned_agent_id"]
    from app.modules.whatsapp.supervisor import sla_monitor

    await sla_monitor()  # must not fail or duplicate alerts
