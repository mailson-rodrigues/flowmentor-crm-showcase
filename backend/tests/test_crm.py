import re

import sqlalchemy as sa


async def _products(client, h):
    return {p["sku"]: p for p in (await client.get("/api/v1/products", headers=h)).json()["items"]}


async def _account(client, h):
    return (await client.get("/api/v1/accounts", headers=h)).json()["items"][0]


async def test_deal_totals_discount_and_permission(client, supervisor, agent1, env):
    prods = await _products(client, supervisor)
    acc = await _account(client, supervisor)
    r = await client.post("/api/v1/deals", headers=agent1, json={"account_id": acc["id"], "title": "Consultoria teste", "items": [
        {"product_service_id": prods["CONS-HORA"]["id"], "quantity": 10}, {"product_service_id": prods["CONS-POP"]["id"], "quantity": 1}]})
    assert r.status_code == 201, r.text
    deal = r.json()
    assert deal["expected_value"] == 3000 + 6500 and deal["final_contracted_value"] == 9500
    # preço unitário só pode ser sobrescrito por quem tem deals:discount_high
    assert all(i["unit_price"] == 300 for i in deal["items"] if i["sku"] == "CONS-HORA")
    # agente pode aplicar até 10% ...
    ok = await client.patch(f"/api/v1/deals/{deal['id']}", headers=agent1, json={"discount_percentage": 10})
    assert ok.status_code == 200 and ok.json()["final_contracted_value"] == 8550
    # ... acima disso exige aprovação de supervisor
    blocked = await client.patch(f"/api/v1/deals/{deal['id']}", headers=agent1, json={"discount_percentage": 15})
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "discount_requires_approval"
    approved = await client.patch(f"/api/v1/deals/{deal['id']}", headers=supervisor, json={"discount_percentage": 15})
    assert approved.status_code == 200 and approved.json()["final_contracted_value"] == 8075
    sug = (await client.get(f"/api/v1/deals/{deal['id']}", headers=supervisor)).json()["discount_suggestion"]
    assert sug["suggested_pct"] >= 0 and sug["subtotal"] == 9500


