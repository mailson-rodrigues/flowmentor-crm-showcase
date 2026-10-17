"""Automated KPIs: FRT, AHT, FCR, throughput, CSAT/NPS/CES, agent occupancy and SLA.

Implemented on the transactional database using the *snapshots* stored in ``tickets`` (without scanning messages).
At larger volumes, ``KpiStore`` can be replaced by a ClickHouse/Elasticsearch implementation fed by the
event bus — the HTTP API stays identical.
"""
from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import Principal, require
from app.db import get_session, utcnow
from app.models import AgentPresenceLog, Message, MessageDirection, SatisfactionSurvey, Ticket, TicketEvent, TicketStatus, User

router = APIRouter(prefix="/api/v1/kpis", tags=["KPIs"])


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[f] + (s[c] - s[f]) * (k - f), 1)


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    total, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return total


def _bucket(dt: datetime, hourly: bool) -> str:
    return dt.strftime("%Y-%m-%d %H:00") if hourly else dt.strftime("%Y-%m-%d")


def _range(date_from: date | None, date_to: date | None) -> tuple[datetime, datetime]:
    end_d = date_to or utcnow().date()
    start_d = date_from or (end_d - timedelta(days=29))
    return datetime.combine(start_d, time.min, tzinfo=timezone.utc), datetime.combine(end_d, time.max, tzinfo=timezone.utc)


