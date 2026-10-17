"""Pipeline business rules: totals, institutional discount, stage transitions."""
from __future__ import annotations

import uuid
from decimal import ROUND_HALF_UP, Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, bad_request
from app.core.events import emit
from app.db import utcnow
from app.models import Account, Activity, Deal, DealItem, DealStage, DealStageHistory, Organization, ProductService
from app.models.crm import STAGE_PROBABILITY
from app.modules.core.defaults import effective_settings

CENT = Decimal("0.01")


def money(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def deal_subtotal(deal: Deal) -> Decimal:
    return money(sum((money(i.subtotal) for i in deal.items), Decimal("0")))


def discounted(subtotal: Decimal, pct) -> Decimal:
    return money(subtotal * (Decimal("100") - Decimal(str(pct or 0))) / Decimal("100"))


def recalc_deal(deal: Deal) -> None:
    for it in deal.items:
        it.subtotal = money(Decimal(str(it.quantity)) * Decimal(str(it.unit_price)))
    deal.expected_value = deal_subtotal(deal)
    deal.final_contracted_value = discounted(deal.expected_value, deal.discount_percentage)


async def suggest_discount(session: AsyncSession, org_id: uuid.UUID, deal: Deal) -> dict:
    """Automatic institutional discount: the greater of the value-based tier and the discount negotiated on the account."""
    org = await session.get(Organization, org_id)
    cfg = effective_settings(org.settings)["discount"]
    subtotal = float(deal_subtotal(deal))
    tier = 0.0
    for t in sorted(cfg["institutional_tiers"], key=lambda x: x["min_value"]):
        if subtotal >= t["min_value"]:
            tier = float(t["pct"])
    account = await session.get(Account, deal.account_id)
    account_pct = float(account.institutional_discount_pct or 0) if account else 0.0
    pct = max(tier, account_pct)
    return {
        "subtotal": subtotal, "tier_pct": tier, "account_pct": account_pct, "suggested_pct": pct,
        "suggested_final_value": float(discounted(Decimal(str(subtotal)), pct)),
        "auto_approve_max_pct": cfg["auto_approve_max_pct"],
    }


async def auto_approve_limit(session: AsyncSession, org_id: uuid.UUID) -> float:
    org = await session.get(Organization, org_id)
    return float(effective_settings(org.settings)["discount"]["auto_approve_max_pct"])


async def check_discount_permission(session: AsyncSession, principal, pct: float) -> None:
    limit = await auto_approve_limit(session, principal.org_id)
    if pct > limit and not principal.can("deals:discount_high"):
        raise AppError(403, "discount_requires_approval",
                       f"Desconto acima de {limit:.0f}% requer aprovação de um Supervisor Comercial")


async def build_item(session: AsyncSession, org_id: uuid.UUID, product_id: uuid.UUID, quantity, unit_price, allow_override: bool, description: str | None = None) -> DealItem:
    prod = await session.get(ProductService, product_id)
    if not prod or prod.organization_id != org_id or not prod.is_active:
        raise bad_request("Produto/serviço inexistente ou inativo", code="invalid_product")
    price = money(unit_price) if (unit_price is not None and allow_override) else money(prod.base_unit_price)
    qty = Decimal(str(quantity))
    if qty <= 0:
        raise bad_request("Quantidade deve ser maior que zero")
    item = DealItem(product_service_id=prod.id, description=description or prod.name, quantity=qty, unit_price=price, subtotal=money(qty * price))
    item.product = prod  # evita lazy-load no contexto async
    return item


async def move_stage(session: AsyncSession, deal: Deal, new_stage: str, principal, loss_reason: str | None = None, position: float | None = None) -> None:
    try:
        target = DealStage(new_stage)
    except ValueError:
        raise bad_request("Estágio inválido", code="invalid_stage")
    old = deal.stage
    if target == DealStage.fechado_ganho:
        if not deal.items:
            raise bad_request("Adicione ao menos um item antes de fechar o negócio", code="deal_without_items")
        await check_discount_permission(session, principal, float(deal.discount_percentage or 0))
    if target == DealStage.fechado_perdido and not (loss_reason or deal.loss_reason):
        raise bad_request("Informe o motivo da perda", code="loss_reason_required")
    if position is not None:
        deal.position = position
    if old == target:
        return
    deal.stage = target
    deal.probability = STAGE_PROBABILITY[target.value]
    now = utcnow()
    if target in (DealStage.fechado_ganho, DealStage.fechado_perdido):
        deal.closed_at = now
        if target == DealStage.fechado_perdido:
            deal.loss_reason = loss_reason or deal.loss_reason
        else:
            recalc_deal(deal)
    else:
        deal.closed_at = None
    session.add(DealStageHistory(deal_id=deal.id, from_stage=old.value, to_stage=target.value, user_id=principal.user_id))
    session.add(Activity(organization_id=deal.organization_id, account_id=deal.account_id, deal_id=deal.id, user_id=principal.user_id,
                         kind="stage_change", title=f"Estágio: {old.value} → {target.value}", data={"from": old.value, "to": target.value}))
    payload = {"deal_id": str(deal.id), "account_id": str(deal.account_id), "title": deal.title, "from": old.value, "to": target.value,
               "value": float(deal.final_contracted_value or 0)}
    emit(session, "deal.stage_changed", deal.organization_id, payload, principal.user_id)
    if target == DealStage.fechado_ganho:
        emit(session, "deal.won", deal.organization_id, payload, principal.user_id)
    elif target == DealStage.fechado_perdido:
        emit(session, "deal.lost", deal.organization_id, {**payload, "loss_reason": deal.loss_reason}, principal.user_id)