async def test_won_deal_creates_contract_and_cases(client, supervisor, env):
    prods = await _products(client, supervisor)
    acc = await _account(client, supervisor)
    deal = (await client.post("/api/v1/deals", headers=supervisor, json={"account_id": acc["id"], "title": "Pacote completo", "items": [
        {"product_service_id": prods["CONS-HORA"]["id"], "quantity": 20}, {"product_service_id": prods["DIAG-BANCO"]["id"], "quantity": 1},
        {"product_service_id": prods["CAP-ANUAL"]["id"], "quantity": 1}]})).json()
    # perdido exige motivo
    bad = await client.put(f"/api/v1/deals/{deal['id']}/stage", headers=supervisor, json={"stage": "fechado_perdido"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "loss_reason_required"
    won = await client.put(f"/api/v1/deals/{deal['id']}/stage", headers=supervisor, json={"stage": "fechado_ganho"})
    assert won.status_code == 200 and won.json()["probability"] == 100
    await env.drain()
    contracts = (await client.get("/api/v1/contracts", headers=supervisor)).json()["items"]
    ct = next(c for c in contracts if c["deal_id"] == deal["id"])
    assert ct["hours_contracted"] == 20 and ct["modality"] == "recurring_subscription" and ct["recurring_amount"]
    cases = (await client.get("/api/v1/cases", headers=supervisor, params={"account_id": acc["id"]})).json()["items"]
    types = {c["case_type"] for c in cases if c["deal_id"] == deal["id"]}
    assert types == {"consultoria", "apoio_diagnostico", "capacitacao"}
    # horas: lançar consumo e checar limiares
    case = next(c for c in cases if c["case_type"] == "consultoria" and c["deal_id"] == deal["id"])
    assert case["hours_contracted"] == 20
    r = await client.post(f"/api/v1/cases/{case['id']}/time-entries", headers=supervisor, json={"hours": 17, "description": "Validação de painel"})
    assert r.status_code == 201
    got = (await client.get(f"/api/v1/cases/{case['id']}", headers=supervisor)).json()
    assert got["hours_consumed"] == 17 and got["hours_pct"] == 85.0
    # faturamento por consumo respeita banco de horas contratado (nada a faturar dentro da franquia)
    inv = await client.post(f"/api/v1/contracts/{ct['id']}/invoice-from-usage", headers=supervisor)
    assert inv.status_code == 201 and "nada a faturar" in inv.json().get("message", "")
    # excedente é faturado a R$ 300/h
    await client.post(f"/api/v1/cases/{case['id']}/time-entries", headers=supervisor, json={"hours": 5})
    inv = await client.post(f"/api/v1/contracts/{ct['id']}/invoice-from-usage", headers=supervisor)
    assert inv.status_code == 201 and inv.json()["amount"] == 300 * 2


async def test_proposal_generation_and_public_link(client, supervisor):
    prods = await _products(client, supervisor)
    acc = await _account(client, supervisor)
    deal = (await client.post("/api/v1/deals", headers=supervisor, json={"account_id": acc["id"], "title": "Proposta X", "items": [
        {"product_service_id": prods["DIAG-EXAME"]["id"], "quantity": 100}]})).json()
    p = await client.post(f"/api/v1/deals/{deal['id']}/proposals", headers=supervisor, json={"valid_days": 10})
    assert p.status_code == 201 and re.match(r"PROP-\d{4}-0001", p.json()["number"])
    sent = await client.post(f"/api/v1/proposals/{p.json()['id']}/send", headers=supervisor)
    assert sent.json()["status"] == "sent"
    assert (await client.get(f"/api/v1/deals/{deal['id']}", headers=supervisor)).json()["stage"] == "proposta_apresentada"
    link = (await client.post(f"/api/v1/proposals/{p.json()['id']}/share-link", headers=supervisor)).json()["url"]
    html = await client.get(link)
    assert html.status_code == 200 and "R$ 18.500,00" in html.text and "LGPD" in html.text
    assert (await client.get(link.replace("sig=", "sig=00"))).status_code == 403


async def test_kanban_board_and_abac(client, supervisor, agent1, agent2):
    board = (await client.get("/api/v1/deals/board", headers=supervisor)).json()["columns"]
    assert [c["stage"] for c in board][:2] == ["lead_inbound", "prospeccao"]
    sup_total = sum(c["count"] for c in board)
    assert sup_total == 4
    mine = sum(c["count"] for c in (await client.get("/api/v1/deals/board", headers=agent1)).json()["columns"])
    other = sum(c["count"] for c in (await client.get("/api/v1/deals/board", headers=agent2)).json()["columns"])
    assert mine == 2 and other == 2  # cada atendente só enxerga as próprias oportunidades


async def test_pix_invoice_and_billing_cycle(client, admin, supervisor):
    await client.put("/api/v1/org/settings", headers=admin, json={"settings": {"billing": {"pix_key": "financeiro@flowmentor.com.br"}}})
    acc = await _account(client, supervisor)
    ct = (await client.post("/api/v1/contracts", headers=supervisor, json={"account_id": acc["id"], "title": "Assinatura mensal", "modality": "recurring_subscription",
                                                                           "start_date": "2026-01-01", "recurring_amount": 4200, "billing_day": 5})).json()
    assert ct["next_billing_date"] == "2026-01-05"
    res = (await client.post("/api/v1/billing/run-cycle", headers=supervisor)).json()
    assert res["generated"] == 1
    invs = (await client.get("/api/v1/invoices", headers=supervisor)).json()
    inv = invs["items"][0]
    assert inv["pix_payload"] and "br.gov.bcb.pix" in inv["pix_payload"]
    assert inv["status"] == "overdue" and invs["totals"]["overdue"] == 4200  # vencimento em jan/2026 já passou
    after = (await client.get(f"/api/v1/contracts/{ct['id']}", headers=supervisor)).json()
    assert after["next_billing_date"] == "2026-02-05"
    paid = await client.post(f"/api/v1/invoices/{inv['id']}/pay", headers=supervisor, json={"payment_method": "pix"})
    assert paid.json()["status"] == "paid"
