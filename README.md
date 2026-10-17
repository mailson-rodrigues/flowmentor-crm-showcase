# Flowmentor CRM

**100% web** CRM for Flowmentor (flow cytometry, laboratory consulting, diagnostic support and training),
with a **WhatsApp BSP** (Meta Cloud API), B2B sales pipeline, cases/reports, contracts & billing (PIX),
native WhatsApp satisfaction surveys, automatic KPIs, webhooks/Open API and **LGPD** compliance.

> Stack: **Python 3.12 + FastAPI + SQLAlchemy 2 (async)** · PostgreSQL (SQLite in dev) · Redis Streams · Socket.IO ·
> Plain-JavaScript SPA frontend (ES modules, no build step) · PHP only for form integrations.
>
> The product UI and the end-user messages (bot, surveys, forms) are in Brazilian Portuguese (pt-BR) because the users are Brazilian;
> code, comments and documentation are in English.

## Quick start (Windows / VS Code)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000>. The development `backend/.env` already uses **SQLite**, **Meta in sandbox mode** and demo data.

| User | Role |
|---|---|
| `admin@flowmentor.com.br` | Administrator |
| `supervisor@flowmentor.com.br` | Supervisor |
| `consultor@flowmentor.com.br` | Technical Consultant |
| `atendente1@…` / `atendente2@…` | Agents |
| `auditor@flowmentor.com.br` | Auditor / DPO |

> **No password is committed to the repository.** Set `BOOTSTRAP_ADMIN_PASSWORD` (administrator) and `DEMO_USER_PASSWORD` (demo users) in `backend/.env`.
> If left empty, the app generates random passwords on first run and logs them **only once** in the startup log (non-production only).
> Also use your own `SECRET_KEY`. In production the app refuses to start with an insecure configuration.

**Try WhatsApp without Meta credentials:** sign in as `consultor`, set the status to *Available* (“Disponível”), and under
*Settings → WhatsApp → Simulate incoming message* (UI labels: “Configurações → WhatsApp → Simular mensagem recebida”, as admin) inject a message. The full flow runs:
bot → menu → handoff → routing → real-time service → closing → CSAT/NPS survey → KPIs.

VS Code shortcuts: `Ctrl+Shift+B`/`Terminal → Run Task` (setup, server, tests, migrations) and **Run and Debug** (API, worker, pytest).

```powershell
cd backend; .\.venv\Scripts\python.exe -m pytest -q     # 29 automated tests (in-memory SQLite)
# against real PostgreSQL/Redis (the test database schema is recreated!):
#   $env:POSTGRES_PASSWORD="<local-password>"; docker compose -f docker-compose.yml -f docker-compose.test.yml up -d db redis
#   docker compose exec db createdb -U flowmentor flowmentor_test
#   $env:TEST_DATABASE_URL="postgresql+asyncpg://flowmentor:<local-password>@127.0.0.1:5432/flowmentor_test"; $env:TEST_REDIS_URL="redis://127.0.0.1:6379/15"
#   .\.venv\Scripts\python.exe -m pytest -q
```

## Validation (full stack and frontend)

Local staging stack (PostgreSQL 16 + Redis 7 + API + worker, Meta in sandbox mode, demo data):

```powershell
# .env at the repo root (not committed): copy .env.example and fill in SECRET_KEY, KEK_KEYS, POSTGRES_PASSWORD, META_APP_SECRET...
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
cd backend
$env:META_APP_SECRET="<same value as in .env>"; $env:DEMO_USER_PASSWORD="<same password as the demo users>"
.\.venv\Scripts\python.exe scripts\live_smoke.py          # end-to-end flow (webhook → bot → routing → transfer → KPIs → audit)
.\.venv\Scripts\python.exe scripts\concurrency_check.py   # idempotency, SKIP LOCKED routing and audit chain under concurrency
```

**Frontend test page:** open <http://localhost:8000/static/test/index.html>, enter the demo users' password and click
*Run all tests* (“Executar todos os testes”). It exercises the SPA against the real API: assets and security headers, import of every module,
a **per-role RBAC** matrix (routes visible in the UI × server response), rendering of every page without console/HTTP errors, detail pages,
XSS resistance, real time (Socket.IO ← worker ← Redis), basic accessibility and responsive styles. The result can be exported as JSON.
Use only in staging/development (the test creates and deletes an account and opens a simulated conversation).

## Modules

| Module | What it delivers | Code |
|---|---|---|
| **Core / Registries** | Multi-tenant, institutional accounts (hospital, laboratory, clinic…), contacts (CRM/CRBM), RBAC + ABAC, PII masking | `app/modules/core` |
| **Pipeline CRM** | Kanban/table, catalog (4 axes), items, **automatic institutional discount** with approval tiers, contract proposals (printable HTML/PDF), unified timeline | `app/modules/pipeline` |
| **Operations** | Cases, **consumed × contracted hours** (80%/100% alerts), reports (24h/4h SLA), contracts, invoices, PIX copy-and-paste (BR Code), NFS-e (port) | `app/modules/ops` |
| **WhatsApp BSP** | Embedded Signup, webhook < 200 ms with HMAC, queues, HSM templates, media in a bucket with signed URLs, routing, bot + handoff, internal notes, transfer, real time | `app/modules/whatsapp` |
| **Satisfaction surveys** | CSAT / NPS (2 steps) / CES via buttons and lists, comment + sentiment (NLP), anti-spam, expiry | `whatsapp/surveys.py` |
| **KPIs** | FRT, AHT, FCR, throughput, CSAT/NPS/CES, agent occupancy, SLA, live dashboard, **whisper** and **barge-in** | `whatsapp/kpis.py`, `supervisor.py` |
| **Webhooks / Open API** | Signed outbound endpoints (HMAC), exponential retry, event catalog, API keys, lead capture | `app/modules/webhooks`, `openapi` |
| **LGPD & Audit** | Consents, data-subject rights (arts. 18/19), anonymization/erasure, **hash-chained append-only** audit trail | `app/modules/lgpd`, `core/audit.py` |

