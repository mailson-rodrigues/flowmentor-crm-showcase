"""Automated satisfaction survey inside WhatsApp (CSAT / NPS / CES).

Flow: ``ticket.closed`` → scheduled job (grace window) → interactive message (list/buttons)
→ answer (score) → optional comment (+ sentiment via NLP) → ``survey.answered``.
Anti-spam rules: does not send if the contact answered a survey in the last N days; ignores answers after ``expires_at``.
"""
from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, emit
from app.core.scheduler import job, periodic, schedule_job
from app.db import session_scope, utcnow
from app.models import Contact, MessageType, SatisfactionSurvey, Ticket, TicketStatus
from app.modules.whatsapp import llm, service as svc

CSAT_ROWS = [
    {"id": "{p}:5", "title": "5 - Excelente", "description": "Superou minhas expectativas"},
    {"id": "{p}:4", "title": "4 - Bom", "description": "Atendeu bem"},
    {"id": "{p}:3", "title": "3 - Regular", "description": "Atendeu parcialmente"},
    {"id": "{p}:2", "title": "2 - Ruim", "description": "Abaixo do esperado"},
    {"id": "{p}:1", "title": "1 - Péssimo", "description": "Muito insatisfeito"},
]
CES_ROWS = [
    {"id": "{p}:5", "title": "5 - Muito fácil", "description": "Resolvi sem esforço"},
    {"id": "{p}:4", "title": "4 - Fácil"}, {"id": "{p}:3", "title": "3 - Neutro"},
    {"id": "{p}:2", "title": "2 - Difícil"}, {"id": "{p}:1", "title": "1 - Muito difícil", "description": "Precisei de muito esforço"},
]
SKIP_WORDS = {"pular", "pula", "nao", "nao quero", "sem comentarios", "sem comentario", "nenhum", "-", "n"}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode().strip()


def _rows(template: list[dict], prefix: str) -> list[dict]:
    return [{**r, "id": r["id"].format(p=prefix)} for r in template]


async def is_suppressed(session: AsyncSession, org_id: uuid.UUID, contact_id: uuid.UUID, days: int) -> bool:
    since = utcnow() - timedelta(days=days)
    return (await session.execute(sa.select(SatisfactionSurvey.id).where(
        SatisfactionSurvey.organization_id == org_id, SatisfactionSurvey.contact_id == contact_id,
        SatisfactionSurvey.answered_at.is_not(None), SatisfactionSurvey.answered_at >= since).limit(1))).first() is not None


async def on_ticket_closed(event: Event) -> None:
    """Schedules the survey after the grace window (if eligible)."""
    org_id, tid = uuid.UUID(event.org_id), uuid.UUID(event.payload["ticket_id"])
    async with session_scope() as s:
        cfg = (await svc.org_cfg(s, org_id))["survey"]
        t = await s.get(Ticket, tid)
        if not cfg["enabled"] or t is None or t.assigned_agent_id is None:  # only conversations handled by a human
            return
        await s.refresh(t, ["contact"])
        if t.contact.opted_out_at or t.contact.anonymized_at:
            return
        if await is_suppressed(s, org_id, t.contact_id, cfg["suppress_days"]):
            await svc.log_event(s, t, "survey_suppressed", None, meta={"reason": f"respondeu há menos de {cfg['suppress_days']} dias"})
            return
        await schedule_job(s, "survey.send", {"ticket_id": str(tid)}, utcnow() + timedelta(minutes=cfg["grace_minutes"]), org_id, dedupe_key=f"survey:{tid}")


@job("survey.send")
async def send_survey_job(payload: dict) -> None:
    tid = uuid.UUID(payload["ticket_id"])
    async with session_scope() as s:
        t = await s.get(Ticket, tid)
        if t is None or t.status != TicketStatus.closed:
            return  # reopened during the grace window
        await s.refresh(t, ["contact"])
        cfg = (await svc.org_cfg(s, t.organization_id))["survey"]
        if t.last_inbound_at and t.closed_at and t.last_inbound_at > t.closed_at:
            return
        if (await s.execute(sa.select(SatisfactionSurvey.id).where(SatisfactionSurvey.ticket_id == tid))).first():
            return
        survey = SatisfactionSurvey(organization_id=t.organization_id, ticket_id=tid, contact_id=t.contact_id, agent_id=t.assigned_agent_id, survey_type=cfg["type"],
                                    state="awaiting_rating", expires_at=utcnow() + timedelta(hours=cfg["ttl_hours"]))
        s.add(survey)
        await s.flush()
        intro = cfg.get("intro") or "Como foi seu atendimento?"
        if cfg["type"] == "nps":
            body = f"{intro}\n\nEm uma escala de 0 a 10, o quanto você recomendaria a Flowmentor a um colega?"
            payload_ = {"kind": "buttons", "body": body, "buttons": [[f"svy:{survey.id}:r:0-6", "0 a 6"], [f"svy:{survey.id}:r:7-8", "7 a 8"], [f"svy:{survey.id}:r:9-10", "9 a 10"]]}
        elif cfg["type"] == "ces":
            body = f"{intro}\n\nO quão fácil foi resolver sua solicitação?"
            payload_ = {"kind": "list", "body": body, "button": "Responder", "rows": _rows(CES_ROWS, f"svy:{survey.id}"), "section_title": "Esforço"}
        else:
            body = f"{intro}\n\nDe 1 a 5, qual nota você dá ao atendimento?"
            payload_ = {"kind": "list", "body": body, "button": "Avaliar", "rows": _rows(CSAT_ROWS, f"svy:{survey.id}"), "section_title": "Sua avaliação"}
        try:
            await svc.queue_outbound(s, t, body=body, type_=MessageType.interactive, is_bot=True, payload=payload_)
        except Exception:  # 24h window closed, opt-out, etc.
            survey.state, survey.expires_at = "expired", utcnow()
            return
        t.survey_state = "awaiting_rating"
        await svc.log_event(s, t, "survey_sent", None, new=cfg["type"])
        emit(s, "survey.sent", t.organization_id, {"survey_id": str(survey.id), "ticket_id": str(tid), "type": cfg["type"]})


