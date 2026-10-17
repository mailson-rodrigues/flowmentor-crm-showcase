# Arquitetura — Flowmentor CRM

## 1. Visão geral

Modular monolith com **portas e adaptadores** e **barramento de eventos assíncrono**. Cada módulo expõe um `router` (HTTP) e
consome/produz eventos; integrações externas (Meta, S3, LLM, PIX, NFS-e, webhooks) ficam atrás de interfaces substituíveis.
A divisão em módulos permite extrair serviços no futuro sem reescrever regras de negócio.

```
HTTP (FastAPI) ─► service/regra ─► SQLAlchemy ─► commit ─► after_commit ─► EventBus.publish
                                                                     │
                     ┌───────────────────────────────────────────────┴────────────┐
                     ▼              ▼               ▼              ▼              ▼
              whatsapp.processor  bridge(CRM)   routing/SLA   webhooks.out   realtime(Socket.IO)
```

**Outbox simplificado:** `emit(session, evento, …)` só publica **depois do commit** (`db.after_commit`), evitando que consumidores
leiam estado inexistente. Em produção troque por *transactional outbox* (tabela + relay) se exigir entrega garantida pós-crash.

## 2. Fluxo do webhook da Meta (caminho crítico < 200 ms)

1. `POST /webhooks/whatsapp` lê o corpo cru, valida `X-Hub-Signature-256` (HMAC-SHA256 com `META_APP_SECRET`) e **apenas publica** `whatsapp.webhook.received`.
2. Responde `200 OK` imediatamente — sem banco, sem chamadas externas.
3. O consumidor (`whatsapp/processor.py`) processa: idempotência por `wamid`, contato, opt-in/opt-out (LGPD), resposta de pesquisa,
   ticket (abre/reabre), mensagem, mídia → bucket, bot/handoff, roteamento.
4. Status (`sent/delivered/read/failed`) nunca regridem (ordem de entrega não é garantida pela Meta).

Mensagens de saída: `queue_outbound` grava `queued` e publica `message.outbound_queued`; o consumidor `outbound.py` envia com retry e
atualiza o status. Regras: janela de 24h (texto livre só dentro dela; fora, template HSM aprovado), opt-out e anonimização bloqueiam envio.

## 3. Roteamento e SLA

- Estratégias: **Round-Robin**, **Least Busy** e **Skill-based** (por departamento), mais afinidade (*sticky*) com o último agente.
- Capacidade por agente (`max_concurrent_chats`); filas ordenadas por prioridade e tempo de espera; `FOR UPDATE SKIP LOCKED` no PostgreSQL.
- Gatilhos de despacho: agente fica disponível, ticket encerrado/transferido/enfileirado, e *tick* a cada 15 s.
- Fora do expediente: ticket permanece na fila de contingência e o cliente recebe aviso único.
- SLA: prazos gravados no ticket (por departamento); monitor a cada 30 s emite `sla.at_risk` (≥80%) e `sla.breached` uma única vez.

## 4. Modelo de dados

Segue o DDL fornecido (organizations, users, departments, contacts, tickets, messages, ticket_events, satisfaction_surveys, accounts,
account_contacts, products_services, deals, deal_items, crm_deal_tickets, lgpd_consents, audit_logs) e complementa com:
`whatsapp_accounts`, `wa_templates`, `media_assets`, `ticket_notes`, `agent_presence_log`, `activities` (timeline), `proposals`,
`contracts`, `invoices`, `cases`, `case_time_entries`, `case_reports`, `data_subject_requests`, `webhook_endpoints/deliveries`,
`scheduled_jobs`, `api_keys`, `refresh_tokens`.

- **Snapshots em `tickets`** (`first_response_time_seconds`, `handling_time_seconds`, `wait_time_seconds`, `is_fcr`) evitam varrer mensagens para KPIs.
- **`ticket_events`**: histórico cronológico (fila × tempo com cada agente).
- **`messages`**: particionada por mês (`created_at`) no PostgreSQL; função `ensure_message_partitions` + job diário.
- **`audit_logs`**: append-only (triggers) + cadeia de hashes verificável em `GET /api/v1/lgpd/audit-logs/verify`.
- Estágios de negócio: `lead_inbound, prospeccao, qualificacao_tecnica, reuniao_alinhamento, proposta_apresentada, negociacao, fechado_ganho, fechado_perdido`
  (união do DDL com as etapas citadas no briefing).

## 5. Regras comerciais

- Itens usam o preço de catálogo; **só quem tem `deals:discount_high` altera preço unitário**.
- Desconto institucional sugerido = `max(faixa por valor, desconto da conta)`; acima de `discount.auto_approve_max_pct` exige Supervisor Comercial.
- **Negócio ganho** (`deal.won`) → contrato + casos por eixo (Consultoria, Apoio Diagnóstico, Canal Médico, Capacitação); recorrência mensal gera faturas.
- Banco de horas: faturamento por consumo cobra só o **excedente** da franquia, à hora técnica do contrato (ref. R$ 300).

## 6. Catálogo de eventos (webhooks outbound)

`account.created · contact.created · deal.created/updated/stage_changed/won/lost · proposal.sent · contract.created/status_changed ·
invoice.created/paid · case.created/updated/closed/hours_threshold · report.received/issued · sla.at_risk/breached ·
ticket.created/assigned/transferred/closed · message.received · survey.answered/detractor · lgpd.request_created/anonymized ·
template.status_changed` (lista viva em `GET /api/v1/webhooks/events`).

## 7. Escala e alta disponibilidade

| Preocupação | Solução |
|---|---|
| Ingestão de webhooks | Responder 200 e enfileirar; consumer groups no Redis Streams escalam horizontalmente; DLQ em `…:events:dlq` |
| Tempo real multi-instância | `socketio.AsyncRedisManager` (salas compartilhadas; worker pode emitir) — use WebSocket puro ou *sticky sessions* |
| Banco | Particionamento temporal de `messages`; réplicas de leitura para KPIs; índices por `(ticket_id, created_at)` |
| Analytics pesado | Trocar `compute_overview` por ClickHouse/Elasticsearch alimentados pelo mesmo barramento |
| Jobs | Tabela `scheduled_jobs` + `SKIP LOCKED`; tarefas periódicas idempotentes |
| Mídias | Fora do banco; URLs assinadas com expiração; retenção configurável |

## 8. Segurança — mapa de controles

Ver README. Pontos de atenção operacionais: gerar `KEK_KEYS` em KMS/Secrets Manager, rotacionar com `crypto.rewrap`, restringir o
usuário do banco (sem `UPDATE/DELETE` em `audit_logs`), habilitar backups criptografados e logs centralizados sem PII (`masking.scrub_text`).
