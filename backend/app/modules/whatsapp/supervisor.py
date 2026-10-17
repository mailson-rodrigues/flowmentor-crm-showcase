"""Live supervision dashboard, whispering, barge-in and ticket SLA monitor."""
from __future__ import annotations

import uuid
from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import audit
from app.core.deps import Principal, require
from app.core.errors import bad_request, not_found
from app.core.events import emit
from app.core.scheduler import periodic
from app.core.serialize import plain, row
from app.db import get_session, session_scope, utcnow
from app.models import Department, SatisfactionSurvey, Ticket, TicketNote, TicketStatus, User, OPEN_STATUSES
from app.modules.whatsapp import service as svc
from app.modules.whatsapp.routing import AGENT_ROLES, agent_loads
from app.security.sanitize import sanitize_text

router = APIRouter(prefix="/api/v1/supervisor", tags=["Supervisão ao vivo"])


@router.get("/live")
async def live(p: Principal = Depends(require("supervisor:live")), session: AsyncSession = Depends(get_session)):
    now = utcnow()
    org = p.org_id
    depts = {d.id: d for d in (await session.execute(sa.select(Department).where(Department.organization_id == org))).scalars().all()}
    users = {u.id: u for u in (await session.execute(sa.select(User).where(User.organization_id == org))).scalars().all()}
    open_tickets = (await session.execute(sa.select(Ticket).where(Ticket.organization_id == org, Ticket.status.in_(OPEN_STATUSES)).order_by(Ticket.created_at))).scalars().all()
    loads = await agent_loads(session, org)

    queues = []
    for d in depts.values():
        waiting = [t for t in open_tickets if t.department_id == d.id and t.status == TicketStatus.queued and t.assigned_agent_id is None]
        queues.append({"department_id": str(d.id), "name": d.name, "waiting": len(waiting), "oldest_wait_seconds": int(max(((now - (t.queued_at or t.created_at)).total_seconds() for t in waiting), default=0)),
                       "in_progress": sum(1 for t in open_tickets if t.department_id == d.id and t.status == TicketStatus.in_progress),
                       "bot": sum(1 for t in open_tickets if t.department_id == d.id and t.status == TicketStatus.pending_bot)})
    unrouted = [t for t in open_tickets if t.department_id is None and t.status == TicketStatus.queued]
    if unrouted:
        queues.append({"department_id": None, "name": "Sem departamento", "waiting": len(unrouted), "oldest_wait_seconds": 0, "in_progress": 0, "bot": 0})

    items, alerts = [], []
    for t in open_tickets:
        await session.refresh(t, ["contact"])
        sla = svc.sla_state(t)
        o = svc.ticket_out(t, p, users.get(t.assigned_agent_id), depts.get(t.department_id))
        o["wait_seconds"] = int((now - (t.queued_at or t.created_at)).total_seconds())
        items.append(o)
        if sla["frt"] in ("at_risk", "breached") or sla["solution"] in ("at_risk", "breached"):
            alerts.append({"ticket_id": str(t.id), "contact_name": t.contact.name if t.contact else None, "department": depts[t.department_id].name if t.department_id in depts else None,
                           "agent": users[t.assigned_agent_id].name if t.assigned_agent_id in users else None, "frt": sla["frt"], "solution": sla["solution"],
                           "level": "breached" if "breached" in (sla["frt"], sla["solution"]) else "at_risk"})
    alerts.sort(key=lambda a: 0 if a["level"] == "breached" else 1)

    agents = [{"id": str(u.id), "name": u.name, "role": u.role, "status": u.status, "active_chats": loads.get(u.id, 0), "max_chats": u.max_concurrent_chats,
               "status_since": plain(u.status_changed_at), "skills": u.skills} for u in users.values() if u.role in AGENT_ROLES and u.is_active]
    agents.sort(key=lambda a: ({"available": 0, "busy": 1, "paused": 2, "offline": 3}.get(a["status"], 4), a["name"]))

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    created_today = (await session.execute(sa.select(sa.func.count()).select_from(Ticket).where(Ticket.organization_id == org, Ticket.created_at >= today))).scalar_one()
    closed_today = (await session.execute(sa.select(sa.func.count()).select_from(Ticket).where(Ticket.organization_id == org, Ticket.closed_at >= today))).scalar_one()
    frts = [x for x in (await session.execute(sa.select(Ticket.first_response_time_seconds).where(Ticket.organization_id == org, Ticket.created_at >= today, Ticket.first_response_time_seconds.is_not(None)))).scalars().all()]
    csat = (await session.execute(sa.select(sa.func.avg(SatisfactionSurvey.rating)).where(SatisfactionSurvey.organization_id == org, SatisfactionSurvey.survey_type == "csat",
                                                                                           SatisfactionSurvey.answered_at >= today))).scalar_one()
    return {"generated_at": now.isoformat(), "queues": queues, "agents": agents, "tickets": items, "sla_alerts": alerts,
            "totals": {"open": len(open_tickets), "waiting": sum(q["waiting"] for q in queues), "in_progress": sum(1 for t in open_tickets if t.status == TicketStatus.in_progress),
                       "bot": sum(1 for t in open_tickets if t.status == TicketStatus.pending_bot), "created_today": created_today, "closed_today": closed_today,
                       "avg_frt_today": round(sum(frts) / len(frts)) if frts else None, "csat_today": round(float(csat), 2) if csat else None,
                       "agents_online": sum(1 for a in agents if a["status"] in ("available", "busy"))}}


