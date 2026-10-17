"""LGPD: consentimentos, direitos do titular (arts. 18 e 19), anonimização/exclusão, trilha de auditoria."""
from __future__ import annotations

import csv
import hashlib
import io
import uuid
from datetime import date, datetime, timedelta, time as dtime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.deps import Page, Principal, pagination, require
from app.core.errors import AppError, bad_request, conflict, not_found
from app.core.events import emit
from app.core.scheduler import periodic
from app.core.serialize import contact_out, plain, row
from app.core.storage import get_storage
from app.db import get_session, session_scope, utcnow
from app.models import (
    AccountContact, Activity, AuditLog, Contact, DataSubjectRequest, LgpdConsent, MediaAsset, Message, SatisfactionSurvey, Ticket, TicketEvent, TicketNote, User,
)
from app.modules.whatsapp.media import purge_expired_media
from app.security.masking import scrub_text

router = APIRouter(prefix="/api/v1/lgpd", tags=["LGPD & Auditoria"])

REQUEST_TYPES = {
    "confirmation": "Confirmação da existência de tratamento (art. 18, I)", "access": "Acesso aos dados (art. 18, II)", "rectification": "Correção de dados (art. 18, III)",
    "anonymization": "Anonimização/bloqueio (art. 18, IV)", "deletion": "Eliminação dos dados (art. 18, VI)", "portability": "Portabilidade (art. 18, V)",
    "info_sharing": "Informação sobre compartilhamento (art. 18, VII)", "revoke_consent": "Revogação do consentimento (art. 18, IX)",
}


# ------------------------------------------------------------------ Serviços
async def export_subject_data(session: AsyncSession, org_id: uuid.UUID, contact: Contact, max_messages: int = 5000) -> dict:
    tickets = (await session.execute(sa.select(Ticket).where(Ticket.organization_id == org_id, Ticket.contact_id == contact.id).order_by(Ticket.created_at))).scalars().all()
    tids = [t.id for t in tickets]
    msgs = (await session.execute(sa.select(Message).where(Message.ticket_id.in_(tids)).order_by(Message.created_at).limit(max_messages))).scalars().all() if tids else []
    consents = (await session.execute(sa.select(LgpdConsent).where(LgpdConsent.contact_id == contact.id))).scalars().all()
    surveys = (await session.execute(sa.select(SatisfactionSurvey).where(SatisfactionSurvey.contact_id == contact.id))).scalars().all()
    return {
        "generated_at": utcnow().isoformat(),
        "contact": row(contact, ["id", "wa_id", "name", "email", "job_title", "tags", "notes", "source", "created_at", "opted_out_at", "anonymized_at"]),
        "consents": [row(c, ["legal_basis", "purpose", "opt_in_timestamp", "revoked_at", "consent_source"]) for c in consents],
        "tickets": [row(t, ["id", "status", "subject", "summary", "created_at", "closed_at"]) for t in tickets],
        "messages": [row(m, ["ticket_id", "direction", "type", "body", "created_at"]) for m in msgs],
        "surveys": [row(s, ["survey_type", "rating", "comment", "sentiment", "answered_at"]) for s in surveys],
    }


