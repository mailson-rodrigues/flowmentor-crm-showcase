"""Motor de roteamento: Round-Robin, Menor Carga (Least Busy) e Skill-based, com afinidade de agente (sticky)."""
from __future__ import annotations

import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scheduler import periodic
from app.db import session_scope, utcnow
from app.models import Department, Organization, Ticket, TicketStatus, User, UserDepartment
from app.modules.core.defaults import effective_settings

AGENT_ROLES = ("agent", "consultor")
ACTIVE_STATUSES = (TicketStatus.in_progress, TicketStatus.pending_client)


async def agent_loads(session: AsyncSession, org_id: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = (await session.execute(
        sa.select(Ticket.assigned_agent_id, sa.func.count()).where(Ticket.organization_id == org_id, Ticket.status.in_(ACTIVE_STATUSES), Ticket.assigned_agent_id.is_not(None))
        .group_by(Ticket.assigned_agent_id))).all()
    return {r[0]: r[1] for r in rows}


async def candidate_agents(session: AsyncSession, org_id: uuid.UUID, dept: Department | None) -> list[User]:
    q = sa.select(User).where(User.organization_id == org_id, User.is_active.is_(True), User.role.in_(AGENT_ROLES), User.status == "available")
    users = list((await session.execute(q)).scalars().all())
    if dept is not None:
        has_members = (await session.execute(sa.select(sa.func.count()).select_from(UserDepartment).where(UserDepartment.department_id == dept.id))).scalar_one() > 0
        if has_members:
            member_ids = set((await session.execute(sa.select(UserDepartment.user_id).where(UserDepartment.department_id == dept.id))).scalars().all())
            users = [u for u in users if u.id in member_ids]
    return users


def _skill_score(user: User, required: list[str]) -> int:
    have = {s.lower() for s in (user.skills or [])}
    return sum(1 for s in required if s.lower() in have)


async def pick_agent(session: AsyncSession, ticket: Ticket, dept: Department | None, loads: dict[uuid.UUID, int] | None = None, exclude: set[uuid.UUID] | None = None) -> User | None:
    org = await session.get(Organization, ticket.organization_id)
    cfg = effective_settings(org.settings)["routing"]
    loads = loads if loads is not None else await agent_loads(session, ticket.organization_id)
    candidates = [u for u in await candidate_agents(session, ticket.organization_id, dept) if loads.get(u.id, 0) < (u.max_concurrent_chats or 5) and u.id not in (exclude or set())]
    if not candidates:
        return None
    # 1) afinidade: mesmo agente que atendeu este contato recentemente
    sticky_h = cfg.get("sticky_agent_hours", 0)
    if sticky_h:
        last = (await session.execute(
            sa.select(Ticket.assigned_agent_id).where(
                Ticket.contact_id == ticket.contact_id, Ticket.id != ticket.id, Ticket.assigned_agent_id.is_not(None),
                Ticket.created_at >= utcnow() - timedelta(hours=sticky_h)).order_by(Ticket.created_at.desc()).limit(1))).scalar_one_or_none()
        if last and any(u.id == last for u in candidates):
            return next(u for u in candidates if u.id == last)

    strategy = (dept.routing_strategy if dept else cfg.get("default_strategy")) or "least_busy"
    required = list(ticket.required_skills or []) or list(dept.required_skills or [] if dept else [])

    if strategy == "skill_based" and required:
        scored = sorted(candidates, key=lambda u: (-_skill_score(u, required), loads.get(u.id, 0), u.status_changed_at or utcnow()))
        return scored[0]
    if strategy == "round_robin" and dept is not None:
        ordered = sorted(candidates, key=lambda u: str(u.id))
        last_id = dept.rr_last_user_id
        nxt = next((u for u in ordered if last_id is None or str(u.id) > str(last_id)), ordered[0])
        dept.rr_last_user_id = nxt.id
        return nxt
    # least_busy (padrão) — empate: quem está há mais tempo livre
    return sorted(candidates, key=lambda u: (loads.get(u.id, 0), u.status_changed_at or utcnow()))[0]


async def dispatch_queue(session: AsyncSession, org_id: uuid.UUID, department_id: uuid.UUID | None = None, limit: int = 100) -> int:
    from app.modules.whatsapp import service as svc

    q = sa.select(Ticket).where(Ticket.organization_id == org_id, Ticket.status == TicketStatus.queued, Ticket.assigned_agent_id.is_(None))
    if department_id:
        q = q.where(Ticket.department_id == department_id)
    prio = sa.case((Ticket.priority == "urgent", 0), (Ticket.priority == "high", 1), (Ticket.priority == "normal", 2), else_=3)
    q = q.order_by(prio, Ticket.queued_at.asc().nulls_last(), Ticket.created_at).limit(limit)
    if session.bind.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True, of=Ticket)  # `of`: o LEFT JOIN de contact não pode ser bloqueado no PostgreSQL
    tickets = (await session.execute(q)).scalars().all()
    if not tickets:
        return 0
    loads = await agent_loads(session, org_id)
    depts = {d.id: d for d in (await session.execute(sa.select(Department).where(Department.organization_id == org_id))).scalars().all()}
    assigned = 0
    for t in tickets:
        agent = await pick_agent(session, t, depts.get(t.department_id), loads)
        if not agent:
            continue
        await svc.assign(session, t, agent, by_user_id=None, reason="auto_routing")
        loads[agent.id] = loads.get(agent.id, 0) + 1
        assigned += 1
    return assigned


async def on_capacity_event(event) -> None:
    """Chamado em: agente ficou disponível, ticket fechado/resolvido, ticket enfileirado/transferido."""
    if event.org_id is None:
        return
    if event.type == "agent.status_changed" and event.payload.get("status") != "available":
        return
    async with session_scope() as s:
        await dispatch_queue(s, uuid.UUID(event.org_id))


@periodic("routing.tick", 15)
async def routing_tick() -> None:
    async with session_scope() as s:
        org_ids = (await s.execute(sa.select(Ticket.organization_id).where(Ticket.status == TicketStatus.queued, Ticket.assigned_agent_id.is_(None)).distinct())).scalars().all()
        for oid in org_ids:
            await dispatch_queue(s, oid)