## Architecture (Ports & Adapters + event bus)

```
Browser (SPA) ──HTTPS/WSS──► API Gateway (FastAPI: JWT, RBAC/ABAC, rate limit, CSP/HSTS)
                                   │ modules: Core · Pipeline · Operations · WhatsApp · LGPD · Webhooks
                                   ▼
                      Event Bus (Redis Streams | memory)  ◄── Meta Webhook (immediate 200 OK)
                      │        │          │           │
           whatsapp.processor  bridge   routing    webhooks.dispatch · realtime(Socket.IO) · surveys · ops
                      ▼
          PostgreSQL (messages partitioned by month) · S3/GCS (media) · KMS/KEK (envelope encryption)
```

Details, event catalog and decisions in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Security & LGPD (implemented)

- **In transit:** HSTS, strict CSP, `X-Frame-Options`, `nosniff`, COOP/CORP; TLS 1.3 at the proxy ([`deploy/Caddyfile`](deploy/Caddyfile)).
- **At rest:** AES-256-GCM envelope encryption (per-value DEK + rotating KEK) for CNPJ and Meta tokens; *blind index* for search.
- **Access:** short-lived JWT + refresh with **rotation and reuse detection**, Argon2id, lockout after failed attempts, rate limiting, RBAC (6 roles) + ABAC (a salesperson only sees their own deals; an agent only sees their department's queue).
- **Privacy:** phone numbers/CNPJ masked per role; *reveal* requires permission and records `VIEW_PHONE`; opt-out via the keyword “PARAR”; consent recorded.
- **Audit:** `audit_logs` chained by SHA-256 + `BEFORE UPDATE/DELETE/TRUNCATE` triggers on PostgreSQL (migration `0001`).
- **Untrusted input:** sanitization of WhatsApp messages, frontend without `innerHTML`, SSRF protection on webhooks, parameterized ORM.

## Production

```powershell
copy .env.example .env      # fill in SECRET_KEY, KEK_KEYS, POSTGRES_PASSWORD, META_*
docker compose up -d --build                          # api + worker + postgres + redis
docker compose --profile proxy --profile s3 up -d     # + Caddy (TLS 1.3) + MinIO
```

In the Meta App, configure: **Callback URL** `https://YOUR_DOMAIN/webhooks/whatsapp`, **Verify token** = `META_WEBHOOK_VERIFY_TOKEN`,
field `messages` (and `message_template_status_update`). Embedded Signup: `META_APP_ID`, `META_EMBEDDED_SIGNUP_CONFIG_ID`, `META_MODE=live`.

## Structure

```
backend/app/  config.py · db.py · main.py · worker.py · registry.py · seed.py
  core/       events (bus) · audit · scheduler · storage · ratelimit · middleware · deps
  security/   crypto · passwords · tokens · rbac · masking · sanitize
  models/     core · crm · ops · whatsapp · compliance · integrations
  modules/    auth · core · pipeline · ops · whatsapp · webhooks · lgpd · dashboard · openapi
backend/alembic/   migrations (monthly partitions + immutable audit)
backend/tests/     29 tests (security, CRM, end-to-end WhatsApp flow, LGPD, webhooks)
frontend/          SPA (index.html + static/js/pages/*) · design system in static/css/app.css
integrations/php/  lead-capture form (PHP 8)
```

## Known limitations (read before going to production)

- The Meta integration was built against the Cloud API documentation and tested in **sandbox mode**; it must be validated in `live` mode
  with a real WABA (Embedded Signup, templates, media and webhooks).
- **PostgreSQL 16 / Redis 7 validated** (docker compose, Windows, 2026-10-17): `alembic upgrade head` and `downgrade base` run without errors;
  `messages` is partitioned by month (`messages_default` + partitions), enums are native, `audit_logs` rejects UPDATE/DELETE/TRUNCATE via trigger
  and `alembic check` reports no divergence between models and migration. The full suite passes on SQLite (29 tests) and on PostgreSQL + Redis
  (29 + `test_redis_bus.py` + `test_postgres_migration.py`). The daily `db.partitions` job (`ensure_message_partitions`) was exercised and is idempotent.
  Also validated with the full stack in containers: concurrent seed and migration (advisory locks), end-to-end flow with the worker over Redis
  Streams, concurrent routing (`FOR UPDATE SKIP LOCKED`, honoring `max_concurrent_chats`), webhook idempotency under concurrency,
  an intact audit chain under concurrent writes, and `pg_dump`/`pg_restore` (partitions and triggers preserved).
  Not validated: sustained load at production scale, Redis cluster/Sentinel, and Meta validation in `live` mode.
- NFS-e and payment gateways are **ports** (`NfseProvider`, `PixGateway`); the default generates a static PIX BR Code and leaves the NFS-e pending.
- KPIs use the transactional database (fast thanks to the snapshots on `tickets`); for large volumes, plug ClickHouse/Elasticsearch into the same API.
- LIS/PACS integration: use outbound webhooks + Open API; specific adapters depend on each laboratory's protocol.