async def anonymize_contact(session: AsyncSession, org_id: uuid.UUID, contact: Contact) -> dict:
    """Redação irreversível: preserva métricas/estatísticas, remove qualquer dado que identifique o titular."""
    if contact.anonymized_at:
        return {"already": True}
    tids = list((await session.execute(sa.select(Ticket.id).where(Ticket.organization_id == org_id, Ticket.contact_id == contact.id))).scalars().all())
    stats = {"tickets": len(tids), "messages": 0, "media_deleted": 0}
    if tids:
        res = await session.execute(sa.update(Message).where(Message.ticket_id.in_(tids)).values(body="[conteúdo removido — LGPD]", payload=None, media_url=None))
        stats["messages"] = res.rowcount or 0
        assets = (await session.execute(sa.select(MediaAsset).where(MediaAsset.ticket_id.in_(tids), MediaAsset.deleted_at.is_(None)))).scalars().all()
        for a in assets:
            try:
                await get_storage().delete(a.storage_key)
            except Exception:  # noqa: BLE001
                pass
            a.deleted_at = utcnow()
            stats["media_deleted"] += 1
        await session.execute(sa.update(TicketNote).where(TicketNote.ticket_id.in_(tids)).values(body="[removido — LGPD]"))
        await session.execute(sa.update(Ticket).where(Ticket.id.in_(tids)).values(summary=None, subject=None, last_message_preview="[removido — LGPD]"))
    await session.execute(sa.update(SatisfactionSurvey).where(SatisfactionSurvey.contact_id == contact.id).values(comment=None))
    await session.execute(sa.update(Activity).where(Activity.contact_id == contact.id).values(body=None))
    digest = hashlib.sha256(f"{contact.id}|{contact.wa_id}".encode()).hexdigest()[:16]
    contact.wa_id, contact.name, contact.email, contact.job_title, contact.notes, contact.tags = f"anon-{digest}", "Titular anonimizado", None, None, None, []
    contact.anonymized_at, contact.opted_out_at = utcnow(), utcnow()
    await session.execute(sa.update(LgpdConsent).where(LgpdConsent.contact_id == contact.id, LgpdConsent.revoked_at.is_(None)).values(revoked_at=utcnow()))
    emit(session, "lgpd.anonymized", org_id, {"contact_id": str(contact.id)})
    return stats


async def delete_conversations(session: AsyncSession, org_id: uuid.UUID, contact: Contact) -> dict:
    """Direito ao esquecimento: elimina todo o histórico de conversas do titular (a trilha de auditoria permanece)."""
    tids = list((await session.execute(sa.select(Ticket.id).where(Ticket.organization_id == org_id, Ticket.contact_id == contact.id))).scalars().all())
    deleted = {"tickets": len(tids), "messages": 0}
    if tids:
        for a in (await session.execute(sa.select(MediaAsset).where(MediaAsset.ticket_id.in_(tids)))).scalars().all():
            try:
                await get_storage().delete(a.storage_key)
            except Exception:  # noqa: BLE001
                pass
            await session.delete(a)
        deleted["messages"] = (await session.execute(sa.delete(Message).where(Message.ticket_id.in_(tids)))).rowcount or 0
        await session.execute(sa.delete(SatisfactionSurvey).where(SatisfactionSurvey.ticket_id.in_(tids)))
        await session.execute(sa.delete(Activity).where(sa.or_(Activity.ticket_id.in_(tids), Activity.contact_id == contact.id)))
        await session.execute(sa.delete(Ticket).where(Ticket.id.in_(tids)))  # cascata: eventos, notas
    await anonymize_contact(session, org_id, contact)
    await session.execute(sa.delete(AccountContact).where(AccountContact.contact_id == contact.id))
    return deleted


# ------------------------------------------------------------------ Consentimentos
class ConsentIn(BaseModel):
    contact_id: uuid.UUID
    legal_basis: str = Field(max_length=80)
    purpose: str
    consent_source: str = Field("formulario_web", max_length=50)


@router.get("/consents")
async def list_consents(contact_id: uuid.UUID | None = None, p: Principal = Depends(require("lgpd:manage")), session: AsyncSession = Depends(get_session)):
    q = sa.select(LgpdConsent).where(LgpdConsent.organization_id == p.org_id)
    if contact_id:
        q = q.where(LgpdConsent.contact_id == contact_id)
    rows = (await session.execute(q.order_by(LgpdConsent.opt_in_timestamp.desc()).limit(300))).scalars().all()
    return {"items": [row(c, ["id", "contact_id", "legal_basis", "purpose", "opt_in_timestamp", "revoked_at", "consent_source", "ip_address"]) for c in rows]}


@router.post("/consents", status_code=201)
async def create_consent(body: ConsentIn, request: Request, p: Principal = Depends(require("lgpd:manage")), session: AsyncSession = Depends(get_session)):
    c = await session.get(Contact, body.contact_id)
    if not c or c.organization_id != p.org_id:
        raise not_found("Contato")
    x = LgpdConsent(organization_id=p.org_id, contact_id=c.id, legal_basis=body.legal_basis, purpose=body.purpose, consent_source=body.consent_source, ip_address=audit.client_ip(request))
    session.add(x)
    await session.flush()
    await audit.log_action(session, org_id=p.org_id, action="CREATE_CONSENT", resource_type="lgpd_consents", resource_id=x.id, actor_user_id=p.user_id, request=request)
    return row(x, ["id", "contact_id", "legal_basis", "purpose", "opt_in_timestamp", "consent_source"])


