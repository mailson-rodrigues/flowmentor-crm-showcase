# Flowmentor CRM

CRM **100% web** para a Flowmentor (citometria de fluxo, consultoria laboratorial, apoio diagnóstico e capacitação),
com **BSP de WhatsApp** (Cloud API da Meta), pipeline comercial B2B, casos/laudos, contratos & faturamento (PIX),
pesquisa de satisfação nativa no WhatsApp, KPIs automáticos, webhooks/Open API e conformidade **LGPD**.

> Stack: **Python 3.12 + FastAPI + SQLAlchemy 2 (async)** · PostgreSQL (SQLite em dev) · Redis Streams · Socket.IO ·
> Frontend SPA em JavaScript puro (ES modules, sem build) · PHP apenas para integrações de formulário.

## Início rápido (Windows / VS Code)

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Abra <http://localhost:8000>. O `backend/.env` de desenvolvimento já usa **SQLite**, **Meta em sandbox** e dados de demonstração.

| Usuário | Perfil |
|---|---|
| `admin@flowmentor.com.br` | Administrador |
| `supervisor@flowmentor.com.br` | Supervisor |
| `consultor@flowmentor.com.br` | Consultor Técnico |
| `atendente1@…` / `atendente2@…` | Atendentes |
| `auditor@flowmentor.com.br` | Auditor / DPO |

> **Nenhuma senha é versionada.** Defina `BOOTSTRAP_ADMIN_PASSWORD` (administrador) e `DEMO_USER_PASSWORD` (usuários de demonstração) no `backend/.env`.
> Se ficarem vazias, o app gera senhas aleatórias na primeira execução e as registra **uma única vez** no log de inicialização (só fora de produção).
> Use também uma `SECRET_KEY` própria. Em produção o app recusa subir com configuração insegura.

**Testar o WhatsApp sem credenciais da Meta:** entre como `consultor`, ponha o status em *Disponível*, e em
*Configurações → WhatsApp → Simular mensagem recebida* (como admin) injete uma mensagem. O fluxo completo roda:
bot → menu → handoff → roteamento → atendimento em tempo real → encerramento → pesquisa CSAT/NPS → KPIs.

Atalhos no VS Code: `Ctrl+Shift+B`/`Terminal → Run Task` (setup, servidor, testes, migrações) e **Run and Debug** (API, worker, pytest).

```powershell
cd backend; .\.venv\Scripts\python.exe -m pytest -q     # 29 testes automatizados (SQLite em memória)
# contra PostgreSQL/Redis reais (o schema do banco de teste é recriado!):
#   $env:POSTGRES_PASSWORD="<senha-local>"; docker compose -f docker-compose.yml -f docker-compose.test.yml up -d db redis
#   docker compose exec db createdb -U flowmentor flowmentor_test
#   $env:TEST_DATABASE_URL="postgresql+asyncpg://flowmentor:<senha-local>@127.0.0.1:5432/flowmentor_test"; $env:TEST_REDIS_URL="redis://127.0.0.1:6379/15"
#   .\.venv\Scripts\python.exe -m pytest -q
```

## Validação (stack completa e frontend)

Stack de homologação local (PostgreSQL 16 + Redis 7 + API + worker, Meta em sandbox, dados de demonstração):

```powershell
# .env na raiz (não versionado): copie .env.example e preencha SECRET_KEY, KEK_KEYS, POSTGRES_PASSWORD, META_APP_SECRET...
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
cd backend
$env:META_APP_SECRET="<mesmo valor do .env>"; $env:DEMO_USER_PASSWORD="<mesma senha dos usuários de demonstração>"
.\.venv\Scripts\python.exe scripts\live_smoke.py          # fluxo ponta a ponta (webhook → bot → roteamento → transferência → KPIs → auditoria)
.\.venv\Scripts\python.exe scripts\concurrency_check.py   # idempotência, roteamento SKIP LOCKED e cadeia de auditoria sob concorrência
```

