"""Smoke ponta a ponta contra uma stack em execução (API + worker + PostgreSQL + Redis Streams, Meta em sandbox).

    DEMO_USER_PASSWORD=<senha> META_APP_SECRET=<segredo> python scripts/live_smoke.py [http://localhost:8000]

Exige os dados de demonstração (SEED_DEMO_DATA=true). O processamento é assíncrono (worker), então o script faz polling.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
PW = os.environ.get("DEMO_USER_PASSWORD") or sys.exit("Defina DEMO_USER_PASSWORD (a mesma senha dos usuários de demonstração da stack)")
http = httpx.Client(base_url=BASE, timeout=20)
WA = "5562" + str(uuid.uuid4().int)[:9]


def login(email: str) -> dict:
    r = http.post("/api/v1/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, (email, r.text)
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def inbound(**kw) -> None:
    msg = {"from": WA, "id": f"wamid.S{uuid.uuid4().hex[:16]}", "timestamp": str(int(time.time()))}
    if "selection" in kw:
        msg.update(type="interactive", interactive={"type": "list_reply", "list_reply": {"id": kw["selection"][0], "title": kw["selection"][1]}})
    else:
        msg.update(type="text", text={"body": kw.get("text", "Olá")})
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "556299990000", "phone_number_id": "100000000000002"},
             "contacts": [{"profile": {"name": "Dra. Smoke"}, "wa_id": WA}], "messages": [msg]}
    body = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "100000000000001", "changes": [{"field": "messages", "value": value}]}]}).encode()
    headers = {"Content-Type": "application/json"}
    secret = os.environ.get("META_APP_SECRET")  # a API em produção exige X-Hub-Signature-256
    if secret:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = http.post("/webhooks/whatsapp", content=body, headers=headers)
    assert r.status_code == 200, r.text


def wait(fn, what: str, timeout: float = 20.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.4)
    raise AssertionError(f"timeout: {what}")


def step(msg: str) -> None:
    print(f"  ok  {msg}", flush=True)


def tickets(h, view):
    return http.get("/api/v1/tickets", headers=h, params={"view": view}).json()["items"]


def main() -> None:
    assert http.get("/api/health").json()["status"] == "ok"
    sup, cons, ag1 = login("supervisor@flowmentor.com.br"), login("consultor@flowmentor.com.br"), login("atendente1@flowmentor.com.br")
    step("health + logins")
    assert http.put("/api/v1/auth/me/status", headers=cons, json={"status": "available"}).status_code == 200

    inbound(text="Olá")
    t = wait(lambda: next((x for x in tickets(sup, "bot") if x["contact"]["wa_id"].endswith(WA[-4:])), None), "ticket criado pelo bot (worker via Redis)")
    msgs = wait(lambda: [m for m in http.get(f"/api/v1/tickets/{t['id']}/messages", headers=sup).json()["messages"] if m["direction"] == "outbound"], "menu do bot")
    assert msgs[0]["is_bot"] and msgs[0]["payload"]["kind"] == "list"
    step("webhook → ticket + menu do bot")

    inbound(selection=("menu:apoio_diagnostico", "Laudos e diagnóstico"))
    mine = wait(lambda: [x for x in tickets(cons, "mine") if x["id"] == t["id"]], "roteamento para o consultor (FOR UPDATE SKIP LOCKED)")
    assert mine[0]["status"] == "in_progress"
    step("handoff → fila → roteamento atribui ao consultor")

    r = http.post(f"/api/v1/tickets/{t['id']}/messages", headers=cons, json={"body": "Olá! Vou analisar o caso."})
    assert r.status_code == 201, r.text
    assert http.post(f"/api/v1/tickets/{t['id']}/notes", headers=cons, json={"body": "nota interna"}).status_code == 201
    assert http.post(f"/api/v1/supervisor/tickets/{t['id']}/whisper", headers=sup, json={"body": "priorize"}).status_code == 201
    step("resposta, nota interna e sussurro")

    users = http.get("/api/v1/users", headers=sup).json()["items"]
    a1 = next(u["id"] for u in users if u["email"] == "atendente1@flowmentor.com.br")
    assert http.post(f"/api/v1/tickets/{t['id']}/transfer", headers=cons, json={"agent_id": a1, "reason": "proposta"}).status_code == 200
    wait(lambda: [x for x in tickets(ag1, "mine") if x["id"] == t["id"]], "transferência")
    cl = http.post(f"/api/v1/tickets/{t['id']}/close", headers=ag1)
    assert cl.status_code == 200 and cl.json()["status"] == "closed", cl.text
    step("transferência e encerramento")

    k = http.get("/api/v1/kpis/overview", headers=sup).json()
    assert k["totals"]["closed"] >= 1
    accs = http.get("/api/v1/accounts", headers=sup, params={"q": "Smoke"}).json()
    assert accs["total"] >= 1
    step("KPIs e lead criado automaticamente no CRM")

    p = http.get("/api/v1/lgpd/audit-logs", headers=login("auditor@flowmentor.com.br"))
    assert p.status_code == 200
    v = http.get("/api/v1/lgpd/audit-logs/verify", headers=login("auditor@flowmentor.com.br"))
    print("  info audit chain:", v.status_code, v.text[:120])
    print("SMOKE OK")


if __name__ == "__main__":
    main()