async def compute_overview(session: AsyncSession, org_id: uuid.UUID, start: datetime, end: datetime, department_id: uuid.UUID | None = None, agent_id: uuid.UUID | None = None) -> dict:
    now = utcnow()
    hourly = (end - start) <= timedelta(days=2)
    tq = sa.select(Ticket).where(Ticket.organization_id == org_id, Ticket.created_at >= start, Ticket.created_at <= end)
    if department_id:
        tq = tq.where(Ticket.department_id == department_id)
    if agent_id:
        tq = tq.where(Ticket.assigned_agent_id == agent_id)
    tickets = (await session.execute(tq)).scalars().all()
    tids = [t.id for t in tickets]

    frt = [t.first_response_time_seconds for t in tickets if t.first_response_time_seconds is not None]
    aht = [t.handling_time_seconds for t in tickets if t.handling_time_seconds is not None and t.status == TicketStatus.closed]
    waits = [t.wait_time_seconds for t in tickets if t.wait_time_seconds is not None]
    closed = [t for t in tickets if t.status == TicketStatus.closed]
    human_closed = [t for t in closed if t.assigned_agent_id]
    fcr = round(100 * sum(1 for t in human_closed if t.is_fcr) / len(human_closed), 1) if human_closed else None
    met = [t for t in tickets if t.first_response_time_seconds is not None and t.sla_first_response_due_at]
    sla_ok = sum(1 for t in met if t.first_response_time_seconds <= (t.sla_first_response_due_at - t.created_at).total_seconds())
    breaches = sum(1 for t in tickets if (t.sla_flags or {}).get("frt_breached") or (t.sla_flags or {}).get("solution_breached"))

    # Throughput per bucket
    series: dict[str, dict[str, int]] = defaultdict(lambda: {"created": 0, "closed": 0, "transferred": 0, "messages_in": 0, "messages_out": 0})
    for t in tickets:
        series[_bucket(t.created_at, hourly)]["created"] += 1
    cq = sa.select(Ticket.closed_at).where(Ticket.organization_id == org_id, Ticket.closed_at >= start, Ticket.closed_at <= end)
    if department_id:
        cq = cq.where(Ticket.department_id == department_id)
    if agent_id:
        cq = cq.where(Ticket.assigned_agent_id == agent_id)
    for (c,) in (await session.execute(cq)).all():
        series[_bucket(c, hourly)]["closed"] += 1
    if tids:
        ev = (await session.execute(sa.select(TicketEvent.created_at).where(TicketEvent.ticket_id.in_(tids), TicketEvent.event_type == "transferred"))).scalars().all()
        for c in ev:
            series[_bucket(c, hourly)]["transferred"] += 1
    mq = sa.select(Message.created_at, Message.direction).where(Message.organization_id == org_id, Message.created_at >= start, Message.created_at <= end)
    if tids and (department_id or agent_id):
        mq = mq.where(Message.ticket_id.in_(tids))
    for c, d in (await session.execute(mq)).all():
        series[_bucket(c, hourly)]["messages_in" if d == MessageDirection.inbound else "messages_out"] += 1
    throughput = [{"bucket": k, **v} for k, v in sorted(series.items())]

    # Pesquisas
    sq = sa.select(SatisfactionSurvey).where(SatisfactionSurvey.organization_id == org_id, SatisfactionSurvey.sent_at >= start, SatisfactionSurvey.sent_at <= end)
    if agent_id:
        sq = sq.where(SatisfactionSurvey.agent_id == agent_id)
    surveys = (await session.execute(sq)).scalars().all()
    if department_id:
        tset = set(tids)
        surveys = [s for s in surveys if s.ticket_id in tset]
    answered = [s for s in surveys if s.rating is not None]
    csat = [s.rating for s in answered if s.survey_type == "csat"]
    nps = [s.rating for s in answered if s.survey_type == "nps"]
    ces = [s.rating for s in answered if s.survey_type == "ces"]
    prom, det = sum(1 for r in nps if r >= 9), sum(1 for r in nps if r <= 6)
    sentiments = defaultdict(int)
    for s in answered:
        if s.sentiment:
            sentiments[s.sentiment] += 1
    quality = {
        "surveys_sent": len(surveys), "surveys_answered": len(answered), "response_rate": round(100 * len(answered) / len(surveys), 1) if surveys else None,
        "csat_avg": round(sum(csat) / len(csat), 2) if csat else None, "csat_satisfied_pct": round(100 * sum(1 for r in csat if r >= 4) / len(csat), 1) if csat else None,
        "nps": round(100 * (prom - det) / len(nps)) if nps else None, "nps_promoters": prom, "nps_detractors": det, "nps_passives": len(nps) - prom - det,
        "ces_avg": round(sum(ces) / len(ces), 2) if ces else None, "sentiment": dict(sentiments),
        "rating_distribution": {str(k): sum(1 for r in csat if r == k) for k in range(1, 6)},
    }

    # Occupancy and per-agent table
    users = {u.id: u for u in (await session.execute(sa.select(User).where(User.organization_id == org_id, User.role.in_(["agent", "consultor", "supervisor"])))).scalars().all()}
    plogs = (await session.execute(sa.select(AgentPresenceLog).where(AgentPresenceLog.organization_id == org_id, AgentPresenceLog.started_at <= end,
                                                                        sa.or_(AgentPresenceLog.ended_at.is_(None), AgentPresenceLog.ended_at >= start)))).scalars().all()
    logged, paused = defaultdict(float), defaultdict(float)
    for pl in plogs:
        s, e = max(pl.started_at, start), min(pl.ended_at or now, end, now)
        sec = max((e - s).total_seconds(), 0)
        if pl.status in ("available", "busy"):
            logged[pl.user_id] += sec
        elif pl.status == "paused":
            logged[pl.user_id] += sec
            paused[pl.user_id] += sec
    by_agent_t: dict[uuid.UUID, list[Ticket]] = defaultdict(list)
    for t in tickets:
        if t.assigned_agent_id:
            by_agent_t[t.assigned_agent_id].append(t)
    surveys_by_agent: dict[uuid.UUID, list[int]] = defaultdict(list)
    for s in answered:
        if s.survey_type == "csat" and s.agent_id:
            surveys_by_agent[s.agent_id].append(s.rating)
    agents = []
    for uid, u in users.items():
        ts = by_agent_t.get(uid, [])
        if not ts and not logged.get(uid):
            continue
        intervals = [(max(t.assigned_at, start), min(t.closed_at or now, end)) for t in ts if t.assigned_at and min(t.closed_at or now, end) > max(t.assigned_at, start)]
        conv = _union_seconds(intervals)
        log_s = logged.get(uid, 0.0)
        a_frt = [t.first_response_time_seconds for t in ts if t.first_response_time_seconds is not None]
        a_aht = [t.handling_time_seconds for t in ts if t.handling_time_seconds is not None]
        agents.append({
            "agent_id": str(uid), "name": u.name, "tickets": len(ts), "closed": sum(1 for t in ts if t.status == TicketStatus.closed), "avg_frt_seconds": _avg(a_frt), "avg_aht_seconds": _avg(a_aht),
            "csat_avg": round(sum(surveys_by_agent[uid]) / len(surveys_by_agent[uid]), 2) if surveys_by_agent.get(uid) else None,
            "logged_seconds": int(log_s), "paused_seconds": int(paused.get(uid, 0)), "conversation_seconds": int(min(conv, log_s) if log_s else conv),
            "occupancy_pct": round(100 * min(conv, log_s) / log_s, 1) if log_s else None,
        })
    agents.sort(key=lambda a: -a["tickets"])

    by_dept = defaultdict(list)
    for t in tickets:
        by_dept[t.department_id].append(t)
    from app.models import Department

    dnames = {d.id: d.name for d in (await session.execute(sa.select(Department).where(Department.organization_id == org_id))).scalars().all()}
    departments = [{"department_id": str(k) if k else None, "name": dnames.get(k, "Sem departamento"), "tickets": len(v),
                    "avg_frt_seconds": _avg([t.first_response_time_seconds for t in v if t.first_response_time_seconds is not None]),
                    "avg_aht_seconds": _avg([t.handling_time_seconds for t in v if t.handling_time_seconds is not None])} for k, v in by_dept.items()]
    status_counts = defaultdict(int)
    for t in tickets:
        status_counts[t.status.value] += 1

    return {
        "period": {"from": start.isoformat(), "to": end.isoformat(), "granularity": "hour" if hourly else "day"},
        "totals": {"tickets": len(tickets), "closed": len(closed), "open": sum(1 for t in tickets if t.status != TicketStatus.closed), "by_status": dict(status_counts),
                   "bot_resolved": sum(1 for t in closed if not t.assigned_agent_id), "sla_breaches": breaches},
        "speed": {"frt_avg_seconds": _avg(frt), "frt_median_seconds": _pct(frt, .5), "frt_p90_seconds": _pct(frt, .9), "wait_avg_seconds": _avg(waits)},
        "resolution": {"aht_avg_seconds": _avg(aht), "aht_median_seconds": _pct(aht, .5), "fcr_pct": fcr},
        "sla": {"frt_compliance_pct": round(100 * sla_ok / len(met), 1) if met else None, "measured": len(met), "breaches": breaches},
        "throughput": throughput, "quality": quality, "agents": agents, "departments": departments,
    }


