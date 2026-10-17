"""Event reactions: deal.won → contract + operational cases; SLA monitor for cases/reports."""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.core.events import Event, EventBus, emit
from app.core.scheduler import periodic
from app.db import session_scope, utcnow
from app.models import Activity, Case, CaseReport, CaseStatus, CaseType, Contract, Deal
from app.modules.ops.router import CaseIn, create_case, next_billing, next_number, run_billing_cycle

CATEGORY_TO_CASE_TYPE = {
    "Consultoria de Processos": CaseType.consultoria,
    "Apoio Diagnóstico": CaseType.apoio_diagnostico,
    "Canal Médico": CaseType.canal_medico,
    "Capacitação": CaseType.capacitacao,
}


async def on_deal_won(event: Event) -> None:
    deal_id = uuid.UUID(event.payload["deal_id"])
    async with session_scope() as s:
        deal = await s.get(Deal, deal_id)
        if not deal or (await s.execute(sa.select(Contract.id).where(Contract.deal_id == deal.id))).first():
            return  # idempotente
        await s.refresh(deal, ["items"])
        hours = sum((Decimal(str(i.quantity)) for i in deal.items if i.product.billing_modality.value == "hourly"), Decimal("0"))
        exams = int(sum((Decimal(str(i.quantity)) for i in deal.items if i.product.billing_modality.value == "unit_volume"), Decimal("0")))
        rec_items = [i for i in deal.items if i.product.billing_modality.value == "recurring_subscription"]
        factor = (Decimal("100") - Decimal(str(deal.discount_percentage or 0))) / Decimal("100")
        recurring = sum((Decimal(str(i.unit_price)) for i in rec_items), Decimal("0")) * factor if rec_items else None
        modalities = {i.product.billing_modality.value for i in deal.items}
        modality = "recurring_subscription" if rec_items else ("hourly" if "hourly" in modalities else ("unit_volume" if "unit_volume" in modalities else "fixed_project"))
        start = date.today()
        contract = Contract(
            organization_id=deal.organization_id, account_id=deal.account_id, deal_id=deal.id, number=await next_number(s, Contract, deal.organization_id, "CT"),
            title=deal.title, status="active", modality=modality, start_date=start, end_date=start + timedelta(days=365), total_value=deal.final_contracted_value or 0,
            recurring_amount=recurring.quantize(Decimal("0.01")) if recurring else None, billing_period="monthly", hours_contracted=hours, exams_contracted=exams,
            next_billing_date=next_billing(start, 5, "monthly") if recurring else None,
        )
        s.add(contract)
        await s.flush()
        by_cat: dict[str, list] = {}
        for it in deal.items:
            by_cat.setdefault(it.product.module_category, []).append(it)
        for cat, items in by_cat.items():
            ctype = CATEGORY_TO_CASE_TYPE.get(cat, CaseType.consultoria)
            case_hours = sum((Decimal(str(i.quantity)) for i in items if i.product.billing_modality.value == "hourly"), Decimal("0"))
            await create_case(s, deal.organization_id, CaseIn(account_id=deal.account_id, title=f"{cat} — {deal.title}", case_type=ctype, contract_id=contract.id,
                                                              deal_id=deal.id, owner_user_id=deal.assigned_user_id, hours_contracted=case_hours), deal.assigned_user_id)
        from app.models import Account

        acc = await s.get(Account, deal.account_id)
        if acc and acc.status != "customer":
            acc.status = "customer"
        s.add(Activity(organization_id=deal.organization_id, account_id=deal.account_id, deal_id=deal.id, kind="deal_won", title=f"Negócio ganho! Contrato {contract.number} gerado",
                       data={"contract_id": str(contract.id)}))
        emit(s, "contract.created", deal.organization_id, {"contract_id": str(contract.id), "number": contract.number, "account_id": str(deal.account_id)})


@periodic("ops.sla_monitor", 120)
async def sla_monitor() -> None:
    """Flags cases/reports whose SLA was breached and emits ``sla.breached``."""
    now = utcnow()
    async with session_scope() as s:
        cases = (await s.execute(sa.select(Case).where(Case.sla_due_at < now, Case.sla_breached_at.is_(None), Case.status.notin_([CaseStatus.done, CaseStatus.cancelled])))).scalars().all()
        for c in cases:
            c.sla_breached_at = now
            emit(s, "sla.breached", c.organization_id, {"scope": "case", "case_id": str(c.id), "number": c.number, "priority": c.priority})
        reps = (await s.execute(sa.select(CaseReport).where(CaseReport.due_at < now, CaseReport.sla_breached_at.is_(None), CaseReport.status.notin_(["issued", "rejected"])))).scalars().all()
        for r in reps:
            r.sla_breached_at = now
            emit(s, "sla.breached", r.organization_id, {"scope": "report", "report_id": str(r.id), "case_id": str(r.case_id), "exam_code": r.exam_code, "urgency": r.urgency})


@periodic("billing.cycle", 3600)
async def billing_cycle_task() -> None:
    await run_billing_cycle()


def register(bus: EventBus) -> None:
    bus.subscribe("deal.won", on_deal_won, "ops.on_deal_won")