@router.post("/consents/{consent_id}/revoke")
async def revoke_consent(consent_id: uuid.UUID, request: Request, p: Principal = Depends(require("lgpd:manage")), session: AsyncSession = Depends(get_session)):
    x = await session.get(LgpdConsent, consent_id)
    if not x or x.organization_id != p.org_id:
        raise not_found("Consentimento")
    x.revoked_at = x.revoked_at or utcnow()
    await audit.log_action(session, org_id=p.org_id, action="REVOKE_CONSENT", resource_type="lgpd_consents", resource_id=x.id, actor_user_id=p.user_id, request=request)
    return {"revoked_at": plain(x.revoked_at)}


# ------------------------------------------------------------------ Direitos do titular
class DsrIn(BaseModel):
    contact_id: uuid.UUID | None = None
    wa_id: str | None = None
    request_type: str
    requester_name: str | None = None
    requester_contact: str | None = None
    details: str | None = None
    changes: dict | None = None  # retificação: {"name": "...", "email": "..."}


def dsr_out(r: DataSubjectRequest) -> dict:
    out = row(r, ["id", "contact_id", "request_type", "status", "requester_name", "requester_contact", "details", "result", "due_at", "handled_by", "created_at", "completed_at"])
    out["type_label"] = REQUEST_TYPES.get(r.request_type)
    out["overdue"] = bool(r.status in ("open", "in_progress") and r.due_at and r.due_at < utcnow())
    return out


@router.get("/request-types")
async def request_types(p: Principal = Depends(require("lgpd:manage"))):
    return {"items": [{"type": k, "label": v} for k, v in REQUEST_TYPES.items()]}


@router.post("/requests", status_code=201)
async def create_request(body: DsrIn, request: Request, p: Principal = Depends(require("lgpd:manage")), session: AsyncSession = Depends(get_session)):
    if body.request_type not in REQUEST_TYPES:
        raise bad_request("Tipo de solicitação inválido")
    contact = None
    if body.contact_id:
        contact = await session.get(Contact, body.contact_id)
    elif body.wa_id:
        from app.modules.core.router import normalize_wa_id

        contact = (await session.execute(sa.select(Contact).where(Contact.organization_id == p.org_id, Contact.wa_id == normalize_wa_id(body.wa_id)))).scalar_one_or_none()
    if contact is None or contact.organization_id != p.org_id:
        raise not_found("Titular (contato)")
    r = DataSubjectRequest(organization_id=p.org_id, contact_id=contact.id, request_type=body.request_type, requester_name=body.requester_name or contact.name,
                           requester_contact=body.requester_contact, details=body.details, due_at=utcnow() + timedelta(days=15),
                           result={"changes": body.changes} if body.changes else None)
    session.add(r)
    await session.flush()
    await audit.log_action(session, org_id=p.org_id, action="LGPD_REQUEST_CREATED", resource_type="data_subject_requests", resource_id=r.id, actor_user_id=p.user_id, request=request,
                           new_state={"type": r.request_type})
    emit(session, "lgpd.request_created", p.org_id, {"request_id": str(r.id), "type": r.request_type, "contact_id": str(contact.id)}, p.user_id)
    return dsr_out(r)


@router.get("/requests")
async def list_requests(status: str | None = None, p: Principal = Depends(require("lgpd:manage")), session: AsyncSession = Depends(get_session)):
    q = sa.select(DataSubjectRequest).where(DataSubjectRequest.organization_id == p.org_id)
    if status:
        q = q.where(DataSubjectRequest.status == status)
    rows = (await session.execute(q.order_by(DataSubjectRequest.created_at.desc()).limit(200))).scalars().all()
    return {"items": [dsr_out(r) for r in rows]}


