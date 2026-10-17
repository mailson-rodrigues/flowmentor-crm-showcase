"""Módulo Operacional (Casos/Projetos, horas, laudos) e Contratos & Faturamento."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import audit
from app.core.deps import Page, Principal, get_principal, pagination, require
from app.core.errors import AppError, bad_request, not_found
from app.core.events import emit
from app.core.serialize import row
from app.db import get_session, utcnow
from app.models import (
    Account, Activity, Case, CaseReport, CaseStatus, CaseTimeEntry, CaseType, Contract, Invoice, Organization, User,
)
from app.modules.core.defaults import effective_settings
from app.modules.ops.pix import NfseProvider, PixGateway
from app.security.sanitize import sanitize_text

router = APIRouter(prefix="/api/v1", tags=["Operacional & Contratos"])

pix_gateway = PixGateway()
nfse_provider = NfseProvider()

CASE_FIELDS = ["id", "number", "title", "description", "case_type", "status", "priority", "account_id", "contract_id", "deal_id", "ticket_id", "owner_user_id",
               "hours_contracted", "hours_consumed", "due_at", "sla_due_at", "sla_breached_at", "closed_at", "tags", "created_at", "updated_at"]


def case_out(c: Case) -> dict:
    contracted, consumed = float(c.hours_contracted or 0), float(c.hours_consumed or 0)
    out = row(c, CASE_FIELDS, account_name=(c.account.trade_name or c.account.corporate_name) if c.account else None)
    out["hours_pct"] = round(consumed / contracted * 100, 1) if contracted else None
    out["hours_over"] = bool(contracted and consumed > contracted)
    return out


async def next_number(session: AsyncSession, model, org_id: uuid.UUID, prefix: str) -> str:
    year = utcnow().year
    n = (await session.execute(sa.select(sa.func.count()).select_from(model).where(model.organization_id == org_id, model.number.like(f"{prefix}-{year}-%")))).scalar_one()
    return f"{prefix}-{year}-{n + 1:04d}"


def _case_scope(p: Principal, stmt):
    stmt = stmt.where(Case.organization_id == p.org_id)
    if not p.can("cases:all") and p.user_id:
        stmt = stmt.where(sa.or_(Case.owner_user_id == p.user_id, Case.owner_user_id.is_(None)))
    return stmt


async def _case_or_404(session: AsyncSession, p: Principal, cid: uuid.UUID) -> Case:
    c = (await session.execute(_case_scope(p, sa.select(Case).where(Case.id == cid)))).scalar_one_or_none()
    if not c:
        raise not_found("Caso")
    return c


# ------------------------------------------------------------------ Casos
class CaseIn(BaseModel):
    account_id: uuid.UUID
    title: str = Field(min_length=2, max_length=200)
    description: str | None = None
    case_type: CaseType = CaseType.consultoria
    priority: str = Field("normal", pattern="^(low|normal|high|urgent)$")
    contract_id: uuid.UUID | None = None
    deal_id: uuid.UUID | None = None
    ticket_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    hours_contracted: Decimal = Decimal("0")
    due_at: datetime | None = None
    sla_hours: int | None = Field(None, ge=1, le=24 * 60)
    tags: list[str] = []


class CasePatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status: CaseStatus | None = None
    priority: str | None = Field(None, pattern="^(low|normal|high|urgent)$")
    owner_user_id: uuid.UUID | None = None
    hours_contracted: Decimal | None = None
    due_at: datetime | None = None
    tags: list[str] | None = None


async def create_case(session: AsyncSession, org_id: uuid.UUID, body: CaseIn, actor_id: uuid.UUID | None) -> Case:
    acc = await session.get(Account, body.account_id)
    if not acc or acc.organization_id != org_id:
        raise not_found("Conta")
    now = utcnow()
    default_sla = {"apoio_diagnostico": 24 if body.priority != "urgent" else 4}.get(body.case_type.value)
    sla_h = body.sla_hours or default_sla
    c = Case(organization_id=org_id, account_id=acc.id, number=await next_number(session, Case, org_id, "CASO"), title=sanitize_text(body.title, 200),
             description=sanitize_text(body.description, 8000), case_type=body.case_type, priority=body.priority, contract_id=body.contract_id,
             deal_id=body.deal_id, ticket_id=body.ticket_id, owner_user_id=body.owner_user_id or actor_id, hours_contracted=body.hours_contracted,
             due_at=body.due_at, sla_due_at=(now + timedelta(hours=sla_h)) if sla_h else None, tags=body.tags)
    session.add(c)
    await session.flush()
    await session.refresh(c, ["account"])
    session.add(Activity(organization_id=org_id, account_id=acc.id, deal_id=c.deal_id, case_id=c.id, user_id=actor_id, kind="case", title=f"Caso {c.number} criado: {c.title}"))
    emit(session, "case.created", org_id, {"case_id": str(c.id), "number": c.number, "account_id": str(acc.id), "type": c.case_type.value, "priority": c.priority}, actor_id)
    return c


@router.get("/cases")
async def list_cases(status: str | None = None, case_type: str | None = None, account_id: uuid.UUID | None = None, owner_user_id: uuid.UUID | None = None,
                     q: str | None = None, page: Page = Depends(pagination), p: Principal = Depends(require("cases:read")), session: AsyncSession = Depends(get_session)):
    stmt = _case_scope(p, sa.select(Case))
    for col, val in ((Case.status, status), (Case.case_type, case_type), (Case.account_id, account_id), (Case.owner_user_id, owner_user_id)):
        if val:
            stmt = stmt.where(col == val)
    if q:
        stmt = stmt.where(sa.or_(sa.func.lower(Case.title).like(f"%{q.lower()}%"), Case.number.like(f"%{q.upper()}%")))
    total = (await session.execute(sa.select(sa.func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.order_by(Case.created_at.desc()).limit(page.limit).offset(page.offset))).scalars().all()
    return {"total": total, "items": [case_out(c) for c in rows]}


@router.post("/cases", status_code=201)
async def post_case(body: CaseIn, p: Principal = Depends(require("cases:write")), session: AsyncSession = Depends(get_session)):
    return case_out(await create_case(session, p.org_id, body, p.user_id))


@router.get("/cases/{case_id}")
async def get_case(case_id: uuid.UUID, p: Principal = Depends(require("cases:read")), session: AsyncSession = Depends(get_session)):
    c = await _case_or_404(session, p, case_id)
    out = case_out(c)
    entries = (await session.execute(sa.select(CaseTimeEntry).where(CaseTimeEntry.case_id == c.id).order_by(CaseTimeEntry.work_date.desc(), CaseTimeEntry.created_at.desc()))).scalars().all()
    out["time_entries"] = [row(e, ["id", "user_id", "work_date", "hours", "description", "billable", "invoice_id"]) for e in entries]
    reps = (await session.execute(sa.select(CaseReport).where(CaseReport.case_id == c.id).order_by(CaseReport.created_at.desc()))).scalars().all()
    out["reports"] = [report_out(r) for r in reps]
    return out


@router.patch("/cases/{case_id}")
async def patch_case(case_id: uuid.UUID, body: CasePatch, request: Request, p: Principal = Depends(require("cases:write")), session: AsyncSession = Depends(get_session)):
    c = await _case_or_404(session, p, case_id)
    old = {"status": c.status.value, "owner_user_id": str(c.owner_user_id)}
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(c, k, sanitize_text(v, 8000) if isinstance(v, str) and k in ("title", "description") else v)
    if c.status in (CaseStatus.done, CaseStatus.cancelled) and not c.closed_at:
        c.closed_at = utcnow()
        emit(session, "case.closed", p.org_id, {"case_id": str(c.id), "number": c.number, "status": c.status.value}, p.user_id)
    elif c.status not in (CaseStatus.done, CaseStatus.cancelled):
        c.closed_at = None
    if "status" in data or "owner_user_id" in data:
        await audit.log_action(session, org_id=p.org_id, action="UPDATE_CASE", resource_type="cases", resource_id=c.id, actor_user_id=p.user_id, request=request,
                               old_state=old, new_state={"status": c.status.value, "owner_user_id": str(c.owner_user_id)})
        emit(session, "case.updated", p.org_id, {"case_id": str(c.id), "number": c.number, "status": c.status.value}, p.user_id)
    return case_out(c)


class TimeEntryIn(BaseModel):
    hours: Decimal = Field(gt=0, le=24)
    work_date: date | None = None
    description: str | None = Field(None, max_length=300)
    billable: bool = True


@router.post("/cases/{case_id}/time-entries", status_code=201)
async def add_time_entry(case_id: uuid.UUID, body: TimeEntryIn, p: Principal = Depends(require("cases:write")), session: AsyncSession = Depends(get_session)):
    c = await _case_or_404(session, p, case_id)
    before = float(c.hours_consumed or 0)
    e = CaseTimeEntry(case_id=c.id, organization_id=p.org_id, user_id=p.user_id, work_date=body.work_date or date.today(), hours=body.hours,
                      description=sanitize_text(body.description, 300), billable=body.billable)
    session.add(e)
    c.hours_consumed = Decimal(str(c.hours_consumed or 0)) + body.hours
    contracted = float(c.hours_contracted or 0)
    if contracted:
        pct_before, pct_after = before / contracted * 100, float(c.hours_consumed) / contracted * 100
        for threshold in (80, 100):
            if pct_before < threshold <= pct_after:
                emit(session, "case.hours_threshold", p.org_id, {"case_id": str(c.id), "number": c.number, "threshold_pct": threshold,
                                                               "consumed": float(c.hours_consumed), "contracted": contracted}, p.user_id)
    await session.flush()
    return row(e, ["id", "work_date", "hours", "description", "billable"], hours_consumed=c.hours_consumed)


# ------------------------------------------------------------------ Laudos
class ReportIn(BaseModel):
    exam_code: str = Field(min_length=1, max_length=60)
    panel: str | None = None
    urgency: str = Field("routine", pattern="^(routine|urgent)$")
    due_hours: int | None = Field(None, ge=1)


class ReportPatch(BaseModel):
    status: str | None = Field(None, pattern="^(received|processing|in_review|issued|rejected)$")
    analyst_user_id: uuid.UUID | None = None
    conclusion: str | None = None


def report_out(r: CaseReport) -> dict:
    return row(r, ["id", "case_id", "exam_code", "panel", "urgency", "status", "analyst_user_id", "due_at", "issued_at", "sla_breached_at", "conclusion", "created_at"])


@router.post("/cases/{case_id}/reports", status_code=201)
async def add_report(case_id: uuid.UUID, body: ReportIn, p: Principal = Depends(require("cases:write")), session: AsyncSession = Depends(get_session)):
    c = await _case_or_404(session, p, case_id)
    hours = body.due_hours or (4 if body.urgency == "urgent" else 24)
    r = CaseReport(organization_id=p.org_id, case_id=c.id, exam_code=body.exam_code, panel=body.panel, urgency=body.urgency, analyst_user_id=p.user_id,
                   due_at=utcnow() + timedelta(hours=hours))
    session.add(r)
    await session.flush()
    emit(session, "report.received", p.org_id, {"report_id": str(r.id), "case_id": str(c.id), "exam_code": r.exam_code, "urgency": r.urgency}, p.user_id)
    return report_out(r)


@router.patch("/reports/{report_id}")
async def patch_report(report_id: uuid.UUID, body: ReportPatch, request: Request, p: Principal = Depends(require("cases:write")), session: AsyncSession = Depends(get_session)):
    r = await session.get(CaseReport, report_id)
    if not r or r.organization_id != p.org_id:
        raise not_found("Laudo")
    await _case_or_404(session, p, r.case_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("status") == "issued" and r.status != "issued":
        if not (data.get("conclusion") or r.conclusion):
            raise bad_request("Informe a conclusão do laudo para emiti-lo", code="conclusion_required")
        r.issued_at = utcnow()
        emit(session, "report.issued", p.org_id, {"report_id": str(r.id), "case_id": str(r.case_id), "exam_code": r.exam_code}, p.user_id)
        await audit.log_action(session, org_id=p.org_id, action="ISSUE_REPORT", resource_type="case_reports", resource_id=r.id, actor_user_id=p.user_id, request=request)
    for k, v in data.items():
        setattr(r, k, sanitize_text(v, 20000) if k == "conclusion" and v else v)
    return report_out(r)


@router.get("/reports")
async def list_reports(status: str | None = None, pending: bool = False, p: Principal = Depends(require("cases:read")), session: AsyncSession = Depends(get_session)):
    stmt = sa.select(CaseReport, Case).join(Case, Case.id == CaseReport.case_id).where(CaseReport.organization_id == p.org_id)
    if not p.can("cases:all") and p.user_id:
        stmt = stmt.where(sa.or_(Case.owner_user_id == p.user_id, CaseReport.analyst_user_id == p.user_id))
    if pending:
        stmt = stmt.where(CaseReport.status.notin_(["issued", "rejected"]))
    elif status:
        stmt = stmt.where(CaseReport.status == status)
    rows = (await session.execute(stmt.order_by(CaseReport.urgency.desc(), CaseReport.due_at.asc()).limit(300))).all()
    return {"items": [{**report_out(r), "case_number": c.number, "case_title": c.title, "account_id": str(c.account_id)} for r, c in rows]}


# ------------------------------------------------------------------ Contratos
class ContractIn(BaseModel):
    account_id: uuid.UUID
    deal_id: uuid.UUID | None = None
    title: str = Field(min_length=2, max_length=200)
    modality: str = Field("fixed_project", pattern="^(hourly|unit_volume|recurring_subscription|fixed_project)$")
    start_date: date
    end_date: date | None = None
    total_value: Decimal = Decimal("0")
    recurring_amount: Decimal | None = None
    billing_day: int = Field(5, ge=1, le=28)
    billing_period: str = Field("monthly", pattern="^(monthly|yearly|once)$")
    hours_contracted: Decimal = Decimal("0")
    hourly_rate: Decimal = Decimal("300")
    exams_contracted: int = 0
    auto_renew: bool = False
    terms: str | None = None


CONTRACT_FIELDS = ["id", "number", "title", "status", "modality", "account_id", "deal_id", "start_date", "end_date", "total_value", "recurring_amount", "billing_day",
                   "billing_period", "next_billing_date", "hours_contracted", "hourly_rate", "exams_contracted", "auto_renew", "terms", "created_at"]


def next_billing(start: date, day: int, period: str, after: date | None = None) -> date | None:
    if period == "once":
        return None if after else start
    ref = after or start
    if period == "monthly":
        if after is None:
            cand = date(ref.year, ref.month, min(day, 28))
            if cand >= ref:
                return cand
        return date(ref.year + (ref.month // 12), ref.month % 12 + 1, min(day, 28))
    return date(ref.year + (1 if after else 0), ref.month, min(ref.day, 28))


async def hours_summary(session: AsyncSession, contract: Contract) -> dict:
    consumed = (await session.execute(sa.select(sa.func.coalesce(sa.func.sum(Case.hours_consumed), 0)).where(Case.contract_id == contract.id))).scalar_one()
    contracted = float(contract.hours_contracted or 0)
    consumed = float(consumed or 0)
    return {"contracted": contracted, "consumed": consumed, "remaining": max(contracted - consumed, 0), "pct": round(consumed / contracted * 100, 1) if contracted else None,
            "overage": max(consumed - contracted, 0) if contracted else 0}


def contract_out(c: Contract) -> dict:
    return row(c, CONTRACT_FIELDS, account_name=(c.account.trade_name or c.account.corporate_name) if c.account else None)


@router.get("/contracts")
async def list_contracts(status: str | None = None, account_id: uuid.UUID | None = None, p: Principal = Depends(require("contracts:read")), session: AsyncSession = Depends(get_session)):
    stmt = sa.select(Contract).where(Contract.organization_id == p.org_id)
    if status:
        stmt = stmt.where(Contract.status == status)
    if account_id:
        stmt = stmt.where(Contract.account_id == account_id)
    rows = (await session.execute(stmt.order_by(Contract.created_at.desc()).limit(300))).scalars().all()
    items = []
    for c in rows:
        o = contract_out(c)
        o["hours"] = await hours_summary(session, c)
        items.append(o)
    return {"items": items}


@router.post("/contracts", status_code=201)
async def create_contract(body: ContractIn, request: Request, p: Principal = Depends(require("contracts:write")), session: AsyncSession = Depends(get_session)):
    acc = await session.get(Account, body.account_id)
    if not acc or acc.organization_id != p.org_id:
        raise not_found("Conta")
    c = Contract(organization_id=p.org_id, number=await next_number(session, Contract, p.org_id, "CT"), status="active", **body.model_dump())
    c.next_billing_date = next_billing(body.start_date, body.billing_day, body.billing_period) if body.recurring_amount else None
    session.add(c)
    await session.flush()
    await session.refresh(c, ["account"])
    await audit.log_action(session, org_id=p.org_id, action="CREATE_CONTRACT", resource_type="contracts", resource_id=c.id, actor_user_id=p.user_id, request=request, new_state={"number": c.number})
    emit(session, "contract.created", p.org_id, {"contract_id": str(c.id), "number": c.number, "account_id": str(acc.id)}, p.user_id)
    return contract_out(c)


@router.get("/contracts/{contract_id}")
async def get_contract(contract_id: uuid.UUID, p: Principal = Depends(require("contracts:read")), session: AsyncSession = Depends(get_session)):
    c = await session.get(Contract, contract_id)
    if not c or c.organization_id != p.org_id:
        raise not_found("Contrato")
    out = contract_out(c)
    out["hours"] = await hours_summary(session, c)
    invs = (await session.execute(sa.select(Invoice).where(Invoice.contract_id == c.id).order_by(Invoice.due_date.desc()))).scalars().all()
    out["invoices"] = [invoice_out(i) for i in invs]
    cases = (await session.execute(sa.select(Case).where(Case.contract_id == c.id))).scalars().all()
    out["cases"] = [case_out(x) for x in cases]
    return out


class ContractStatusIn(BaseModel):
    status: str = Field(pattern="^(draft|active|suspended|ended|cancelled)$")


@router.put("/contracts/{contract_id}/status")
async def contract_status(contract_id: uuid.UUID, body: ContractStatusIn, request: Request, p: Principal = Depends(require("contracts:write")), session: AsyncSession = Depends(get_session)):
    c = await session.get(Contract, contract_id)
    if not c or c.organization_id != p.org_id:
        raise not_found("Contrato")
    old = c.status
    c.status = body.status
    await audit.log_action(session, org_id=p.org_id, action="UPDATE_CONTRACT", resource_type="contracts", resource_id=c.id, actor_user_id=p.user_id, request=request,
                           old_state={"status": old}, new_state={"status": c.status})
    emit(session, "contract.status_changed", p.org_id, {"contract_id": str(c.id), "number": c.number, "from": old, "to": c.status}, p.user_id)
    return contract_out(c)


# ------------------------------------------------------------------ Faturas
INVOICE_FIELDS = ["id", "number", "contract_id", "account_id", "description", "reference_period", "amount", "due_date", "status", "paid_at", "payment_method",
                  "pix_payload", "nfse_number", "nfse_status", "created_at"]


def invoice_out(i: Invoice) -> dict:
    return row(i, INVOICE_FIELDS, account_name=(i.account.trade_name or i.account.corporate_name) if i.account else None)


class InvoiceIn(BaseModel):
    contract_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    description: str = Field(min_length=2, max_length=250)
    amount: Decimal = Field(gt=0)
    due_date: date
    reference_period: str | None = None


async def create_invoice(session: AsyncSession, org_id: uuid.UUID, *, account_id: uuid.UUID, amount: Decimal, due_date: date, description: str,
                         contract_id: uuid.UUID | None = None, reference_period: str | None = None, actor_id: uuid.UUID | None = None) -> Invoice:
    org = await session.get(Organization, org_id)
    inv = Invoice(organization_id=org_id, contract_id=contract_id, account_id=account_id, number=await next_number(session, Invoice, org_id, "FAT"), description=description,
                  amount=amount, due_date=due_date, reference_period=reference_period)
    session.add(inv)
    await session.flush()
    charge = await pix_gateway.create_charge(invoice_number=inv.number, amount=amount, settings=effective_settings(org.settings))
    inv.pix_payload, inv.gateway_ref = charge.get("payload"), charge.get("gateway_ref")
    inv.nfse_status = (await nfse_provider.request(inv))["status"]
    await session.refresh(inv, ["account"])
    emit(session, "invoice.created", org_id, {"invoice_id": str(inv.id), "number": inv.number, "amount": float(amount), "account_id": str(account_id), "due_date": due_date.isoformat()}, actor_id)
    return inv


@router.get("/invoices")
async def list_invoices(status: str | None = None, account_id: uuid.UUID | None = None, p: Principal = Depends(require("contracts:read")), session: AsyncSession = Depends(get_session)):
    stmt = sa.select(Invoice).where(Invoice.organization_id == p.org_id)
    if status:
        stmt = stmt.where(Invoice.status == status)
    if account_id:
        stmt = stmt.where(Invoice.account_id == account_id)
    rows = (await session.execute(stmt.order_by(Invoice.due_date.desc()).limit(300))).scalars().all()
    totals = {"open": 0.0, "overdue": 0.0, "paid": 0.0}
    for i in rows:
        if i.status in totals:
            totals[i.status] += float(i.amount)
    return {"items": [invoice_out(i) for i in rows], "totals": totals}


@router.post("/invoices", status_code=201)
async def post_invoice(body: InvoiceIn, p: Principal = Depends(require("contracts:write")), session: AsyncSession = Depends(get_session)):
    acc_id = body.account_id
    if body.contract_id:
        ct = await session.get(Contract, body.contract_id)
        if not ct or ct.organization_id != p.org_id:
            raise not_found("Contrato")
        acc_id = ct.account_id
    if not acc_id:
        raise bad_request("Informe contract_id ou account_id")
    return invoice_out(await create_invoice(session, p.org_id, account_id=acc_id, amount=body.amount, due_date=body.due_date, description=body.description,
                                             contract_id=body.contract_id, reference_period=body.reference_period, actor_id=p.user_id))


class PayIn(BaseModel):
    payment_method: str = Field("pix", pattern="^(pix|boleto|transfer)$")


@router.post("/invoices/{invoice_id}/pay")
async def pay_invoice(invoice_id: uuid.UUID, body: PayIn, request: Request, p: Principal = Depends(require("contracts:write")), session: AsyncSession = Depends(get_session)):
    inv = await session.get(Invoice, invoice_id)
    if not inv or inv.organization_id != p.org_id:
        raise not_found("Fatura")
    if inv.status == "paid":
        raise bad_request("Fatura já paga", code="already_paid")
    inv.status, inv.paid_at, inv.payment_method = "paid", utcnow(), body.payment_method
    await audit.log_action(session, org_id=p.org_id, action="PAY_INVOICE", resource_type="invoices", resource_id=inv.id, actor_user_id=p.user_id, request=request,
                           new_state={"amount": float(inv.amount), "method": body.payment_method})
    emit(session, "invoice.paid", p.org_id, {"invoice_id": str(inv.id), "number": inv.number, "amount": float(inv.amount)}, p.user_id)
    return invoice_out(inv)


@router.post("/contracts/{contract_id}/invoice-from-usage", status_code=201)
async def invoice_from_usage(contract_id: uuid.UUID, p: Principal = Depends(require("contracts:write")), session: AsyncSession = Depends(get_session)):
    """Fatura as horas técnicas faturáveis ainda não cobradas (hora técnica ref. R$ 300,00)."""
    c = await session.get(Contract, contract_id)
    if not c or c.organization_id != p.org_id:
        raise not_found("Contrato")
    entries = (await session.execute(sa.select(CaseTimeEntry).join(Case, Case.id == CaseTimeEntry.case_id).where(
        Case.contract_id == c.id, CaseTimeEntry.billable.is_(True), CaseTimeEntry.invoice_id.is_(None)))).scalars().all()
    hours = sum((Decimal(str(e.hours)) for e in entries), Decimal("0"))
    if hours <= 0:
        raise bad_request("Não há horas faturáveis pendentes", code="nothing_to_invoice")
    billable_hours = hours
    if c.modality == "hourly" or not c.hours_contracted:
        pass
    else:  # banco de horas: só excedente
        used_before = (await session.execute(sa.select(sa.func.coalesce(sa.func.sum(CaseTimeEntry.hours), 0)).join(Case, Case.id == CaseTimeEntry.case_id).where(
            Case.contract_id == c.id, CaseTimeEntry.invoice_id.is_not(None)))).scalar_one()
        free_left = max(Decimal(str(c.hours_contracted)) - Decimal(str(used_before)), Decimal("0"))
        billable_hours = max(hours - free_left, Decimal("0"))
    rate = Decimal(str(c.hourly_rate or settings.consulting_hourly_rate_ref))
    amount = (billable_hours * rate).quantize(Decimal("0.01"))
    if amount <= 0:
        for e in entries:
            e.invoice_id = c.id  # marca como consumidas dentro do banco de horas
        return {"message": "Horas dentro do banco de horas contratado; nada a faturar.", "hours": float(hours)}
    inv = await create_invoice(session, p.org_id, account_id=c.account_id, contract_id=c.id, amount=amount, due_date=date.today() + timedelta(days=30),
                               description=f"Horas técnicas ({billable_hours}h x R$ {rate})", reference_period=date.today().strftime("%Y-%m"), actor_id=p.user_id)
    for e in entries:
        e.invoice_id = inv.id
    return invoice_out(inv)


async def run_billing_cycle() -> dict:
    """Tarefa diária: gera faturas recorrentes vencidas e marca atrasadas."""
    from app.db import session_scope

    generated = overdue = 0
    today = date.today()
    async with session_scope() as s:
        due = (await s.execute(sa.select(Contract).where(Contract.status == "active", Contract.recurring_amount.is_not(None), Contract.next_billing_date <= today))).scalars().all()
        for c in due:
            period = c.next_billing_date.strftime("%Y-%m")
            await create_invoice(s, c.organization_id, account_id=c.account_id, contract_id=c.id, amount=c.recurring_amount, description=f"{c.title} — {period}",
                                 due_date=c.next_billing_date + timedelta(days=10), reference_period=period)
            c.next_billing_date = next_billing(c.start_date, c.billing_day, c.billing_period, after=c.next_billing_date) if c.billing_period != "once" else None
            if c.end_date and c.next_billing_date and c.next_billing_date > c.end_date and not c.auto_renew:
                c.next_billing_date = None
            generated += 1
        res = await s.execute(sa.update(Invoice).where(Invoice.status == "open", Invoice.due_date < today).values(status="overdue"))
        overdue = res.rowcount or 0
    return {"generated": generated, "marked_overdue": overdue}


@router.post("/billing/run-cycle")
async def trigger_billing_cycle(p: Principal = Depends(require("contracts:write"))):
    return await run_billing_cycle()