**Página de teste do frontend:** abra <http://localhost:8000/static/test/index.html>, informe a senha dos usuários de demonstração e clique em
*Executar todos os testes*. Ela exercita a SPA contra a API real: assets e cabeçalhos de segurança, importação de todos os módulos, matriz
**RBAC por perfil** (rotas visíveis na UI × resposta do servidor), renderização de cada página sem erros de console/HTTP, páginas de detalhe,
resistência a XSS, tempo real (Socket.IO ← worker ← Redis), acessibilidade básica e estilos responsivos. O resultado pode ser exportado em JSON.
Use apenas em homologação/desenvolvimento (o teste cria e exclui uma conta e abre um atendimento simulado).

## Módulos

| Módulo | O que entrega | Código |
|---|---|---|
| **Core / Cadastros** | Multi-tenant, contas institucionais (hospital, laboratório, clínica…), interlocutores (CRM/CRBM), RBAC + ABAC, mascaramento de PII | `app/modules/core` |
| **Pipeline CRM** | Kanban/tabela, catálogo (4 eixos), itens, **desconto institucional automático** com alçada, propostas contratuais (HTML imprimível/PDF), timeline unificada | `app/modules/pipeline` |
| **Operação** | Casos, **horas consumidas × contratadas** (alertas 80%/100%), laudos (SLA 24h/4h), contratos, faturas, PIX copia-e-cola (BR Code), NFS-e (porta) | `app/modules/ops` |
| **WhatsApp BSP** | Embedded Signup, webhook < 200 ms com HMAC, filas, templates HSM, mídia em bucket com URL assinada, roteamento, bot + handoff, notas internas, transferência, tempo real | `app/modules/whatsapp` |
| **Pesquisa de satisfação** | CSAT / NPS (2 etapas) / CES por botões e listas, comentário + sentimento (NLP), anti-spam, expiração | `whatsapp/surveys.py` |
| **KPIs** | FRT, TMA/AHT, FCR, throughput, CSAT/NPS/CES, ocupação do agente, SLA, painel ao vivo, **whisper** e **barge-in** | `whatsapp/kpis.py`, `supervisor.py` |
| **Webhooks / Open API** | Endpoints outbound assinados (HMAC), retry exponencial, catálogo de eventos, chaves de API, captura de leads | `app/modules/webhooks`, `openapi` |
| **LGPD & Auditoria** | Consentimentos, direitos do titular (arts. 18/19), anonimização/eliminação, auditoria **append-only encadeada por hash** | `app/modules/lgpd`, `core/audit.py` |

## Arquitetura (Ports & Adapters + barramento de eventos)

```
Navegador (SPA) ──HTTPS/WSS──► API Gateway (FastAPI: JWT, RBAC/ABAC, rate limit, CSP/HSTS)
                                   │ módulos: Core · Pipeline · Operação · WhatsApp · LGPD · Webhooks
                                   ▼
                      Event Bus (Redis Streams | memória)  ◄── Meta Webhook (200 OK imediato)
                      │        │          │           │
           whatsapp.processor  bridge   routing    webhooks.dispatch · realtime(Socket.IO) · surveys · ops
                      ▼
          PostgreSQL (mensagens particionadas por mês) · S3/GCS (mídias) · KMS/KEK (cripto de envelope)
```

Detalhes, catálogo de eventos e decisões em [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md).

## Segurança & LGPD (implementado)

- **Trânsito:** HSTS, CSP estrita, `X-Frame-Options`, `nosniff`, COOP/CORP; TLS 1.3 no proxy ([`deploy/Caddyfile`](deploy/Caddyfile)).
- **Repouso:** envelope encryption AES-256-GCM (DEK por valor + KEK rotativa) para CNPJ e tokens da Meta; *blind index* para busca.
- **Acesso:** JWT curto + refresh com **rotação e detecção de reutilização**, Argon2id, bloqueio por tentativas, rate limit, RBAC (6 perfis) + ABAC (vendedor só vê o seu; agente só vê a fila do seu departamento).
- **Privacidade:** telefones/CNPJ mascarados por perfil; *revelar* exige permissão e grava `VIEW_PHONE`; opt-out por “PARAR”; consentimento registrado.
- **Auditoria:** `audit_logs` encadeado por SHA-256 + triggers `BEFORE UPDATE/DELETE/TRUNCATE` no PostgreSQL (migração `0001`).
- **Entrada não confiável:** sanitização de mensagens do WhatsApp, frontend sem `innerHTML`, anti-SSRF nos webhooks, ORM parametrizado.