@router.post("/requests/{request_id}/process")
async def process_request(request_id: uuid.UUID, request: Request, p: Principal = Depends(require("lgpd:manage")), session: AsyncSession = Depends(get_session)):
    """Executa a solicitação conforme o tipo (rotina automatizada dos arts. 18/19)."""
    r = await session.get(DataSubjectRequest, request_id)
    if not r or r.organization_id != p.org_id:
        raise not_found("Solicitação")
    if r.status == "completed":
        raise conflict("Solicitação já concluída", "already_completed")
    c = await session.get(Contact, r.contact_id)
    if c is None:
        raise not_found("Titular")
    t = r.request_type
    result: dict
    if t == "confirmation":
        n_t = (await session.execute(sa.select(sa.func.count()).select_from(Ticket).where(Ticket.contact_id == c.id))).scalar_one()
        consents = (await session.execute(sa.select(LgpdConsent).where(LgpdConsent.contact_id == c.id))).scalars().all()
        result = {"processing": not bool(c.anonymized_at), "tickets": n_t, "data_categories": ["identificação (nome, telefone, e-mail)", "histórico de atendimento"],
                  "purposes": sorted({x.purpose for x in consents}), "legal_bases": sorted({x.legal_basis for x in consents})}
    elif t in ("access", "portability"):
        result = await export_subject_data(session, p.org_id, c)
    elif t == "rectification":
        changes = (r.result or {}).get("changes") or {}
        allowed = {k: v for k, v in changes.items() if k in ("name", "email", "job_title")}
        if not allowed:
            raise bad_request("Informe os campos a retificar (name, email, job_title)", code="no_changes")
        for k, v in allowed.items():
            setattr(c, k, v)
        result = {"updated_fields": list(allowed)}
    elif t == "revoke_consent":
        n = (await session.execute(sa.update(LgpdConsent).where(LgpdConsent.contact_id == c.id, LgpdConsent.revoked_at.is_(None)).values(revoked_at=utcnow()))).rowcount
        c.opted_out_at = utcnow()
        result = {"revoked": n, "opted_out": True}
    elif t == "anonymization":
        result = await anonymize_contact(session, p.org_id, c)
    elif t == "deletion":
        result = await delete_conversations(session, p.org_id, c)
    elif t == "info_sharing":
        result = {"operators": [{"name": "Meta Platforms (WhatsApp Business Platform)", "purpose": "Transporte de mensagens"},
                                {"name": "Provedor de nuvem/armazenamento", "purpose": "Hospedagem e armazenamento de mídias"}], "sharing_with_third_parties": "Apenas operadores necessários ao serviço"}
    else:
        raise bad_request("Tipo não suportado")
    r.status, r.completed_at, r.handled_by = "completed", utcnow(), p.user_id
    r.result = {k: v for k, v in result.items()} if t not in ("access", "portability") else {"summary": {"tickets": len(result["tickets"]), "messages": len(result["messages"])}, "export": result}
    await audit.log_action(session, org_id=p.org_id, action=f"LGPD_{t.upper()}", resource_type="contacts", resource_id=c.id, actor_user_id=p.user_id, request=request,
                           new_state={"request_id": str(r.id)})
    return dsr_out(r)


@router.get("/contacts/{contact_id}/export")
async def export_contact(contact_id: uuid.UUID, request: Request, p: Principal = Depends(require("lgpd:manage")), session: AsyncSession = Depends(get_session)):
    import json

    c = await session.get(Contact, contact_id)
    if not c or c.organization_id != p.org_id:
        raise not_found("Contato")
    data = await export_subject_data(session, p.org_id, c)
    await audit.log_action(session, org_id=p.org_id, action="EXPORT_CONTACT_DATA", resource_type="contacts", resource_id=c.id, actor_user_id=p.user_id, request=request)
    return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="titular-{c.id}.json"'})


@router.post("/contacts/{contact_id}/anonymize")
async def anonymize_now(contact_id: uuid.UUID, request: Request, p: Principal = Depends(require("lgpd:manage")), session: AsyncSession = Depends(get_session)):
    c = await session.get(Contact, contact_id)
    if not c or c.organization_id != p.org_id:
        raise not_found("Contato")
    stats = await anonymize_contact(session, p.org_id, c)
    await audit.log_action(session, org_id=p.org_id, action="ANONYMIZE_CONTACT", resource_type="contacts", resource_id=c.id, actor_user_id=p.user_id, request=request, new_state=stats)
    return stats