@router.get("/overview")
async def overview(date_from: date | None = None, date_to: date | None = None, department_id: uuid.UUID | None = None, agent_id: uuid.UUID | None = None,
                   p: Principal = Depends(require("kpi:view")), session: AsyncSession = Depends(get_session)):
    start, end = _range(date_from, date_to)
    if not p.can("inbox:all") and p.user_id:  # ABAC: consultants/agents only see their own indicators
        agent_id = p.user_id
    return await compute_overview(session, p.org_id, start, end, department_id, agent_id)


@router.get("/surveys")
async def survey_responses(date_from: date | None = None, date_to: date | None = None, sentiment: str | None = None, max_rating: int | None = None, limit: int = Query(100, le=500),
                           p: Principal = Depends(require("kpi:view")), session: AsyncSession = Depends(get_session)):
    """Lists ratings and comments (detractors first) for recovery action."""
    from app.models import Contact

    start, end = _range(date_from, date_to)
    q = (sa.select(SatisfactionSurvey, Contact, User).join(Contact, Contact.id == SatisfactionSurvey.contact_id).outerjoin(User, User.id == SatisfactionSurvey.agent_id)
         .where(SatisfactionSurvey.organization_id == p.org_id, SatisfactionSurvey.sent_at >= start, SatisfactionSurvey.sent_at <= end, SatisfactionSurvey.rating.is_not(None)))
    if sentiment:
        q = q.where(SatisfactionSurvey.sentiment == sentiment)
    if max_rating is not None:
        q = q.where(SatisfactionSurvey.rating <= max_rating)
    rows = (await session.execute(q.order_by(SatisfactionSurvey.rating.asc(), SatisfactionSurvey.answered_at.desc()).limit(limit))).all()
    from app.security.masking import mask_phone

    return {"items": [{"id": str(s.id), "ticket_id": str(s.ticket_id), "type": s.survey_type, "rating": s.rating, "comment": s.comment, "sentiment": s.sentiment,
                       "answered_at": s.answered_at.isoformat() if s.answered_at else None, "contact_name": c.name or mask_phone(c.wa_id), "agent_name": u.name if u else None} for s, c, u in rows]}