## Produção

```powershell
copy .env.example .env      # preencha SECRET_KEY, KEK_KEYS, POSTGRES_PASSWORD, META_*
docker compose up -d --build                          # api + worker + postgres + redis
docker compose --profile proxy --profile s3 up -d     # + Caddy (TLS 1.3) + MinIO
```

Configure no App da Meta: **Callback URL** `https://SEU_DOMINIO/webhooks/whatsapp`, **Verify token** = `META_WEBHOOK_VERIFY_TOKEN`,
campo `messages` (e `message_template_status_update`). Embedded Signup: `META_APP_ID`, `META_EMBEDDED_SIGNUP_CONFIG_ID`, `META_MODE=live`.

## Estrutura

```
backend/app/  config.py · db.py · main.py · worker.py · registry.py · seed.py
  core/       events (bus) · audit · scheduler · storage · ratelimit · middleware · deps
  security/   crypto · passwords · tokens · rbac · masking · sanitize
  models/     core · crm · ops · whatsapp · compliance · integrations
  modules/    auth · core · pipeline · ops · whatsapp · webhooks · lgpd · dashboard · openapi
backend/alembic/   migrações (partições mensais + auditoria imutável)
backend/tests/     29 testes (segurança, CRM, fluxo WhatsApp ponta a ponta, LGPD, webhooks)
frontend/          SPA (index.html + static/js/pages/*) · design system em static/css/app.css
integrations/php/  formulário de captura de leads (PHP 8)
```

## Limitações conhecidas (leia antes de ir a produção)

- A integração com a Meta foi desenvolvida contra a documentação da Cloud API e testada em **modo sandbox**; é necessário homologar em `live`
  com um WABA real (Embedded Signup, templates, mídia e webhooks).
- **PostgreSQL 16 / Redis 7 validados** (docker compose, Windows, 2026-10-17): `alembic upgrade head` e `downgrade base` executam sem erro;
  `messages` é particionada por mês (`messages_default` + partições), os enums são nativos, `audit_logs` rejeita UPDATE/DELETE/TRUNCATE por trigger
  e `alembic check` não aponta divergência entre modelos e migração. A suíte completa passa em SQLite (29 testes) e em PostgreSQL + Redis
  (29 + `test_redis_bus.py` + `test_postgres_migration.py`). O job diário `db.partitions` (`ensure_message_partitions`) foi exercitado e é idempotente.
  Validados também com a stack completa em containers: seed e migração concorrentes (advisory locks), fluxo ponta a ponta com worker via Redis
  Streams, roteamento concorrente (`FOR UPDATE SKIP LOCKED`, respeitando `max_concurrent_chats`), idempotência de webhooks sob concorrência,
  cadeia de auditoria íntegra sob escrita concorrente e `pg_dump`/`pg_restore` (partições e triggers preservados).
  Não validado: carga sustentada em escala de produção, Redis em cluster/Sentinel e homologação da Meta em modo `live`.
- NFS-e e gateways de pagamento são **portas** (`NfseProvider`, `PixGateway`); o padrão gera BR Code PIX estático e deixa a NFS-e pendente.
- KPIs usam o banco transacional (rápido graças aos snapshots em `tickets`); para grandes volumes, plugue ClickHouse/Elasticsearch na mesma API.
- Integração LIS/PACS: use webhooks outbound + Open API; adaptadores específicos dependem do protocolo de cada laboratório.