@router.get("/summary")
async def summary(p: Principal = Depends(require("lgpd:manage")), session: AsyncSession = Depends(get_session)):
    now = utcnow()
    open_q = sa.select(DataSubjectRequest).where(DataSubjectRequest.organization_id == p.org_id, DataSubjectRequest.status.in_(["open", "in_progress"]))
    open_reqs = (await session.execute(open_q)).scalars().all()
    active_consents = (await session.execute(sa.select(sa.func.count()).select_from(LgpdConsent).where(LgpdConsent.organization_id == p.org_id, LgpdConsent.revoked_at.is_(None)))).scalar_one()
    anonymized = (await session.execute(sa.select(sa.func.count()).select_from(Contact).where(Contact.organization_id == p.org_id, Contact.anonymized_at.is_not(None)))).scalar_one()
    opted_out = (await session.execute(sa.select(sa.func.count()).select_from(Contact).where(Contact.organization_id == p.org_id, Contact.opted_out_at.is_not(None)))).scalar_one()
    return {"open_requests": len(open_reqs), "overdue_requests": sum(1 for r in open_reqs if r.due_at and r.due_at < now), "active_consents": active_consents,
            "anonymized_contacts": anonymized, "opted_out_contacts": opted_out}


# ------------------------------------------------------------------ Auditoria
@router.get("/audit-logs")
async def audit_logs(action: str | None = None, resource_type: str | None = None, actor_user_id: uuid.UUID | None = None, resource_id: uuid.UUID | None = None,
                     date_from: date | None = None, date_to: date | None = None, page: Page = Depends(pagination), p: Principal = Depends(require("audit:view")),
                     session: AsyncSession = Depends(get_session)):
    q = sa.select(AuditLog).where(AuditLog.organization_id == p.org_id)
    if action:
        q = q.where(AuditLog.action == action)
    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    if actor_user_id:
        q = q.where(AuditLog.actor_user_id == actor_user_id)
    if resource_id:
        q = q.where(AuditLog.resource_id == resource_id)
    if date_from:
        q = q.where(AuditLog.created_at >= datetime.combine(date_from, dtime.min, tzinfo=timezone.utc))
    if date_to:
        q = q.where(AuditLog.created_at <= datetime.combine(date_to, dtime.max, tzinfo=timezone.utc))
    total = (await session.execute(sa.select(sa.func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await session.execute(q.order_by(AuditLog.seq.desc()).limit(page.limit).offset(page.offset))).scalars().all()
    users = {u.id: u.name for u in (await session.execute(sa.select(User).where(User.organization_id == p.org_id))).scalars().all()}
    return {"total": total, "items": [row(a, ["id", "seq", "action", "resource_type", "resource_id", "ip_address", "old_state", "new_state", "hash", "created_at"],
                                          actor_name=users.get(a.actor_user_id), actor_user_id=a.actor_user_id) for a in rows]}


@router.get("/audit-logs/verify")
async def verify(p: Principal = Depends(require("audit:view")), session: AsyncSession = Depends(get_session)):
    return await audit.verify_chain(session, p.org_id)


@router.get("/audit-logs/export.csv")
async def export_audit(request: Request, p: Principal = Depends(require("audit:view", "export:data")), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(sa.select(AuditLog).where(AuditLog.organization_id == p.org_id).order_by(AuditLog.seq).limit(50000))).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["seq", "created_at", "actor_user_id", "action", "resource_type", "resource_id", "ip_address", "hash"])
    for a in rows:
        w.writerow([a.seq, a.created_at.isoformat(), a.actor_user_id, a.action, a.resource_type, a.resource_id, a.ip_address, a.hash])
    await audit.log_action(session, org_id=p.org_id, action="EXPORT_AUDIT_LOG", resource_type="audit_logs", resource_id=None, actor_user_id=p.user_id, request=request, new_state={"rows": len(rows)})
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="auditoria.csv"'})


@periodic("lgpd.retention", 24 * 3600)
async def retention_job() -> None:
    await purge_expired_media()
