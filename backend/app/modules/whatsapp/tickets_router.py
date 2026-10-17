"""Mesa de atendimento (Agent Workspace): tickets, mensagens, notas internas, transferência, encerramento."""
from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.deps import Page, Principal, get_principal, pagination, require
from app.core.errors import AppError, bad_request, forbidden, not_found
from app.core.events import emit
from app.core.serialize import row
from app.db import get_session, utcnow
from app.models import (
    Contact, Department, MediaAsset, Message, MessageDirection, MessageType, Ticket, TicketEvent, TicketNote, TicketStatus, User, WaTemplate, TemplateStatus,
)
from app.modules.whatsapp import llm, media as media_mod, service as svc
from app.modules.whatsapp.outbound import client_for
from app.modules.whatsapp.templates import render_template_components, render_template_text
from app.security.rbac import can_access_ticket
from app.security.sanitize import sanitize_text

router = APIRouter(prefix="/api/v1/tickets", tags=["Atendimento WhatsApp"])


def ticket_scope(p: Principal, stmt):
    stmt = stmt.where(Ticket.organization_id == p.org_id)
    if p.can("inbox:all"):
        return stmt
    queue = sa.and_(Ticket.assigned_agent_id.is_(None), sa.or_(Ticket.department_id.is_(None), Ticket.department_id.in_(list(p.department_ids) or [uuid.uuid4()])))
    return stmt.where(sa.or_(Ticket.assigned_agent_id == p.user_id, queue))


async def get_ticket(session: AsyncSession, p: Principal, tid: uuid.UUID, *, write: bool = False) -> Ticket:
    t = await session.get(Ticket, tid)
    if not t or t.organization_id != p.org_id:
        raise not_found("Atendimento")
    await session.refresh(t, ["contact"])
    if not can_access_ticket(p.role, p.user_id, p.department_ids, t):
        raise forbidden("Sem acesso a este atendimento")
    if write and t.assigned_agent_id and t.assigned_agent_id != p.user_id and not p.can("inbox:all"):
        raise forbidden("Atendimento em andamento com outro agente")
    return t


async def _name_maps(session: AsyncSession, org_id: uuid.UUID) -> tuple[dict, dict]:
    users = {u.id: u for u in (await session.execute(sa.select(User).where(User.organization_id == org_id))).scalars().all()}
    depts = {d.id: d for d in (await session.execute(sa.select(Department).where(Department.organization_id == org_id))).scalars().all()}
    return users, depts


