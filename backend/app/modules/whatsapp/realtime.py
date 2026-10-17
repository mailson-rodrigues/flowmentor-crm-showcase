"""Tempo real via Socket.IO: sincronização de mensagens, status, digitação, presença e alertas.

Salas:  ``user:{id}`` (notificações pessoais) · ``dept:{id}`` (fila do departamento) · ``sup:{org}`` (supervisores)
        · ``ticket:{id}`` (conversa aberta — entrada validada por ABAC).
"""
from __future__ import annotations

import logging
import uuid

import jwt
import socketio
import sqlalchemy as sa

from app.config import settings
from app.core.events import Event, EventBus
from app.db import session_scope
from app.models import Ticket, User
from app.security.rbac import can_access_ticket, has_permission
from app.security.tokens import decode_access_token

log = logging.getLogger("flowmentor.realtime")

# Com Redis, vários workers/containers compartilham as salas e o processo `worker` consegue emitir para os clientes conectados na API.
_manager = socketio.AsyncRedisManager(settings.redis_url) if settings.redis_url else None
sio = socketio.AsyncServer(async_mode="asgi", client_manager=_manager, cors_allowed_origins=settings.cors_origins or [], logger=False, engineio_logger=False,
                           ping_interval=20, ping_timeout=25)


@sio.event
async def connect(sid, environ, auth):
    token = (auth or {}).get("token")
    if not token:
        raise ConnectionRefusedError("unauthorized")
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise ConnectionRefusedError("invalid_token")
    async with session_scope() as s:
        user = (await s.execute(sa.select(User).where(User.id == uuid.UUID(payload["sub"])))).scalar_one_or_none()
        if not user or not user.is_active:
            raise ConnectionRefusedError("inactive")
        info = {"user_id": str(user.id), "org_id": str(user.organization_id), "role": user.role, "name": user.name, "dept_ids": [str(d) for d in user.department_ids]}
    await sio.save_session(sid, info)
    await sio.enter_room(sid, f"user:{info['user_id']}")
    for d in info["dept_ids"]:
        await sio.enter_room(sid, f"dept:{d}")
    if has_permission(info["role"], "inbox:all") or has_permission(info["role"], "supervisor:live"):
        await sio.enter_room(sid, f"sup:{info['org_id']}")
    await sio.emit("ready", {"user_id": info["user_id"]}, to=sid)


@sio.event
async def join_ticket(sid, data=None, *_):
    info = await sio.get_session(sid)
    data = data or {}
    try:
        tid = uuid.UUID(str(data.get("ticket_id")))
    except ValueError:
        return {"ok": False}
    async with session_scope() as s:
        t = await s.get(Ticket, tid)
        if not t or str(t.organization_id) != info["org_id"] or not can_access_ticket(info["role"], uuid.UUID(info["user_id"]), {uuid.UUID(d) for d in info["dept_ids"]}, t):
            return {"ok": False}
    await sio.enter_room(sid, f"ticket:{tid}")
    return {"ok": True}


@sio.event
async def leave_ticket(sid, data=None, *_):
    await sio.leave_room(sid, f"ticket:{(data or {}).get('ticket_id')}")


@sio.event
async def typing(sid, data=None, *_):
    """Indicador de digitação entre atendentes/supervisores que observam o mesmo ticket."""
    info = await sio.get_session(sid)
    data = data or {}
    room = f"ticket:{data.get('ticket_id')}"
    if room in sio.rooms(sid):
        await sio.emit("typing", {"ticket_id": data.get("ticket_id"), "user_id": info["user_id"], "name": info["name"], "typing": bool(data.get("typing", True))}, room=room, skip_sid=sid)


