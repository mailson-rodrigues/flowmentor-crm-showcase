"""Executive dashboard: sales, operations, contracts/billing and customer service (consolidated view)."""
from __future__ import annotations

from datetime import date, timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, get_principal
from app.db import get_session, utcnow
from app.models import Case, CaseReport, CaseStatus, Contract, Deal, DealStage, Invoice, SatisfactionSurvey, Ticket, TicketStatus, User
from app.models.crm import STAGE_PROBABILITY

router = APIRouter(prefix="/api/v1/dashboard", tags=["Dashboard"])


@router.get("")
async def dashboard(p: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)):
    org, now = p.org_id, utcnow()
    out: dict = {"generated_at": now.isoformat()}

    if p.can("deals:read"):
        dq = sa.select(Deal).where(Deal.organization_id == org)
        if not p.can("deals:all") and p.user_id:
            dq = dq.where(sa.or_(Deal.assigned_user_id == p.user_id, Deal.assigned_user_id.is_(None)))
        deals = (await session.execute(dq)).scalars().all()
        open_deals = [d for d in deals if not d.stage.value.startswith("fechado")]
        won = [d for d in deals if d.stage == DealStage.fechado_ganho]
        lost = [d for d in deals if d.stage == DealStage.fechado_perdido]
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        val = lambda d: float(d.final_contracted_value or d.expected_value or 0)  # noqa: E731
        by_stage = {s.value: {"count": 0, "value": 0.0} for s in DealStage}
        for d in deals:
            by_stage[d.stage.value]["count"] += 1
            by_stage[d.stage.value]["value"] += val(d)
        loss_reasons: dict[str, int] = {}
        for d in lost:
            k = (d.loss_reason or "Não informado")[:60]
            loss_reasons[k] = loss_reasons.get(k, 0) + 1
        out["commercial"] = {
            "open_deals": len(open_deals), "pipeline_value": sum(val(d) for d in open_deals),
            "weighted_forecast": sum(val(d) * STAGE_PROBABILITY[d.stage.value] / 100 for d in open_deals),
            "won_month_value": sum(val(d) for d in won if d.closed_at and d.closed_at >= month_start), "won_month_count": sum(1 for d in won if d.closed_at and d.closed_at >= month_start),
            "win_rate": round(100 * len(won) / (len(won) + len(lost)), 1) if (won or lost) else None,
            "avg_ticket": round(sum(val(d) for d in won) / len(won), 2) if won else None,
            "new_leads_30d": sum(1 for d in deals if d.created_at >= now - timedelta(days=30)),
            "by_stage": by_stage, "loss_reasons": loss_reasons,
        }

    if p.can("cases:read"):
        cq = sa.select(Case).where(Case.organization_id == org)
        if not p.can("cases:all") and p.user_id:
            cq = cq.where(sa.or_(Case.owner_user_id == p.user_id, Case.owner_user_id.is_(None)))
        cases = (await session.execute(cq)).scalars().all()
        active = [c for c in cases if c.status not in (CaseStatus.done, CaseStatus.cancelled)]
        pending_reports = (await session.execute(sa.select(CaseReport).where(CaseReport.organization_id == org, CaseReport.status.notin_(["issued", "rejected"])))).scalars().all()
        out["operations"] = {
            "open_cases": len(active), "breached_cases": sum(1 for c in active if c.sla_breached_at), "urgent_cases": sum(1 for c in active if c.priority == "urgent"),
            "pending_reports": len(pending_reports), "urgent_reports": sum(1 for r in pending_reports if r.urgency == "urgent"),
            "overdue_reports": sum(1 for r in pending_reports if r.due_at and r.due_at < now),
            "hours_contracted": float(sum(c.hours_contracted or 0 for c in active)), "hours_consumed": float(sum(c.hours_consumed or 0 for c in active)),
            "by_type": {t: sum(1 for c in active if c.case_type.value == t) for t in ("consultoria", "apoio_diagnostico", "canal_medico", "capacitacao")},
        }

    if p.can("contracts:read"):
        contracts = (await session.execute(sa.select(Contract).where(Contract.organization_id == org, Contract.status == "active"))).scalars().all()
        invoices = (await session.execute(sa.select(Invoice).where(Invoice.organization_id == org))).scalars().all()
        out["finance"] = {
            "active_contracts": len(contracts), "mrr": float(sum(c.recurring_amount or 0 for c in contracts if c.billing_period == "monthly")),
            "open_amount": float(sum(i.amount for i in invoices if i.status == "open")), "overdue_amount": float(sum(i.amount for i in invoices if i.status == "overdue")),
            "paid_month": float(sum(i.amount for i in invoices if i.status == "paid" and i.paid_at and i.paid_at.month == now.month and i.paid_at.year == now.year)),
            "expiring_30d": sum(1 for c in contracts if c.end_date and c.end_date <= date.today() + timedelta(days=30)),
        }

    if p.can("inbox:use"):
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tq = sa.select(Ticket).where(Ticket.organization_id == org, Ticket.created_at >= today - timedelta(days=7))
        tickets = (await session.execute(tq)).scalars().all()
        todays = [t for t in tickets if t.created_at >= today]
        frt = [t.first_response_time_seconds for t in todays if t.first_response_time_seconds is not None]
        csat = (await session.execute(sa.select(sa.func.avg(SatisfactionSurvey.rating)).where(SatisfactionSurvey.organization_id == org, SatisfactionSurvey.survey_type == "csat",
                                                                                              SatisfactionSurvey.answered_at >= today - timedelta(days=7)))).scalar_one()
        online = (await session.execute(sa.select(sa.func.count()).select_from(User).where(User.organization_id == org, User.status.in_(["available", "busy"])))).scalar_one()
        out["support"] = {
            "queue": sum(1 for t in tickets if t.status == TicketStatus.queued and not t.assigned_agent_id), "in_progress": sum(1 for t in tickets if t.status == TicketStatus.in_progress),
            "bot": sum(1 for t in tickets if t.status == TicketStatus.pending_bot), "created_today": len(todays), "closed_today": sum(1 for t in tickets if t.closed_at and t.closed_at >= today),
            "avg_frt_today": round(sum(frt) / len(frt)) if frt else None, "csat_7d": round(float(csat), 2) if csat else None, "agents_online": online,
            "daily": [{"day": (today - timedelta(days=i)).strftime("%d/%m"), "tickets": sum(1 for t in tickets if (today - timedelta(days=i)) <= t.created_at < (today - timedelta(days=i - 1)))} for i in range(6, -1, -1)],
        }
    return out