@router.get("")
async def list_tickets(view: str = "mine", department_id: uuid.UUID | None = None, agent_id: uuid.UUID | None = None, contact_id: uuid.UUID | None = None, q: str | None = None,
                       page: Page = Depends(pagination), p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    stmt = ticket_scope(p, sa.select(Ticket).join(Contact, Contact.id == Ticket.contact_id))
    open_statuses = [TicketStatus.pending_bot, TicketStatus.queued, TicketStatus.in_progress, TicketStatus.pending_client]
    if view == "mine":
        stmt = stmt.where(Ticket.assigned_agent_id == p.user_id, Ticket.status.in_(open_statuses + [TicketStatus.resolved]))
    elif view == "queue":
        stmt = stmt.where(Ticket.status == TicketStatus.queued, Ticket.assigned_agent_id.is_(None))
    elif view == "bot":
        stmt = stmt.where(Ticket.status == TicketStatus.pending_bot)
    elif view == "closed":
        stmt = stmt.where(Ticket.status.in_([TicketStatus.closed, TicketStatus.resolved]))
    elif view == "open":
        stmt = stmt.where(Ticket.status.in_(open_statuses))
    if department_id:
        stmt = stmt.where(Ticket.department_id == department_id)
    if agent_id:
        stmt = stmt.where(Ticket.assigned_agent_id == agent_id)
    if contact_id:
        stmt = stmt.where(Ticket.contact_id == contact_id)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(sa.or_(sa.func.lower(Contact.name).like(like), sa.func.lower(Ticket.last_message_preview).like(like), sa.func.lower(Ticket.subject).like(like), Contact.wa_id.like(f"%{q}%")))
    total = (await session.execute(sa.select(sa.func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.order_by(Ticket.last_message_at.desc().nulls_last(), Ticket.created_at.desc()).limit(page.limit).offset(page.offset))).scalars().all()
    users, depts = await _name_maps(session, p.org_id)
    counts = {}
    if view == "mine":
        counts["queue"] = (await session.execute(ticket_scope(p, sa.select(sa.func.count()).select_from(Ticket)).where(Ticket.status == TicketStatus.queued, Ticket.assigned_agent_id.is_(None)))).scalar_one()
    return {"total": total, "counts": counts, "items": [svc.ticket_out(t, p, users.get(t.assigned_agent_id), depts.get(t.department_id)) for t in rows]}


@router.get("/{ticket_id}")
async def get_ticket_detail(ticket_id: uuid.UUID, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id)
    users, depts = await _name_maps(session, p.org_id)
    out = svc.ticket_out(t, p, users.get(t.assigned_agent_id), depts.get(t.department_id))
    from app.models import CrmDealTicket, Deal

    deals = (await session.execute(sa.select(Deal).join(CrmDealTicket, CrmDealTicket.deal_id == Deal.id).where(CrmDealTicket.ticket_id == t.id))).scalars().all()
    out["deals"] = [row(d, ["id", "title", "stage", "account_id"]) for d in deals]
    return out


@router.get("/{ticket_id}/messages")
async def list_messages(ticket_id: uuid.UUID, before: datetime | None = None, limit: int = 60, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id)
    stmt = sa.select(Message).where(Message.ticket_id == t.id)
    if before:
        stmt = stmt.where(Message.created_at < before)
    msgs = list(reversed((await session.execute(stmt.order_by(Message.created_at.desc()).limit(min(limit, 200)))).scalars().all()))
    notes_q = sa.select(TicketNote).where(TicketNote.ticket_id == t.id).order_by(TicketNote.created_at)
    notes = [n for n in (await session.execute(notes_q)).scalars().all()
             if n.kind == "note" or p.can("supervisor:live") or n.target_user_id == p.user_id or n.author_id == p.user_id]
    users, _ = await _name_maps(session, p.org_id)
    return {"messages": [svc.message_out(m) for m in msgs], "has_more": len(msgs) == min(limit, 200),
            "notes": [row(n, ["id", "kind", "body", "author_id", "target_user_id", "created_at"], author_name=users[n.author_id].name if n.author_id in users else "Bot") for n in notes]}


class SendIn(BaseModel):
    type: str = Field("text", pattern="^(text|template)$")
    body: str | None = Field(None, max_length=4096)
    template_id: uuid.UUID | None = None
    variables: dict[str, str] = {}


async def _auto_claim(session: AsyncSession, t: Ticket, p: Principal) -> None:
    """Atendente que responde um ticket da fila (ou do bot) o assume automaticamente."""
    if t.assigned_agent_id is None and p.user:
        await svc.assign(session, t, p.user, p.user_id, reason="claimed_on_reply")


@router.post("/{ticket_id}/messages", status_code=201)
async def send_message(ticket_id: uuid.UUID, body: SendIn, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id, write=True)
    if t.status == TicketStatus.closed and body.type != "template":
        raise AppError(409, "ticket_closed", "Atendimento encerrado. Reabra-o para responder.")
    if body.type == "template":
        tpl = await session.get(WaTemplate, body.template_id) if body.template_id else None
        if not tpl or tpl.organization_id != p.org_id or tpl.status != TemplateStatus.approved:
            raise bad_request("Template inexistente ou não aprovado pela Meta", code="template_not_approved")
        comps = render_template_components(tpl, body.variables)
        text = render_template_text(tpl, body.variables)
        await _auto_claim(session, t, p)
        if t.status in (TicketStatus.closed, TicketStatus.resolved):
            await svc.reopen_ticket(session, t, p.user_id)
        m = await svc.queue_outbound(session, t, body=text, type_=MessageType.template, sender_user_id=p.user_id,
                                     payload={"template": {"name": tpl.name, "language": tpl.language, "components": comps}, "template_id": str(tpl.id)})
    else:
        text = sanitize_text(body.body or "", 4096).strip()
        if not text:
            raise bad_request("Mensagem vazia")
        await _auto_claim(session, t, p)
        m = await svc.queue_outbound(session, t, body=text, sender_user_id=p.user_id)
        if t.status == TicketStatus.resolved:
            await svc.set_status(session, t, TicketStatus.in_progress, p.user_id, "agent_replied")
    return svc.message_out(m)


@router.post("/{ticket_id}/media", status_code=201)
async def send_media(ticket_id: uuid.UUID, file: UploadFile = File(...), caption: str | None = Form(None), p: Principal = Depends(require("inbox:use")),
                     session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id, write=True)
    data = await file.read(media_mod.MAX_BYTES + 1)
    asset = await media_mod.store_bytes(session, p.org_id, t.id, data, file.content_type, file.filename)
    mime = asset.mime_type or ""
    mtype = MessageType.image if mime.startswith("image/") else MessageType.audio if mime.startswith("audio/") else MessageType.video if mime.startswith("video/") else MessageType.document
    await _auto_claim(session, t, p)
    m = await svc.queue_outbound(session, t, body=sanitize_text(caption, 1000) or (asset.filename if mtype == MessageType.document else None), type_=mtype, sender_user_id=p.user_id,
                                 media_asset_id=asset.id)
    return svc.message_out(m)


class StartIn(BaseModel):
    contact_id: uuid.UUID
    template_id: uuid.UUID
    variables: dict[str, str] = {}
    department_id: uuid.UUID | None = None


@router.post("/outbound", status_code=201)
async def start_outbound(body: StartIn, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    """Conversa ativa iniciada pela empresa (fora da janela de 24h) — exige template aprovado."""
    c = await session.get(Contact, body.contact_id)
    if not c or c.organization_id != p.org_id:
        raise not_found("Contato")
    tpl = await session.get(WaTemplate, body.template_id)
    if not tpl or tpl.organization_id != p.org_id or tpl.status != TemplateStatus.approved:
        raise bad_request("Template inexistente ou não aprovado", code="template_not_approved")
    t = await svc.find_open_ticket(session, p.org_id, c.id)
    if t is None:
        t = await svc.create_ticket(session, p.org_id, c, None, bot=False)
        t.status = TicketStatus.queued
    await session.refresh(t, ["contact"])
    if t.assigned_agent_id is None:
        await svc.assign(session, t, p.user, p.user_id, reason="outbound_started")
    m = await svc.queue_outbound(session, t, body=render_template_text(tpl, body.variables), type_=MessageType.template, sender_user_id=p.user_id,
                                 payload={"template": {"name": tpl.name, "language": tpl.language, "components": render_template_components(tpl, body.variables)}, "template_id": str(tpl.id)})
    return {"ticket_id": str(t.id), "message": svc.message_out(m)}


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.post("/{ticket_id}/notes", status_code=201)
async def add_note(ticket_id: uuid.UUID, body: NoteIn, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    """Nota interna — nunca é enviada ao cliente."""
    t = await get_ticket(session, p, ticket_id)
    n = TicketNote(ticket_id=t.id, organization_id=p.org_id, author_id=p.user_id, kind="note", body=sanitize_text(body.body, 4000))
    session.add(n)
    await session.flush()
    emit(session, "ticket.note_added", p.org_id, {"ticket_id": str(t.id), "note_id": str(n.id), "author_id": str(p.user_id), "body": n.body, "kind": "note",
                                                   "agent_id": str(t.assigned_agent_id) if t.assigned_agent_id else None, "department_id": str(t.department_id) if t.department_id else None})
    return row(n, ["id", "kind", "body", "author_id", "created_at"], author_name=p.user.name if p.user else None)


class AssignIn(BaseModel):
    agent_id: uuid.UUID | None = None  # None = assumir para mim


@router.post("/{ticket_id}/assign")
async def assign_ticket(ticket_id: uuid.UUID, body: AssignIn, request: Request, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id)
    target_id = body.agent_id or p.user_id
    if target_id != p.user_id and not p.can("inbox:all"):
        raise forbidden("Apenas supervisores podem atribuir a outro agente")
    if t.assigned_agent_id and t.assigned_agent_id != p.user_id and not p.can("inbox:all"):
        raise forbidden("Atendimento já está com outro agente")
    agent = await session.get(User, target_id)
    if not agent or agent.organization_id != p.org_id:
        raise not_found("Agente")
    prev_status = t.status
    await svc.assign(session, t, agent, p.user_id, reason="manual")
    if prev_status in (TicketStatus.resolved, TicketStatus.closed):
        await svc.reopen_ticket(session, t, p.user_id)
    await audit.log_action(session, org_id=p.org_id, action="ASSIGN_TICKET", resource_type="tickets", resource_id=t.id, actor_user_id=p.user_id, request=request,
                           new_state={"agent_id": str(agent.id)})
    return svc.ticket_out(t, p, agent)


class TransferIn(BaseModel):
    department_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    reason: str | None = Field(None, max_length=300)


@router.post("/{ticket_id}/transfer")
async def transfer_ticket(ticket_id: uuid.UUID, body: TransferIn, request: Request, p: Principal = Depends(require("inbox:transfer")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id, write=True)
    await svc.transfer(session, t, by_user_id=p.user_id, department_id=body.department_id, agent_id=body.agent_id, reason=sanitize_text(body.reason, 300))
    if body.reason:  # nota interna preserva o contexto da passagem de bastão
        session.add(TicketNote(ticket_id=t.id, organization_id=p.org_id, author_id=p.user_id, kind="note", body=f"↪ Transferido: {sanitize_text(body.reason, 300)}"))
    await audit.log_action(session, org_id=p.org_id, action="TRANSFER_TICKET", resource_type="tickets", resource_id=t.id, actor_user_id=p.user_id, request=request,
                           new_state={"department_id": str(body.department_id), "agent_id": str(body.agent_id)})
    return svc.ticket_out(t, p)


class StatusIn(BaseModel):
    status: TicketStatus


@router.post("/{ticket_id}/status")
async def change_status(ticket_id: uuid.UUID, body: StatusIn, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id, write=True)
    if body.status in (TicketStatus.pending_bot, TicketStatus.queued):
        raise bad_request("Use transferência/fila para esta transição", code="invalid_transition")
    if body.status == TicketStatus.closed:
        if not p.can("inbox:close"):
            raise forbidden()
        await svc.close_ticket(session, t, p.user_id, "closed_by_agent")
    elif t.status in (TicketStatus.resolved, TicketStatus.closed) and body.status in (TicketStatus.in_progress, TicketStatus.pending_client):
        await svc.reopen_ticket(session, t, p.user_id)
    else:
        await svc.set_status(session, t, body.status, p.user_id)
    return svc.ticket_out(t, p)


@router.post("/{ticket_id}/close")
async def close(ticket_id: uuid.UUID, p: Principal = Depends(require("inbox:close")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id, write=True)
    await svc.close_ticket(session, t, p.user_id, "closed_by_agent")
    return svc.ticket_out(t, p)


@router.post("/{ticket_id}/reopen")
async def reopen(ticket_id: uuid.UUID, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id)
    if t.status not in (TicketStatus.closed, TicketStatus.resolved):
        raise bad_request("Atendimento já está aberto", code="not_closed")
    if t.assigned_agent_id is None and p.user:
        await svc.assign(session, t, p.user, p.user_id, reason="reopen")
    await svc.reopen_ticket(session, t, p.user_id)
    return svc.ticket_out(t, p)


class TicketPatch(BaseModel):
    priority: str | None = Field(None, pattern="^(low|normal|high|urgent)$")
    subject: str | None = Field(None, max_length=200)
    tags: list[str] | None = None


@router.patch("/{ticket_id}")
async def patch_ticket(ticket_id: uuid.UUID, body: TicketPatch, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id, write=True)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(t, k, sanitize_text(v, 200) if isinstance(v, str) else v)
    emit(session, "ticket.updated", p.org_id, svc.ticket_event_payload(t), p.user_id)
    return svc.ticket_out(t, p)


@router.post("/{ticket_id}/read", status_code=204)
async def mark_read(ticket_id: uuid.UUID, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id)
    t.unread_count = 0
    last = (await session.execute(sa.select(Message.meta_message_id).where(Message.ticket_id == t.id, Message.direction == MessageDirection.inbound,
                                                                         Message.meta_message_id.is_not(None)).order_by(Message.created_at.desc()).limit(1))).scalar_one_or_none()
    if last and t.assigned_agent_id == p.user_id:
        client, _ = await client_for(session, t)
        try:
            await client.mark_read(last)  # envia "lido" (tiques azuis) ao cliente
        except Exception:  # noqa: BLE001
            pass


@router.post("/{ticket_id}/summary")
async def regenerate_summary(ticket_id: uuid.UUID, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id)
    t.summary = await llm.summarize_conversation(await svc.load_transcript(session, t.id))
    return {"summary": t.summary}


@router.get("/{ticket_id}/events")
async def ticket_events(ticket_id: uuid.UUID, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    t = await get_ticket(session, p, ticket_id)
    rows = (await session.execute(sa.select(TicketEvent).where(TicketEvent.ticket_id == t.id).order_by(TicketEvent.created_at))).scalars().all()
    users, depts = await _name_maps(session, p.org_id)
    return {"items": [row(e, ["id", "event_type", "previous_value", "new_value", "user_id", "meta", "created_at"], user_name=users[e.user_id].name if e.user_id in users else None) for e in rows]}


class LinkIn(BaseModel):
    account_id: uuid.UUID
    role_description: str | None = None


@router.post("/{ticket_id}/link-account")
async def link_account(ticket_id: uuid.UUID, body: LinkIn, p: Principal = Depends(require("inbox:use")), session: AsyncSession = Depends(get_session)):
    """Associa manualmente o número do WhatsApp a uma Conta institucional do CRM."""
    from app.models import Account, AccountContact

    t = await get_ticket(session, p, ticket_id)
    acc = await session.get(Account, body.account_id)
    if not acc or acc.organization_id != p.org_id:
        raise not_found("Conta")
    if not (await session.execute(sa.select(AccountContact.id).where(AccountContact.account_id == acc.id, AccountContact.contact_id == t.contact_id))).first():
        session.add(AccountContact(account_id=acc.id, contact_id=t.contact_id, role_description=body.role_description))
    emit(session, "contact.linked", p.org_id, {"account_id": str(acc.id), "contact_id": str(t.contact_id), "ticket_id": str(t.id)}, p.user_id)
    return {"linked": True, "account_id": str(acc.id)}