def _ticket_targets(p: dict, always_dept: bool = False) -> list[str]:
    rooms = [f"sup:{p.get('org_id')}"] if p.get("org_id") else []
    agent, dept = p.get("agent_id"), p.get("department_id")
    if agent:
        rooms.append(f"user:{agent}")
    if dept and (always_dept or not agent or p.get("status") in ("queued", "pending_bot")):
        rooms.append(f"dept:{dept}")
    if not dept and not agent:
        rooms.append(f"sup:{p.get('org_id')}")
    return rooms


async def _emit_many(event_name: str, rooms: list[str], data: dict) -> None:
    for room in dict.fromkeys(rooms):
        await sio.emit(event_name, data, room=room)


async def on_event(event: Event) -> None:
    """Traduz eventos de domínio em pushes de Socket.IO."""
    t, p = event.type, {**event.payload, "org_id": event.org_id}
    if t == "message.created":
        ticket = p.get("ticket", {})
        data = {k: v for k, v in p.items() if k not in ("ticket", "org_id")}
        await sio.emit("message", data, room=f"ticket:{p['ticket_id']}")
        await _emit_many("ticket_updated", _ticket_targets({**ticket, "org_id": event.org_id}), {"ticket": ticket, "reason": "message"})
    elif t == "message.status":
        await sio.emit("message_status", {"message_id": p["message_id"], "ticket_id": p["ticket_id"], "status": p["status"], "error": p.get("error")}, room=f"ticket:{p['ticket_id']}")
    elif t in ("ticket.created", "ticket.updated", "ticket.queued", "ticket.assigned", "ticket.transferred", "ticket.closed", "ticket.resolved", "ticket.handoff"):
        rooms = _ticket_targets(p, always_dept=t in ("ticket.assigned", "ticket.transferred", "ticket.closed"))
        if p.get("previous_agent_id"):
            rooms.append(f"user:{p['previous_agent_id']}")
        if p.get("from_agent_id"):
            rooms.append(f"user:{p['from_agent_id']}")
        if p.get("from_department_id"):
            rooms.append(f"dept:{p['from_department_id']}")
        await _emit_many("ticket_event", rooms, {"type": t, "ticket": {k: v for k, v in p.items() if k != "org_id"}})
        if p.get("ticket_id"):
            await sio.emit("ticket_event", {"type": t, "ticket": {k: v for k, v in p.items() if k != "org_id"}}, room=f"ticket:{p['ticket_id']}")
    elif t == "ticket.note_added":
        await sio.emit("note", {k: v for k, v in p.items() if k != "org_id"}, room=f"ticket:{p['ticket_id']}")
    elif t == "ticket.whisper":
        await sio.emit("whisper", {k: v for k, v in p.items() if k != "org_id"}, room=f"user:{p['target_user_id']}")
    elif t == "ticket.barge_in" and p.get("previous_agent_id"):
        await sio.emit("barge_in", {"ticket_id": p["ticket_id"]}, room=f"user:{p['previous_agent_id']}")
    elif t in ("sla.at_risk", "sla.breached"):
        rooms = [f"sup:{event.org_id}"] + ([f"user:{p['agent_id']}"] if p.get("agent_id") else [])
        await _emit_many("sla_alert", rooms, {"level": "breached" if t == "sla.breached" else "at_risk", **{k: v for k, v in p.items() if k != "org_id"}})
    elif t == "agent.status_changed":
        await sio.emit("agent_status", {k: v for k, v in p.items() if k != "org_id"}, room=f"sup:{event.org_id}")
    elif t in ("survey.answered", "survey.detractor"):
        await sio.emit("survey", {"type": t, **{k: v for k, v in p.items() if k != "org_id"}}, room=f"sup:{event.org_id}")
    elif t in ("deal.won", "deal.stage_changed", "case.created", "report.issued", "template.status_changed"):
        await sio.emit("crm_event", {"type": t, **{k: v for k, v in p.items() if k != "org_id"}}, room=f"sup:{event.org_id}")


def register(bus: EventBus) -> None:
    bus.subscribe("*", on_event, "realtime.push")
