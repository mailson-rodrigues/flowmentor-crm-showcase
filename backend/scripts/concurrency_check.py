"""Concurrency checks against the running stack (PostgreSQL + Redis + worker). Usage:

    DEMO_USER_PASSWORD=<password> META_APP_SECRET=<secret> python scripts/concurrency_check.py [http://localhost:8000]

A) simultaneous redelivery of the same wamid does not duplicate messages (idempotency)
B) concurrent routing (FOR UPDATE SKIP LOCKED): no ticket with two agents, respecting max_concurrent_chats
C) concurrent writes to audit_logs keep the hash chain intact (advisory lock)
"""
from __future__ import annotations

import asyncio
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
SECRET = os.environ.get("META_APP_SECRET")


def payload(wa: str, wamid: str, text: str | None = None, selection: tuple[str, str] | None = None) -> bytes:
    msg = {"from": wa, "id": wamid, "timestamp": str(int(time.time()))}
    if selection:
        msg.update(type="interactive", interactive={"type": "list_reply", "list_reply": {"id": selection[0], "title": selection[1]}})
    else:
        msg.update(type="text", text={"body": text or "Olá"})
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "556299990000", "phone_number_id": "100000000000002"},
             "contacts": [{"profile": {"name": f"Carga {wa[-4:]}"}, "wa_id": wa}], "messages": [msg]}
    return json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "100000000000001", "changes": [{"field": "messages", "value": value}]}]}).encode()


async def post(c: httpx.AsyncClient, body: bytes) -> int:
    h = {"Content-Type": "application/json"}
    if SECRET:
        h["X-Hub-Signature-256"] = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return (await c.post("/webhooks/whatsapp", content=body, headers=h)).status_code


async def login(c: httpx.AsyncClient, email: str) -> dict:
    r = await c.post("/api/v1/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def poll(fn, what: str, timeout: float = 30.0):
    end = time.time() + timeout
    while time.time() < end:
        v = await fn()
        if v:
            return v
        await asyncio.sleep(0.7)
    raise AssertionError(f"timeout: {what}")


async def main() -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        sup = await login(c, "supervisor@flowmentor.com.br")
        a1, a2 = await login(c, "atendente1@flowmentor.com.br"), await login(c, "atendente2@flowmentor.com.br")
        cons = await login(c, "consultor@flowmentor.com.br")
        aud = await login(c, "auditor@flowmentor.com.br")

        # --- A) idempotency under concurrency
        wa = "5562" + str(uuid.uuid4().int)[:9]
        wamid = f"wamid.DUP{uuid.uuid4().hex[:12]}"
        codes = await asyncio.gather(*[post(c, payload(wa, wamid, "mensagem repetida")) for _ in range(20)])
        assert set(codes) == {200}, codes
        await asyncio.sleep(3)
        tks = [t for t in (await c.get("/api/v1/tickets", headers=sup, params={"view": "bot"})).json()["items"] if t["contact"]["wa_id"].endswith(wa[-4:])]
        assert len(tks) == 1, f"tickets duplicados: {len(tks)}"
        ms = (await c.get(f"/api/v1/tickets/{tks[0]['id']}/messages", headers=sup)).json()["messages"]
        assert sum(m["meta_message_id"] == wamid for m in ms) == 1, "mensagem duplicada"
        print("  ok  A) 20 entregas simultâneas do mesmo wamid → 1 ticket, 1 mensagem")

        # --- B) roteamento concorrente
        for h, name in ((a1, "atendente1"), (a2, "atendente2")):
            await c.put("/api/v1/auth/me/status", headers=h, json={"status": "available"})
        await c.put("/api/v1/auth/me/status", headers=cons, json={"status": "offline"})
        n = 12
        was = ["5562" + str(uuid.uuid4().int)[:9] for _ in range(n)]
        await asyncio.gather(*[post(c, payload(w, f"wamid.B{uuid.uuid4().hex[:14]}", "oi")) for w in was])
        await asyncio.sleep(2)
        await asyncio.gather(*[post(c, payload(w, f"wamid.B{uuid.uuid4().hex[:14]}", selection=("menu:comercial", "Comercial"))) for w in was])
        suffixes = {w[-4:] for w in was}

        async def settled():
            items = []
            for v in ("queue", "open"):
                items += (await c.get("/api/v1/tickets", headers=sup, params={"view": v, "limit": 100})).json()["items"]
            mine = {t["id"]: t for t in items if t["contact"]["wa_id"][-4:] in suffixes and t["status"] in ("in_progress", "queued")}
            return mine if len(mine) == n and sum(t["status"] == "in_progress" for t in mine.values()) >= 10 else None

        mine = await poll(settled, "12 tickets roteados")
        per = {}
        for t in mine.values():
            if t["status"] == "in_progress":
                per[t["assigned_agent_id"]] = per.get(t["assigned_agent_id"], 0) + 1
        assert all(v <= 5 for v in per.values()), per
        print(f"  ok  B) {n} atendimentos simultâneos → distribuição {sorted(per.values())}, {sum(t['status']=='queued' for t in mine.values())} na fila (limite 5/agente)")
        for t in mine.values():  # limpa: encerra os tickets criados
            owner = a1 if t["assigned_agent_id"] and t.get("assigned_agent_name", "").startswith("Camila") else a2
            await c.post(f"/api/v1/tickets/{t['id']}/close", headers=owner)

        # --- C) auditoria concorrente
        ct = (await c.post("/api/v1/contacts", headers=a1, json={"wa_id": "(62) 98888-" + str(uuid.uuid4().int)[:4], "name": "Auditoria"})).json()["id"]
        res = await asyncio.gather(*[c.post(f"/api/v1/contacts/{ct}/reveal", headers=a1) for _ in range(30)])
        assert all(r.status_code == 200 for r in res), [r.status_code for r in res if r.status_code != 200]
        v = (await c.get("/api/v1/lgpd/audit-logs/verify", headers=aud)).json()
        assert v["valid"], v
        print(f"  ok  C) 30 revelações simultâneas → cadeia de auditoria íntegra ({v['checked']} registros)")
    print("CONCURRENCY OK")


asyncio.run(main())