async def _pending_survey(session: AsyncSession, org_id: uuid.UUID, contact_id: uuid.UUID) -> SatisfactionSurvey | None:
    return (await session.execute(
        sa.select(SatisfactionSurvey).where(SatisfactionSurvey.organization_id == org_id, SatisfactionSurvey.contact_id == contact_id,
                                            SatisfactionSurvey.state.in_(["awaiting_rating", "awaiting_nps_value", "awaiting_comment"]))
        .order_by(SatisfactionSurvey.sent_at.desc()).limit(1))).scalar_one_or_none()


async def _say(session: AsyncSession, survey: SatisfactionSurvey, body: str, payload: dict | None = None, type_: MessageType = MessageType.text) -> None:
    t = await session.get(Ticket, survey.ticket_id)
    await session.refresh(t, ["contact"])
    try:
        await svc.queue_outbound(session, t, body=body, type_=type_, is_bot=True, payload=payload)
    except Exception:  # noqa: BLE001
        pass


def _parse_rating(survey: SatisfactionSurvey, selection_id: str | None, text: str | None) -> tuple[str, int | tuple[int, int]] | None:
    """Returns ('rating', n) | ('range', (lo, hi)) | ('skip', 0) or None if the message is not an answer."""
    if selection_id and selection_id.startswith(f"svy:{survey.id}:"):
        rest = selection_id.split(":", 3)[2:]
        if rest[0] == "r":
            lo, hi = map(int, rest[1].split("-"))
            return "range", (lo, hi)
        if rest[0].isdigit():
            return "rating", int(rest[0])
        return None
    if text:
        m = re.fullmatch(r"\s*(\d{1,2})\s*", text)
        if m:
            n = int(m.group(1))
            lo, hi = (0, 10) if survey.survey_type == "nps" else (1, 5)
            if lo <= n <= hi:
                return "rating", n
    return None


async def try_handle_reply(session: AsyncSession, org_id: uuid.UUID, contact: Contact, *, selection_id: str | None, text: str | None) -> bool:
    """Returns True if the message was consumed as a survey answer (does not open a new ticket)."""
    survey = await _pending_survey(session, org_id, contact.id)
    if survey is None:
        return False
    now = utcnow()
    if survey.expires_at < now:
        survey.state = "expired"  # late answer is ignored → continues as a normal message
        return False
    t = await session.get(Ticket, survey.ticket_id)

    if survey.state == "awaiting_comment":
        comment = (text or "").strip()
        if not comment and not selection_id:
            return False
        if _norm(comment) not in SKIP_WORDS and comment:
            survey.comment = comment[:2000]
            survey.sentiment = await llm.classify_sentiment(comment)
        survey.state = "completed"
        t.survey_state = "completed"
        await _say(session, survey, "Obrigado pelo seu feedback! 💙 Isso nos ajuda a melhorar.")
        emit(session, "survey.completed", org_id, {"survey_id": str(survey.id), "ticket_id": str(survey.ticket_id), "sentiment": survey.sentiment})
        return True

    parsed = _parse_rating(survey, selection_id, text)
    if parsed is None:
        return False  # free text outside the flow → treat as a new message
    kind, val = parsed
    cfg = (await svc.org_cfg(session, org_id))["survey"]
    if kind == "range":  # NPS step 2: exact value within the range
        lo, hi = val
        rows = [{"id": f"svy:{survey.id}:{n}", "title": str(n), "description": {0: "Nada provável", 10: "Com certeza recomendaria"}.get(n)} for n in range(lo, hi + 1)]
        survey.state = "awaiting_nps_value"
        await _say(session, survey, "Qual nota exata?", {"kind": "list", "body": "Qual nota exata?", "button": "Escolher nota", "rows": rows, "section_title": "Nota"}, MessageType.interactive)
        return True
    rating = int(val)
    survey.rating, survey.answered_at = rating, now
    survey.state = "awaiting_comment" if cfg.get("ask_comment", True) else "completed"
    t.survey_state = survey.state
    detractor = (survey.survey_type == "nps" and rating <= 6) or (survey.survey_type != "nps" and rating <= 2)
    emit(session, "survey.answered", org_id, {"survey_id": str(survey.id), "ticket_id": str(survey.ticket_id), "agent_id": str(survey.agent_id) if survey.agent_id else None,
                                              "type": survey.survey_type, "rating": rating, "detractor": detractor})
    if detractor:
        emit(session, "survey.detractor", org_id, {"survey_id": str(survey.id), "ticket_id": str(survey.ticket_id), "rating": rating,
                                                    "agent_id": str(survey.agent_id) if survey.agent_id else None})
    if survey.state == "awaiting_comment":
        await _say(session, survey, "Obrigado pela avaliação! Se quiser, conte em uma mensagem o motivo da sua nota (opcional). Para pular, responda *PULAR*.")
    else:
        await _say(session, survey, "Obrigado pela avaliação! 💙")
    return True


@periodic("surveys.expire", 600)
async def expire_surveys() -> None:
    async with session_scope() as s:
        await s.execute(sa.update(SatisfactionSurvey).where(SatisfactionSurvey.state.in_(["awaiting_rating", "awaiting_nps_value", "awaiting_comment"]),
                                                            SatisfactionSurvey.expires_at < utcnow()).values(state="expired"))
