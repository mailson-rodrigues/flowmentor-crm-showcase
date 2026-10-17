"""Pipeline CRM: catálogo de produtos, oportunidades (kanban/tabela), itens, descontos, propostas."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.deps import Page, Principal, get_principal, pagination, require
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.events import emit
from app.core.serialize import row
from app.core.storage import sign_media, verify_media
from app.db import get_session, utcnow
from app.models import (
    Account, Activity, BillingModality, Contact, CrmDealTicket, Deal, DealItem, DealStage, DealStageHistory, Organization, ProductService, Proposal, Ticket,
)
from app.models.crm import STAGE_ORDER, STAGE_PROBABILITY
from app.modules.pipeline import service as svc
from app.modules.pipeline.proposal_html import render_proposal
from app.security.sanitize import sanitize_text

router = APIRouter(prefix="/api/v1", tags=["Pipeline CRM"])
public_router = APIRouter(tags=["Propostas (link público assinado)"])


# ------------------------------------------------------------------ Catálogo
class ProductIn(BaseModel):
    sku: str = Field(min_length=2, max_length=50)
    name: str = Field(min_length=2, max_length=150)
    description: str | None = None
    module_category: str = Field(min_length=2, max_length=80)
    billing_modality: BillingModality
    unit_label: str | None = None
    base_unit_price: Decimal = Field(ge=0)
    is_active: bool = True


PRODUCT_FIELDS = ["id", "sku", "name", "description", "module_category", "billing_modality", "unit_label", "base_unit_price", "is_active"]


@router.get("/products")
async def list_products(active_only: bool = False, p: Principal = Depends(require("products:read")), session: AsyncSession = Depends(get_session)):
    q = sa.select(ProductService).where(ProductService.organization_id == p.org_id).order_by(ProductService.module_category, ProductService.name)
    if active_only:
        q = q.where(ProductService.is_active.is_(True))
    return {"items": [row(x, PRODUCT_FIELDS) for x in (await session.execute(q)).scalars().all()]}


@router.post("/products", status_code=201)
async def create_product(body: ProductIn, p: Principal = Depends(require("products:manage")), session: AsyncSession = Depends(get_session)):
    if (await session.execute(sa.select(ProductService.id).where(ProductService.organization_id == p.org_id, ProductService.sku == body.sku))).first():
        raise AppError(409, "sku_taken", "SKU já cadastrado")
    x = ProductService(organization_id=p.org_id, **body.model_dump())
    session.add(x)
    await session.flush()
    return row(x, PRODUCT_FIELDS)


@router.put("/products/{product_id}")
async def update_product(product_id: uuid.UUID, body: ProductIn, p: Principal = Depends(require("products:manage")), session: AsyncSession = Depends(get_session)):
    x = await session.get(ProductService, product_id)
    if not x or x.organization_id != p.org_id:
        raise not_found("Produto")
    for k, v in body.model_dump().items():
        setattr(x, k, v)
    return row(x, PRODUCT_FIELDS)


# ------------------------------------------------------------------ Oportunidades
class ItemIn(BaseModel):
    product_service_id: uuid.UUID
    quantity: Decimal = Field(default=Decimal("1"), gt=0)
    unit_price: Decimal | None = None
    description: str | None = None


class DealIn(BaseModel):
    account_id: uuid.UUID
    title: str = Field(min_length=2, max_length=200)
    lead_contact_id: uuid.UUID | None = None
    assigned_user_id: uuid.UUID | None = None
    source: str | None = None
    expected_close_date: date | None = None
    notes: str | None = None
    items: list[ItemIn] = []
    stage: DealStage = DealStage.lead_inbound


class DealPatch(BaseModel):
    title: str | None = None
    lead_contact_id: uuid.UUID | None = None
    assigned_user_id: uuid.UUID | None = None
    source: str | None = None
    expected_close_date: date | None = None
    notes: str | None = None
    discount_percentage: Decimal | None = Field(None, ge=0, le=100)


class StageIn(BaseModel):
    stage: str
    loss_reason: str | None = None
    position: float | None = None


def deal_out(d: Deal, with_items: bool = True) -> dict:
    out = row(d, ["id", "title", "account_id", "lead_contact_id", "assigned_user_id", "stage", "source", "position", "probability",
                  "expected_value", "discount_percentage", "final_contracted_value", "expected_close_date", "loss_reason", "closed_at", "created_at", "updated_at", "notes"])
    out["account_name"] = (d.account.trade_name or d.account.corporate_name) if d.account else None
    if with_items:
        out["items"] = [row(i, ["id", "product_service_id", "description", "quantity", "unit_price", "subtotal"],
                            sku=i.product.sku if i.product else None, billing_modality=i.product.billing_modality if i.product else None,
                            module_category=i.product.module_category if i.product else None) for i in d.items]
    return out


def _deal_scope(p: Principal, stmt):
    stmt = stmt.where(Deal.organization_id == p.org_id)
    if not p.can("deals:all") and p.user_id:  # ABAC: vendedor só enxerga o que é seu (ou sem responsável)
        stmt = stmt.where(sa.or_(Deal.assigned_user_id == p.user_id, Deal.assigned_user_id.is_(None)))
    return stmt


async def _deal_or_404(session: AsyncSession, p: Principal, deal_id: uuid.UUID) -> Deal:
    d = (await session.execute(_deal_scope(p, sa.select(Deal).where(Deal.id == deal_id)))).scalar_one_or_none()
    if not d:
        raise not_found("Oportunidade")
    return d


@router.get("/deals")
async def list_deals(stage: str | None = None, account_id: uuid.UUID | None = None, assigned_user_id: uuid.UUID | None = None, q: str | None = None,
                     page: Page = Depends(pagination), p: Principal = Depends(require("deals:read")), session: AsyncSession = Depends(get_session)):
    stmt = _deal_scope(p, sa.select(Deal))
    if stage:
        stmt = stmt.where(Deal.stage == stage)
    if account_id:
        stmt = stmt.where(Deal.account_id == account_id)
    if assigned_user_id:
        stmt = stmt.where(Deal.assigned_user_id == assigned_user_id)
    if q:
        stmt = stmt.where(sa.func.lower(Deal.title).like(f"%{q.lower()}%"))
    total = (await session.execute(sa.select(sa.func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.order_by(Deal.updated_at.desc()).limit(page.limit).offset(page.offset))).scalars().all()
    return {"total": total, "items": [deal_out(d, with_items=False) for d in rows]}


@router.get("/deals/board")
async def deals_board(assigned_user_id: uuid.UUID | None = None, include_closed: bool = True, p: Principal = Depends(require("deals:read")), session: AsyncSession = Depends(get_session)):
    stmt = _deal_scope(p, sa.select(Deal))
    if assigned_user_id:
        stmt = stmt.where(Deal.assigned_user_id == assigned_user_id)
    deals = (await session.execute(stmt.order_by(Deal.position, Deal.created_at))).scalars().all()
    columns = []
    for st in STAGE_ORDER:
        if not include_closed and st.startswith("fechado"):
            continue
        items = [d for d in deals if d.stage.value == st]
        total = sum(float(d.final_contracted_value or d.expected_value or 0) for d in items)
        columns.append({"stage": st, "count": len(items), "total_value": total,
                        "weighted_value": total * STAGE_PROBABILITY[st] / 100, "deals": [deal_out(d, with_items=False) for d in items]})
    return {"columns": columns}


@router.post("/deals", status_code=201)
async def create_deal(body: DealIn, request: Request, p: Principal = Depends(require("deals:write")), session: AsyncSession = Depends(get_session)):
    acc = await session.get(Account, body.account_id)
    if not acc or acc.organization_id != p.org_id:
        raise not_found("Conta")
    d = Deal(organization_id=p.org_id, account_id=acc.id, title=sanitize_text(body.title, 200), lead_contact_id=body.lead_contact_id,
             assigned_user_id=body.assigned_user_id or p.user_id, source=body.source, expected_close_date=body.expected_close_date,
             notes=sanitize_text(body.notes, 4000), stage=DealStage.lead_inbound, probability=STAGE_PROBABILITY["lead_inbound"])
    session.add(d)
    await session.flush()
    await session.refresh(d, ["items", "account"])
    for it in body.items:
        d.items.append(await svc.build_item(session, p.org_id, it.product_service_id, it.quantity, it.unit_price, p.can("deals:discount_high"), it.description))
    svc.recalc_deal(d)
    session.add(DealStageHistory(deal_id=d.id, from_stage=None, to_stage="lead_inbound", user_id=p.user_id))
    session.add(Activity(organization_id=p.org_id, account_id=acc.id, deal_id=d.id, user_id=p.user_id, kind="deal_created", title=f"Oportunidade criada: {d.title}"))
    if body.stage != DealStage.lead_inbound:
        await svc.move_stage(session, d, body.stage.value, p)
    await audit.log_action(session, org_id=p.org_id, action="CREATE_DEAL", resource_type="deals", resource_id=d.id, actor_user_id=p.user_id, request=request, new_state={"title": d.title})
    emit(session, "deal.created", p.org_id, {"deal_id": str(d.id), "account_id": str(acc.id), "title": d.title}, p.user_id)
    return deal_out(d)


@router.get("/deals/{deal_id}")
async def get_deal(deal_id: uuid.UUID, p: Principal = Depends(require("deals:read")), session: AsyncSession = Depends(get_session)):
    d = await _deal_or_404(session, p, deal_id)
    out = deal_out(d)
    hist = (await session.execute(sa.select(DealStageHistory).where(DealStageHistory.deal_id == d.id).order_by(DealStageHistory.created_at))).scalars().all()
    out["history"] = [row(h, ["from_stage", "to_stage", "user_id", "created_at"]) for h in hist]
    tickets = (await session.execute(sa.select(Ticket).join(CrmDealTicket, CrmDealTicket.ticket_id == Ticket.id).where(CrmDealTicket.deal_id == d.id))).scalars().all()
    out["tickets"] = [row(t, ["id", "status", "subject", "created_at", "closed_at"]) for t in tickets]
    props = (await session.execute(sa.select(Proposal).where(Proposal.deal_id == d.id).order_by(Proposal.created_at.desc()))).scalars().all()
    out["proposals"] = [row(x, ["id", "number", "version", "status", "valid_until", "sent_at", "created_at"]) for x in props]
    out["discount_suggestion"] = await svc.suggest_discount(session, p.org_id, d)
    return out


@router.patch("/deals/{deal_id}")
async def update_deal(deal_id: uuid.UUID, body: DealPatch, request: Request, p: Principal = Depends(require("deals:write")), session: AsyncSession = Depends(get_session)):
    d = await _deal_or_404(session, p, deal_id)
    if d.stage in (DealStage.fechado_ganho, DealStage.fechado_perdido):
        raise bad_request("Negócio encerrado não pode ser editado", code="deal_closed")
    old = {"title": d.title, "discount_percentage": float(d.discount_percentage or 0), "assigned_user_id": str(d.assigned_user_id)}
    data = body.model_dump(exclude_unset=True)
    if "discount_percentage" in data and data["discount_percentage"] is not None:
        await svc.check_discount_permission(session, p, float(data["discount_percentage"]))
        d.discount_percentage = data.pop("discount_percentage")
        if float(d.discount_percentage) > await svc.auto_approve_limit(session, p.org_id):
            d.discount_approved_by = p.user_id
    data.pop("discount_percentage", None)
    for k, v in data.items():
        setattr(d, k, sanitize_text(v, 4000) if isinstance(v, str) else v)
    svc.recalc_deal(d)
    await audit.log_action(session, org_id=p.org_id, action="UPDATE_DEAL", resource_type="deals", resource_id=d.id, actor_user_id=p.user_id, request=request,
                           old_state=old, new_state={"title": d.title, "discount_percentage": float(d.discount_percentage or 0), "assigned_user_id": str(d.assigned_user_id)})
    emit(session, "deal.updated", p.org_id, {"deal_id": str(d.id)}, p.user_id)
    return deal_out(d)


@router.put("/deals/{deal_id}/stage")
async def change_stage(deal_id: uuid.UUID, body: StageIn, request: Request, p: Principal = Depends(require("deals:write")), session: AsyncSession = Depends(get_session)):
    d = await _deal_or_404(session, p, deal_id)
    old = d.stage.value
    await svc.move_stage(session, d, body.stage, p, body.loss_reason, body.position)
    await audit.log_action(session, org_id=p.org_id, action="UPDATE_DEAL", resource_type="deals", resource_id=d.id, actor_user_id=p.user_id, request=request,
                           old_state={"stage": old}, new_state={"stage": d.stage.value})
    return deal_out(d)


@router.post("/deals/{deal_id}/items", status_code=201)
async def add_item(deal_id: uuid.UUID, body: ItemIn, p: Principal = Depends(require("deals:write")), session: AsyncSession = Depends(get_session)):
    d = await _deal_or_404(session, p, deal_id)
    if d.stage in (DealStage.fechado_ganho, DealStage.fechado_perdido):
        raise bad_request("Negócio encerrado", code="deal_closed")
    d.items.append(await svc.build_item(session, p.org_id, body.product_service_id, body.quantity, body.unit_price, p.can("deals:discount_high"), body.description))
    svc.recalc_deal(d)
    await session.flush()
    return deal_out(d)


@router.put("/deals/{deal_id}/items/{item_id}")
async def update_item(deal_id: uuid.UUID, item_id: uuid.UUID, body: ItemIn, p: Principal = Depends(require("deals:write")), session: AsyncSession = Depends(get_session)):
    d = await _deal_or_404(session, p, deal_id)
    it = next((i for i in d.items if i.id == item_id), None)
    if not it:
        raise not_found("Item")
    it.quantity = body.quantity
    if body.unit_price is not None and p.can("deals:discount_high"):
        it.unit_price = body.unit_price
    if body.description:
        it.description = body.description
    svc.recalc_deal(d)
    return deal_out(d)


@router.delete("/deals/{deal_id}/items/{item_id}")
async def remove_item(deal_id: uuid.UUID, item_id: uuid.UUID, p: Principal = Depends(require("deals:write")), session: AsyncSession = Depends(get_session)):
    d = await _deal_or_404(session, p, deal_id)
    it = next((i for i in d.items if i.id == item_id), None)
    if not it:
        raise not_found("Item")
    d.items.remove(it)
    await session.delete(it)
    svc.recalc_deal(d)
    return deal_out(d)


@router.post("/deals/{deal_id}/discount/apply-suggestion")
async def apply_suggested_discount(deal_id: uuid.UUID, p: Principal = Depends(require("deals:write")), session: AsyncSession = Depends(get_session)):
    d = await _deal_or_404(session, p, deal_id)
    sug = await svc.suggest_discount(session, p.org_id, d)
    await svc.check_discount_permission(session, p, sug["suggested_pct"])
    d.discount_percentage = Decimal(str(sug["suggested_pct"]))
    svc.recalc_deal(d)
    return {"deal": deal_out(d), "suggestion": sug}


@router.post("/deals/{deal_id}/link-ticket/{ticket_id}", status_code=201)
async def link_ticket(deal_id: uuid.UUID, ticket_id: uuid.UUID, p: Principal = Depends(require("deals:write")), session: AsyncSession = Depends(get_session)):
    d = await _deal_or_404(session, p, deal_id)
    t = await session.get(Ticket, ticket_id)
    if not t or t.organization_id != p.org_id:
        raise not_found("Ticket")
    if not (await session.execute(sa.select(CrmDealTicket.id).where(CrmDealTicket.deal_id == d.id, CrmDealTicket.ticket_id == t.id))).first():
        session.add(CrmDealTicket(deal_id=d.id, ticket_id=t.id))
        session.add(Activity(organization_id=p.org_id, account_id=d.account_id, deal_id=d.id, contact_id=t.contact_id, ticket_id=t.id, user_id=p.user_id,
                             kind="ticket_linked", title="Atendimento do WhatsApp vinculado à oportunidade"))
    return {"linked": True}


# ------------------------------------------------------------------ Propostas contratuais
DEFAULT_CLAUSES = [
    {"title": "Objeto", "text": "A CONTRATADA prestará os serviços descritos na presente proposta, conforme escopo e condições abaixo."},
    {"title": "Confidencialidade e LGPD", "text": "As partes comprometem-se a tratar dados pessoais e dados de saúde em conformidade com a Lei nº 13.709/2018 (LGPD), "
     "utilizando-os exclusivamente para as finalidades contratadas e adotando medidas técnicas e administrativas de segurança."},
    {"title": "Responsabilidade técnica", "text": "Laudos e pareceres técnicos têm caráter de apoio diagnóstico e não substituem a avaliação clínica do médico responsável."},
    {"title": "Vigência e reajuste", "text": "Vigência conforme indicado nesta proposta, com reajuste anual pelo IPCA, salvo disposição em contrário."},
]


class ProposalIn(BaseModel):
    valid_days: int = Field(15, ge=1, le=180)
    payment_terms: str | None = "30 dias após emissão da nota fiscal, via PIX ou boleto."
    scope_text: str | None = None
    clauses: list[dict] | None = None


def proposal_out(x: Proposal) -> dict:
    return row(x, ["id", "deal_id", "number", "version", "status", "valid_until", "payment_terms", "scope_text", "clauses", "snapshot", "sent_at", "accepted_at", "created_at"])


@router.post("/deals/{deal_id}/proposals", status_code=201)
async def create_proposal(deal_id: uuid.UUID, body: ProposalIn, p: Principal = Depends(require("deals:write")), session: AsyncSession = Depends(get_session)):
    d = await _deal_or_404(session, p, deal_id)
    if not d.items:
        raise bad_request("Adicione itens à oportunidade antes de gerar a proposta", code="deal_without_items")
    svc.recalc_deal(d)
    year = utcnow().year
    count = (await session.execute(sa.select(sa.func.count()).select_from(Proposal).where(Proposal.organization_id == p.org_id, Proposal.number.like(f"PROP-{year}-%")))).scalar_one()
    version = (await session.execute(sa.select(sa.func.count()).select_from(Proposal).where(Proposal.deal_id == d.id))).scalar_one() + 1
    snapshot = {
        "account": {"name": d.account.corporate_name, "trade_name": d.account.trade_name},
        "items": [{"description": i.description, "sku": i.product.sku, "unit": i.product.unit_label, "quantity": float(i.quantity), "unit_price": float(i.unit_price),
                   "subtotal": float(i.subtotal), "category": i.product.module_category, "modality": i.product.billing_modality.value} for i in d.items],
        "subtotal": float(d.expected_value), "discount_percentage": float(d.discount_percentage or 0), "total": float(d.final_contracted_value or 0),
    }
    x = Proposal(organization_id=p.org_id, deal_id=d.id, number=f"PROP-{year}-{count + 1:04d}", version=version, valid_until=date.today() + timedelta(days=body.valid_days),
                 payment_terms=body.payment_terms, scope_text=body.scope_text, clauses=body.clauses or DEFAULT_CLAUSES, snapshot=snapshot, created_by=p.user_id)
    session.add(x)
    await session.flush()
    return proposal_out(x)


async def _proposal_or_404(session: AsyncSession, p: Principal, pid: uuid.UUID) -> Proposal:
    x = await session.get(Proposal, pid)
    if not x or x.organization_id != p.org_id:
        raise not_found("Proposta")
    await _deal_or_404(session, p, x.deal_id)  # ABAC
    return x


@router.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: uuid.UUID, p: Principal = Depends(require("deals:read")), session: AsyncSession = Depends(get_session)):
    return proposal_out(await _proposal_or_404(session, p, proposal_id))


@router.post("/proposals/{proposal_id}/send")
async def send_proposal(proposal_id: uuid.UUID, p: Principal = Depends(require("deals:write")), session: AsyncSession = Depends(get_session)):
    x = await _proposal_or_404(session, p, proposal_id)
    x.status, x.sent_at = "sent", utcnow()
    d = await session.get(Deal, x.deal_id)
    if d.stage.value in ("lead_inbound", "prospeccao", "qualificacao_tecnica", "reuniao_alinhamento"):
        await svc.move_stage(session, d, "proposta_apresentada", p)
    emit(session, "proposal.sent", p.org_id, {"proposal_id": str(x.id), "deal_id": str(d.id), "number": x.number}, p.user_id)
    return proposal_out(x)


@router.post("/proposals/{proposal_id}/accept")
async def accept_proposal(proposal_id: uuid.UUID, p: Principal = Depends(require("deals:write")), session: AsyncSession = Depends(get_session)):
    x = await _proposal_or_404(session, p, proposal_id)
    x.status, x.accepted_at = "accepted", utcnow()
    return proposal_out(x)


@router.post("/proposals/{proposal_id}/share-link")
async def proposal_share_link(proposal_id: uuid.UUID, request: Request, p: Principal = Depends(require("deals:read")), session: AsyncSession = Depends(get_session)):
    x = await _proposal_or_404(session, p, proposal_id)
    exp = int((utcnow() + timedelta(hours=72)).timestamp())
    sig = sign_media(f"proposal:{x.id}", exp)
    return {"url": f"/p/{x.id}?exp={exp}&sig={sig}"}


@public_router.get("/p/{proposal_id}", response_class=HTMLResponse, include_in_schema=False)
async def public_proposal(proposal_id: uuid.UUID, exp: int, sig: str, session: AsyncSession = Depends(get_session)):
    if not verify_media(f"proposal:{proposal_id}", exp, sig):
        raise AppError(403, "invalid_link", "Link inválido ou expirado")
    x = await session.get(Proposal, proposal_id)
    if not x:
        raise not_found("Proposta")
    org = await session.get(Organization, x.organization_id)
    return HTMLResponse(render_proposal(x, org))