class WhisperIn(BaseModel):
    body: str = Field(min_length=1, max_length=1000)


@router.post("/tickets/{ticket_id}/whisper", status_code=201)
async def whisper(ticket_id: uuid.UUID, body: WhisperIn, request: Request, p: Principal = Depends(require("supervisor:live")), session: AsyncSession = Depends(get_session)):
    """Whisper: guidance visible only to the responsible agent (the customer never sees it)."""
    t = await session.get(Ticket, ticket_id)
    if not t or t.organization_id != p.org_id:
        raise not_found("Atendimento")
    if not t.assigned_agent_id:
        raise bad_request("Atendimento sem agente responsável", code="no_agent")
    n = TicketNote(ticket_id=t.id, organization_id=p.org_id, author_id=p.user_id, kind="whisper", target_user_id=t.assigned_agent_id, body=sanitize_text(body.body, 1000))
    session.add(n)
    await session.flush()
    await svc.log_event(session, t, "whisper", p.user_id)
    emit(session, "ticket.whisper", p.org_id, {"ticket_id": str(t.id), "note_id": str(n.id), "target_user_id": str(t.assigned_agent_id), "author_id": str(p.user_id),
                                                "author_name": p.user.name if p.user else None, "body": n.body})
    await audit.log_action(session, org_id=p.org_id, action="WHISPER", resource_type="tickets", resource_id=t.id, actor_user_id=p.user_id, request=request)
    return {"id": str(n.id), "created_at": plain(n.created_at)}


@router.post("/tickets/{ticket_id}/barge-in")
async def barge_in(ticket_id: uuid.UUID, request: Request, p: Principal = Depends(require("supervisor:live")), session: AsyncSession = Depends(get_session)):
    """Barge-in: the supervisor takes over the conversation immediately; the previous agent is notified."""
    t = await session.get(Ticket, ticket_id)
    if not t or t.organization_id != p.org_id or t.status in (TicketStatus.closed,):
        raise not_found("Atendimento")
    await session.refresh(t, ["contact"])
    prev = t.assigned_agent_id
    await svc.log_event(session, t, "barge_in", p.user_id, prev, p.user_id)
    t.assigned_agent_id = None
    await svc.assign(session, t, p.user, p.user_id, reason="barge_in")
    if t.status == TicketStatus.pending_client:
        await svc.set_status(session, t, TicketStatus.in_progress, p.user_id)
    session.add(TicketNote(ticket_id=t.id, organization_id=p.org_id, author_id=p.user_id, kind="note", body=f"⚡ {p.user.name} assumiu a conversa (barge-in)."))
    emit(session, "ticket.barge_in", p.org_id, {"ticket_id": str(t.id), "previous_agent_id": str(prev) if prev else None, "supervisor_id": str(p.user_id)})
    await audit.log_action(session, org_id=p.org_id, action="BARGE_IN", resource_type="tickets", resource_id=t.id, actor_user_id=p.user_id, request=request,
                           old_state={"agent_id": str(prev)}, new_state={"agent_id": str(p.user_id)})
    return svc.ticket_out(t, p, p.user)


@periodic("tickets.sla_monitor", 30)
async def sla_monitor() -> None:
    """Emits ``sla.at_risk`` (>= 80% of the deadline) and ``sla.breached`` once per ticket/type."""
    now = utcnow()
    async with session_scope() as s:
        tickets = (await s.execute(sa.select(Ticket).where(Ticket.status.in_(OPEN_STATUSES)))).scalars().all()
        for t in tickets:
            flags = dict(t.sla_flags or {})
            changed = False
            checks = []
            if t.first_response_at is None and t.status != TicketStatus.pending_bot and t.sla_first_response_due_at:
                checks.append(("frt", t.sla_first_response_due_at))
            if t.sla_solution_due_at:
                checks.append(("solution", t.sla_solution_due_at))
            for kind, due in checks:
                total = (due - t.created_at).total_seconds() or 1
                ratio = (now - t.created_at).total_seconds() / total
                if ratio >= 1 and not flags.get(f"{kind}_breached"):
                    flags[f"{kind}_breached"] = True
                    changed = True
                    await svc.log_event(s, t, "sla_breached", None, new=kind, meta={"due": due.isoformat()})
                    emit(s, "sla.breached", t.organization_id, {"scope": "ticket", "kind": kind, "ticket_id": str(t.id), "agent_id": str(t.assigned_agent_id) if t.assigned_agent_id else None,
                                                                 "department_id": str(t.department_id) if t.department_id else None})
                elif ratio >= settings.sla_at_risk_ratio and not flags.get(f"{kind}_at_risk") and not flags.get(f"{kind}_breached"):
                    flags[f"{kind}_at_risk"] = True
                    changed = True
                    emit(s, "sla.at_risk", t.organization_id, {"scope": "ticket", "kind": kind, "ticket_id": str(t.id), "agent_id": str(t.assigned_agent_id) if t.assigned_agent_id else None})
            if changed:
                t.sla_flags = flags
