"""Seed: initial organization, admin user, departments, Flowmentor catalog and (optionally) demo data."""
from __future__ import annotations

import logging
import random
import secrets
from datetime import date, timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.config import settings
from app.db import session_scope, utcnow
from app.models import (
    Account, AccountContact, AccountType, BillingModality, Contact, Department, Deal, DealItem, DealStage, DealStageHistory, Organization, ProductService, User, UserDepartment,
    WhatsAppAccount,
)
from app.models.crm import STAGE_PROBABILITY
from app.security.crypto import blind_index
from app.security.passwords import hash_password

log = logging.getLogger("flowmentor.seed")

DEPARTMENTS = [
    ("Comercial", 300, 3600, "round_robin", True), ("Consultoria Técnica", 600, 14400, "skill_based", False), ("Apoio Diagnóstico", 300, 7200, "least_busy", False),
    ("Canal Médico", 600, 7200, "least_busy", False), ("Capacitação", 900, 14400, "round_robin", False),
]

CATALOG = [
    ("CONS-HORA", "Hora técnica de consultoria", "Consultoria de Processos", BillingModality.hourly, "hora", "300.00", "Validação de painéis imunofenotípicos, padronização EuroFlow/OMS e elaboração de POPs."),
    ("CONS-POP", "Elaboração de POPs (pacote)", "Consultoria de Processos", BillingModality.fixed_project, "projeto", "6500.00", "Pacote de procedimentos operacionais padrão para o laboratório de citometria."),
    ("CONS-VAL", "Validação de painel imunofenotípico", "Consultoria de Processos", BillingModality.fixed_project, "painel", "4800.00", "Validação analítica e documentação de painel conforme EuroFlow/OMS."),
    ("DIAG-EXAME", "Laudo de citometria — alta complexidade", "Apoio Diagnóstico", BillingModality.unit_volume, "exame", "185.00", "Processamento de dados brutos (.FCS) e emissão de laudo."),
    ("DIAG-URG", "Laudo urgente (SLA 4h)", "Apoio Diagnóstico", BillingModality.unit_volume, "exame", "290.00", "Análise prioritária com entrega em até 4 horas."),
    ("DIAG-BANCO", "Banco de horas de análise mensal", "Apoio Diagnóstico", BillingModality.recurring_subscription, "mês", "4200.00", "Franquia mensal de horas de análise e discussão de casos."),
    ("MED-REUN", "Reunião clínica multidisciplinar", "Canal Médico", BillingModality.unit_volume, "reunião", "950.00", "Discussão de casos com hematologistas e equipe multidisciplinar."),
    ("MED-INTER", "Plano de interconsulta mensal", "Canal Médico", BillingModality.recurring_subscription, "mês", "1800.00", "Suporte técnico a hematologistas e médicos residentes."),
    ("CAP-ANUAL", "Assinatura anual — Escola de Citometria", "Capacitação", BillingModality.recurring_subscription, "ano", "1490.00", "Acesso à plataforma educacional e à comunidade."),
    ("CAP-INST", "Plano institucional de treinamento", "Capacitação", BillingModality.fixed_project, "plano", "12900.00", "Treinamento in company e acesso institucional para até 20 profissionais."),
]


async def bootstrap() -> None:
    async with session_scope() as s:
        if s.bind.dialect.name == "postgresql":  # several workers/replicas start together: serializes the initial seed
            await s.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext('flowmentor:bootstrap'))"))
        org = (await s.execute(sa.select(Organization).limit(1))).scalar_one_or_none()
        if org is not None:
            return
        org = Organization(name=settings.bootstrap_org_name, slug="flowmentor", settings={})
        s.add(org)
        await s.flush()
        admin_pw = settings.bootstrap_admin_password
        if not admin_pw:
            admin_pw = secrets.token_urlsafe(16)
            log.warning("BOOTSTRAP_ADMIN_PASSWORD não definida: senha gerada para %s (exibida só agora): %s", settings.bootstrap_admin_email, admin_pw)
        admin = User(organization_id=org.id, name="Administrador", email=settings.bootstrap_admin_email.lower(), role="admin", password_hash=hash_password(admin_pw), status="offline")
        s.add(admin)
        depts = []
        for name, frt, sol, strat, default in DEPARTMENTS:
            d = Department(organization_id=org.id, name=name, sla_first_response_seconds=frt, sla_solution_seconds=sol, routing_strategy=strat, is_default=default,
                           required_skills=[])
            s.add(d)
            depts.append(d)
        for sku, name, cat, mod, unit, price, desc in CATALOG:
            s.add(ProductService(organization_id=org.id, sku=sku, name=name, module_category=cat, billing_modality=mod, unit_label=unit, base_unit_price=Decimal(price), description=desc))
        await s.flush()
        log.info("Organização inicial criada. Login: %s", settings.bootstrap_admin_email)
        if settings.seed_demo_data:
            await _demo(s, org, depts)


