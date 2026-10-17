"""Serviço de atendimento: ciclo de vida do ticket, mensagens, SLA e eventos (fonte única das regras)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, bad_request
from app.core.events import emit
from app.core.serialize import contact_out, plain, row
from app.db import utcnow
from app.models import (
    Contact, Department, Message, MessageDirection, MessageStatus, MessageType, Organization, Ticket, TicketEvent, TicketStatus, User, WhatsAppAccount, OPEN_STATUSES,
)
from app.modules.core.defaults import effective_settings
from app.security.sanitize import sanitize_text

WINDOW = timedelta(hours=24)  # janela de atendimento da Cloud API (mensagens livres)


# ---------------------------------------------------------------- serialização
def message_out(m: Message) -> dict:
    return row(m, ["id", "ticket_id", "meta_message_id", "direction", "sender_user_id", "is_bot", "type", "body", "media_asset_id", "payload", "status", "error", "created_at", "delivered_at", "read_at"])


def sla_state(t: Ticket) -> dict:
    now = utcnow()
    out: dict[str, Any] = {"frt": "ok", "solution": "ok"}
    if t.status in (TicketStatus.closed, TicketStatus.resolved):
        out["frt"] = out["solution"] = "done"
        return out
    if t.first_response_at is None and t.sla_first_response_due_at:
        total = (t.sla_first_response_due_at - t.created_at).total_seconds() or 1
        elapsed = (now - t.created_at).total_seconds()
        out["frt"] = "breached" if elapsed > total else ("at_risk" if elapsed / total >= 0.8 else "ok")
        out["frt_remaining_seconds"] = int(total - elapsed)
    elif t.first_response_at:
        out["frt"] = "done"
    if t.sla_solution_due_at:
        total = (t.sla_solution_due_at - t.created_at).total_seconds() or 1
        elapsed = (now - t.created_at).total_seconds()
        out["solution"] = "breached" if elapsed > total else ("at_risk" if elapsed / total >= 0.8 else "ok")
        out["solution_remaining_seconds"] = int(total - elapsed)
    return out


def ticket_out(t: Ticket, principal=None, agent: User | None = None, department: Department | None = None) -> dict:
    d = row(t, ["id", "status", "priority", "subject", "summary", "tags", "bot_active", "unread_count", "after_hours", "department_id", "assigned_agent_id", "whatsapp_account_id",
                "last_message_preview", "created_at", "queued_at", "assigned_at", "first_response_at", "resolved_at", "closed_at", "last_message_at", "last_inbound_at",
                "sla_first_response_due_at", "sla_solution_due_at", "first_response_time_seconds", "handling_time_seconds", "wait_time_seconds", "is_fcr", "reopen_count"])
    d["contact"] = contact_out(t.contact, principal) if t.contact else None
    d["sla"] = sla_state(t)
    d["window_open"] = bool(t.last_inbound_at and utcnow() - t.last_inbound_at < WINDOW)
    d["window_expires_at"] = plain(t.last_inbound_at + WINDOW) if t.last_inbound_at else None
    d["agent_name"] = agent.name if agent else None
    d["department_name"] = department.name if department else None
    return d


def ticket_event_payload(t: Ticket) -> dict:
    return {"ticket_id": str(t.id), "status": t.status.value, "contact_id": str(t.contact_id), "department_id": str(t.department_id) if t.department_id else None,
            "agent_id": str(t.assigned_agent_id) if t.assigned_agent_id else None, "priority": t.priority, "subject": t.subject,
            "preview": t.last_message_preview, "unread": t.unread_count, "contact_name": t.contact.name if t.contact else None}


# ---------------------------------------------------------------- eventos / histórico
async def log_event(session: AsyncSession, t: Ticket, type_: str, user_id: uuid.UUID | None = None, prev: Any = None, new: Any = None, meta: dict | None = None) -> None:
    session.add(TicketEvent(ticket_id=t.id, user_id=user_id, event_type=type_, previous_value=str(prev) if prev is not None else None,
                            new_value=str(new) if new is not None else None, meta=meta))


async def org_cfg(session: AsyncSession, org_id: uuid.UUID) -> dict:
    org = await session.get(Organization, org_id)
    return effective_settings(org.settings)


# ---------------------------------------------------------------- criação / localização
async def find_open_ticket(session: AsyncSession, org_id: uuid.UUID, contact_id: uuid.UUID) -> Ticket | None:
    return (await session.execute(
        sa.select(Ticket).where(Ticket.organization_id == org_id, Ticket.contact_id == contact_id, Ticket.status.in_(list(OPEN_STATUSES) + [TicketStatus.resolved]))
        .order_by(Ticket.created_at.desc()).limit(1))).scalar_one_or_none()


async def default_department(session: AsyncSession, org_id: uuid.UUID, wa: WhatsAppAccount | None) -> Department | None:
    if wa and wa.default_department_id:
        d = await session.get(Department, wa.default_department_id)
        if d:
            return d
    return (await session.execute(sa.select(Department).where(Department.organization_id == org_id).order_by(Department.is_default.desc(), Department.created_at).limit(1))).scalar_one_or_none()


def apply_sla(t: Ticket, dept: Department | None) -> None:
    first = dept.sla_first_response_seconds if dept else 300
    sol = dept.sla_solution_seconds if dept else 3600
    t.sla_first_response_due_at = t.created_at + timedelta(seconds=first)
    t.sla_solution_due_at = t.created_at + timedelta(seconds=sol)


async def create_ticket(session: AsyncSession, org_id: uuid.UUID, contact: Contact, wa: WhatsAppAccount | None = None, *, bot: bool | None = None, priority: str = "normal",
                        reopened_from: Ticket | None = None, department: Department | None = None) -> Ticket:
    cfg = await org_cfg(session, org_id)
    dept = department or await default_department(session, org_id, wa)
    use_bot = bool(cfg["bot"]["enabled"] and (wa.bot_enabled if wa else True)) if bot is None else bot
    now = utcnow()
    t = Ticket(organization_id=org_id, contact_id=contact.id, department_id=dept.id if dept else None, whatsapp_account_id=wa.id if wa else None, priority=priority,
               status=TicketStatus.pending_bot if use_bot else TicketStatus.queued, bot_active=use_bot, created_at=now, queued_at=None if use_bot else now,
               reopened_from_id=reopened_from.id if reopened_from else None, bot_context={}, tags=[], required_skills=[], sla_flags={})
    apply_sla(t, dept)
    session.add(t)
    await session.flush()
    await session.refresh(t, ["contact"])
    await log_event(session, t, "created", meta={"bot": use_bot, "reopened_from": str(reopened_from.id) if reopened_from else None})
    emit(session, "ticket.created", org_id, ticket_event_payload(t))
    if not use_bot:
        emit(session, "ticket.queued", org_id, ticket_event_payload(t))
    return t


# ---------------------------------------------------------------- mensagens
async def add_message(session: AsyncSession, t: Ticket, *, direction: MessageDirection, type_: MessageType = MessageType.text, body: str | None = None, sender_user_id: uuid.UUID | None = None,
                      is_bot: bool = False, meta_message_id: str | None = None, status: MessageStatus | None = None, payload: dict | None = None,
                      media_asset_id: uuid.UUID | None = None, created_at: datetime | None = None) -> Message:
    now = created_at or utcnow()
    inbound = direction == MessageDirection.inbound
    m = Message(ticket_id=t.id, organization_id=t.organization_id, meta_message_id=meta_message_id, direction=direction, type=type_, body=sanitize_text(body) if body else body,
                sender_user_id=sender_user_id, is_bot=is_bot, payload=payload, media_asset_id=media_asset_id, created_at=now,
                status=status or (MessageStatus.received if inbound else MessageStatus.queued))
    session.add(m)
    t.last_message_at = now
    preview = m.body or f"[{type_.value}]"
    t.last_message_preview = preview[:180]
    if inbound:
        t.last_inbound_at = now
        t.unread_count = (t.unread_count or 0) + 1
    await session.flush()
    emit(session, "message.created", t.organization_id, {**message_out(m), "ticket": ticket_event_payload(t)})
    if inbound:
        emit(session, "message.received", t.organization_id, {"message_id": str(m.id), "ticket_id": str(t.id), "contact_id": str(t.contact_id), "type": type_.value})
    return m


async def queue_outbound(session: AsyncSession, t: Ticket, *, body: str | None, type_: MessageType = MessageType.text, sender_user_id: uuid.UUID | None = None, is_bot: bool = False,
                         payload: dict | None = None, media_asset_id: uuid.UUID | None = None, enforce_window: bool = True) -> Message:
    """Registra a mensagem de saída (status ``queued``) e dispara o envio assíncrono via barramento."""
    if t.contact and t.contact.opted_out_at:
        raise AppError(409, "contact_opted_out", "Contato solicitou não receber mensagens (opt-out)")
    if t.contact and t.contact.anonymized_at:
        raise AppError(409, "contact_anonymized", "Contato anonimizado (LGPD)")
    free_form = type_ != MessageType.template
    if enforce_window and free_form and not (t.last_inbound_at and utcnow() - t.last_inbound_at < WINDOW):
        raise AppError(409, "window_closed", "Janela de 24h encerrada: envie um template aprovado (HSM) para reabrir a conversa", {"requires_template": True})
    m = await add_message(session, t, direction=MessageDirection.outbound, type_=type_, body=body, sender_user_id=sender_user_id, is_bot=is_bot, payload=payload, media_asset_id=media_asset_id)
    if sender_user_id and not is_bot:
        await register_human_response(session, t, sender_user_id)
    t.last_message_preview = "Você: " + (m.body or f"[{type_.value}]")[:170] if sender_user_id and not is_bot else (m.body or f"[{type_.value}]")[:180]
    emit(session, "message.outbound_queued", t.organization_id, {"message_id": str(m.id), "ticket_id": str(t.id)})
    return m


async def register_human_response(session: AsyncSession, t: Ticket, user_id: uuid.UUID) -> None:
    now = utcnow()
    t.last_human_outbound_at = now
    t.unread_count = 0
    if t.first_response_at is None:
        t.first_response_at = now
        t.first_response_time_seconds = max(int((now - t.created_at).total_seconds()), 0)
        await log_event(session, t, "first_response", user_id, meta={"seconds": t.first_response_time_seconds})
        emit(session, "ticket.first_response", t.organization_id, {"ticket_id": str(t.id), "seconds": t.first_response_time_seconds, "agent_id": str(user_id)})


# ---------------------------------------------------------------- transições
async def set_status(session: AsyncSession, t: Ticket, new: TicketStatus, user_id: uuid.UUID | None = None, reason: str | None = None) -> None:
    old = t.status
    if old == new:
        return
    now = utcnow()
    t.status = new
    if new == TicketStatus.queued:
        t.queued_at = now
        t.bot_active = False
    if new in (TicketStatus.resolved, TicketStatus.closed):
        t.resolved_at = t.resolved_at or now
    if new == TicketStatus.closed:
        t.closed_at = now
        if t.assigned_at:
            t.handling_time_seconds = max(int((now - t.assigned_at).total_seconds()), 0)
        t.is_fcr = t.reopen_count == 0
        t.bot_active = False
    if old in (TicketStatus.resolved, TicketStatus.closed) and new in OPEN_STATUSES:
        t.closed_at, t.resolved_at = None, None
    await log_event(session, t, "status_change", user_id, old.value, new.value, {"reason": reason} if reason else None)
    payload = ticket_event_payload(t)
    emit(session, "ticket.updated", t.organization_id, payload, user_id)
    if new == TicketStatus.queued:
        emit(session, "ticket.queued", t.organization_id, payload, user_id)
    if new == TicketStatus.resolved:
        emit(session, "ticket.resolved", t.organization_id, payload, user_id)
    if new == TicketStatus.closed:
        emit(session, "ticket.closed", t.organization_id, {**payload, "handling_time_seconds": t.handling_time_seconds, "first_response_time_seconds": t.first_response_time_seconds,
                                                           "is_fcr": t.is_fcr, "closed_at": t.closed_at.isoformat()}, user_id)


async def assign(session: AsyncSession, t: Ticket, agent: User | None, by_user_id: uuid.UUID | None = None, reason: str | None = None) -> None:
    prev = t.assigned_agent_id
    now = utcnow()
    if agent is None:
        t.assigned_agent_id = None
        await log_event(session, t, "agent_unassigned", by_user_id, prev, None, {"reason": reason})
        await set_status(session, t, TicketStatus.queued, by_user_id, reason)
        return
    t.assigned_agent_id = agent.id
    t.bot_active = False
    if t.assigned_at is None:
        t.assigned_at = now
        t.wait_time_seconds = max(int((now - (t.queued_at or t.created_at)).total_seconds()), 0)
    await log_event(session, t, "agent_assigned", by_user_id, prev, agent.id, {"reason": reason, "agent_name": agent.name})
    if t.status in (TicketStatus.queued, TicketStatus.pending_bot):
        await set_status(session, t, TicketStatus.in_progress, by_user_id, reason)
    emit(session, "ticket.assigned", t.organization_id, {**ticket_event_payload(t), "previous_agent_id": str(prev) if prev else None, "reason": reason}, by_user_id)


async def transfer(session: AsyncSession, t: Ticket, *, by_user_id: uuid.UUID | None, department_id: uuid.UUID | None = None, agent_id: uuid.UUID | None = None, reason: str | None = None) -> None:
    """Transfere entre departamentos/agentes mantendo todo o histórico (mesmo ticket, mesmas mensagens e notas)."""
    if not department_id and not agent_id:
        raise bad_request("Informe departamento ou agente de destino")
    prev_dept, prev_agent = t.department_id, t.assigned_agent_id
    new_agent = None
    if agent_id:
        new_agent = await session.get(User, agent_id)
        if not new_agent or new_agent.organization_id != t.organization_id or not new_agent.is_active:
            raise bad_request("Agente de destino inválido")
        if not department_id and new_agent.department_ids and t.department_id not in new_agent.department_ids:
            department_id = new_agent.department_ids[0]
    dept = None
    if department_id:
        dept = await session.get(Department, department_id)
        if not dept or dept.organization_id != t.organization_id:
            raise bad_request("Departamento de destino inválido")
        t.department_id = dept.id
        if t.first_response_at is None:
            apply_sla(t, dept)
    await log_event(session, t, "transferred", by_user_id, f"{prev_dept}|{prev_agent}", f"{t.department_id}|{agent_id}", {"reason": reason, "to_department": str(department_id) if department_id else None,
                                                                                                                        "to_agent": str(agent_id) if agent_id else None})
    emit(session, "ticket.transferred", t.organization_id, {**ticket_event_payload(t), "from_department_id": str(prev_dept) if prev_dept else None,
                                                            "from_agent_id": str(prev_agent) if prev_agent else None, "reason": reason}, by_user_id)
    if new_agent:
        t.assigned_agent_id = None
        await assign(session, t, new_agent, by_user_id, reason=f"transfer:{reason or ''}")
    else:
        t.assigned_agent_id = None
        t.queued_at = utcnow()
        if t.status != TicketStatus.queued:
            await set_status(session, t, TicketStatus.queued, by_user_id, "transferred")
        else:
            emit(session, "ticket.queued", t.organization_id, ticket_event_payload(t))


async def close_ticket(session: AsyncSession, t: Ticket, user_id: uuid.UUID | None, reason: str | None = None) -> None:
    await set_status(session, t, TicketStatus.closed, user_id, reason)


async def reopen_ticket(session: AsyncSession, t: Ticket, user_id: uuid.UUID | None = None) -> None:
    if t.status not in (TicketStatus.resolved, TicketStatus.closed):
        return
    t.reopen_count += 1
    t.is_fcr = False
    target = TicketStatus.in_progress if t.assigned_agent_id else TicketStatus.queued
    await log_event(session, t, "reopened", user_id)
    await set_status(session, t, target, user_id, "reopened")


async def mark_previous_not_fcr(session: AsyncSession, org_id: uuid.UUID, contact_id: uuid.UUID, within: timedelta = timedelta(hours=24)) -> Ticket | None:
    """Cliente voltou em < 24h: o atendimento anterior deixa de contar como FCR."""
    prev = (await session.execute(
        sa.select(Ticket).where(Ticket.organization_id == org_id, Ticket.contact_id == contact_id, Ticket.status == TicketStatus.closed, Ticket.closed_at >= utcnow() - within)
        .order_by(Ticket.closed_at.desc()).limit(1))).scalar_one_or_none()
    if prev:
        prev.is_fcr = False
    return prev


async def load_transcript(session: AsyncSession, ticket_id: uuid.UUID, limit: int = 60) -> list[dict]:
    rows = (await session.execute(sa.select(Message).where(Message.ticket_id == ticket_id).order_by(Message.created_at.desc()).limit(limit))).scalars().all()
    return [message_out(m) for m in reversed(rows)]