async def _demo(s, org: Organization, depts: list[Department]) -> None:
    demo_pw = settings.demo_user_password
    if not demo_pw:
        demo_pw = secrets.token_urlsafe(16)
        log.warning("DEMO_USER_PASSWORD não definida: senha gerada para os usuários de demonstração (exibida só agora): %s", demo_pw)
    pw = hash_password(demo_pw)
    people = [("Marina Duarte", "supervisor@flowmentor.com.br", "supervisor", []), ("Dr. Rafael Nogueira", "consultor@flowmentor.com.br", "consultor", ["consultoria", "apoio_diagnostico"]),
              ("Camila Teixeira", "atendente1@flowmentor.com.br", "agent", ["comercial", "capacitacao"]), ("Bruno Almeida", "atendente2@flowmentor.com.br", "agent", ["comercial", "canal_medico"]),
              ("Dra. Helena Souza", "auditor@flowmentor.com.br", "auditor", [])]
    users = []
    for name, email, role, skills in people:
        u = User(organization_id=org.id, name=name, email=email, role=role, password_hash=pw, skills=skills, status="offline")
        s.add(u)
        users.append(u)
    await s.flush()
    for u in users:
        if u.role in ("agent", "consultor"):
            for d in depts:
                s.add(UserDepartment(user_id=u.id, department_id=d.id))
    wa = WhatsAppAccount(organization_id=org.id, label="Flowmentor — Principal", waba_id="100000000000001", phone_number_id="100000000000002", display_phone_number="+55 62 99999-0000",
                         verified_name="Flowmentor (sandbox)", access_token="SANDBOX_TOKEN_DEMO", status="active", quality_rating="GREEN", messaging_limit="TIER_1K", default_department_id=depts[0].id)
    s.add(wa)
    prods = (await s.execute(sa.select(ProductService).where(ProductService.organization_id == org.id))).scalars().all()
    by_sku = {p.sku: p for p in prods}
    accs = [
        ("Laboratório Central de Hematologia Ltda", "Hemolab", AccountType.laboratorio, "12.345.678/0001-90", "Goiânia", "GO", "customer", 5),
        ("Hospital Santa Clara S.A.", "Santa Clara", AccountType.hospital, "23.456.789/0001-01", "São Paulo", "SP", "prospect", 8),
        ("Clínica Onco Vida Ltda", "Onco Vida", AccountType.clinica, "34.567.890/0001-12", "Brasília", "DF", "prospect", 0),
        ("Universidade Federal do Cerrado", "UFCerrado", AccountType.universidade, "45.678.901/0001-23", "Goiânia", "GO", "prospect", 12),
    ]
    stages = [DealStage.proposta_apresentada, DealStage.negociacao, DealStage.qualificacao_tecnica, DealStage.reuniao_alinhamento]
    random.seed(7)
    for i, (corp, trade, typ, cnpj, city, uf, status, disc) in enumerate(accs):
        digits = "".join(ch for ch in cnpj if ch.isdigit())
        a = Account(organization_id=org.id, corporate_name=corp, trade_name=trade, tax_id=digits, tax_id_hash=blind_index(digits, "tax_id"), account_type=typ, city=city, state=uf, status=status,
                    institutional_discount_pct=disc, owner_user_id=users[2 + (i % 2)].id, tags=[])
        s.add(a)
        await s.flush()
        c = Contact(organization_id=org.id, wa_id=f"55629{9000000 + i * 1111:07d}", name=["Dr. Paulo Mendes", "Dra. Letícia Ramos", "Carlos Eduardo Lima", "Profa. Aline Prado"][i],
                    email=f"contato{i}@{trade.lower().replace(' ', '')}.com.br", job_title=["Hematologista", "Diretora Técnica", "Responsável Técnico", "Coordenadora"][i], source="manual")
        s.add(c)
        await s.flush()
        s.add(AccountContact(account_id=a.id, contact_id=c.id, role_description=c.job_title, council_registration=["CRM-GO 12345", "CRM-SP 67890", "CRBM-3 4567", "CRM-GO 99871"][i], is_primary=True))
        d = Deal(organization_id=org.id, account_id=a.id, lead_contact_id=c.id, assigned_user_id=users[2 + (i % 2)].id, title=["Apoio diagnóstico mensal — Hemolab", "Validação de painéis — Santa Clara",
                                                                                                                              "Consultoria de POPs — Onco Vida", "Treinamento institucional — UFCerrado"][i],
                 stage=stages[i], source=["indicacao", "evento", "whatsapp", "site"][i], probability=STAGE_PROBABILITY[stages[i].value], discount_percentage=Decimal(disc))
        s.add(d)
        await s.flush()
        items = [[("DIAG-EXAME", 120)], [("CONS-VAL", 3), ("CONS-HORA", 40)], [("CONS-POP", 1), ("CONS-HORA", 20)], [("CAP-INST", 1), ("CAP-ANUAL", 20)]][i]
        total = Decimal(0)
        for sku, qty in items:
            sub = by_sku[sku].base_unit_price * Decimal(qty)
            total += sub
            s.add(DealItem(deal_id=d.id, product_service_id=by_sku[sku].id, description=by_sku[sku].name, quantity=Decimal(qty), unit_price=by_sku[sku].base_unit_price, subtotal=sub))
        d.expected_value = total
        d.final_contracted_value = (total * (Decimal(100) - Decimal(disc)) / Decimal(100)).quantize(Decimal("0.01"))
        s.add(DealStageHistory(deal_id=d.id, from_stage=None, to_stage=stages[i].value))
        d.expected_close_date = date.today() + timedelta(days=15 + i * 10)
