"""Automação híbrida (IA + humano): menu/FAQ do bot e orquestrador de transição (handoff) para atendentes."""
from __future__ import annotations

import unicodedata
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.db import utcnow
from app.models import Department, Message, MessageType, Ticket, TicketNote, TicketStatus
from app.modules.core.defaults import is_within_business_hours
from app.modules.whatsapp import llm, service as svc

MENU_ROWS = [
    {"id": "menu:consultoria", "title": "Consultoria técnica", "description": "POPs, validação de painéis, padronização EuroFlow/OMS"},
    {"id": "menu:apoio_diagnostico", "title": "Laudos e diagnóstico", "description": "Processamento de .FCS e laudos de alta complexidade"},
    {"id": "menu:canal_medico", "title": "Canal médico", "description": "Discussão multidisciplinar e interconsultas"},
    {"id": "menu:capacitacao", "title": "Treinamentos", "description": "Escola de citometria e comunidade"},
    {"id": "menu:comercial", "title": "Orçamentos e contratos", "description": "Propostas, planos e valores"},
    {"id": "menu:humano", "title": "Falar com atendente", "description": "Atendimento humano"},
]

INTENT_LABEL = {
    "consultoria": "Consultoria Técnica de Processos e Qualidade", "apoio_diagnostico": "Apoio Diagnóstico Remoto & Laudos",
    "canal_medico": "Canal Médico & Discussão Multidisciplinar", "capacitacao": "Capacitação Técnica & Treinamentos",
    "comercial": "Orçamentos e Contratos", "financeiro": "Financeiro", "humano": "Atendimento humano", "desconhecido": "Atendimento geral",
}
INTENT_DEPT_HINTS = {
    "consultoria": ["consultoria"], "apoio_diagnostico": ["diagn", "laudo", "apoio"], "canal_medico": ["medic"], "capacitacao": ["capacit", "treinam"],
    "comercial": ["comercial", "vendas"], "financeiro": ["financ"],
}
FAQ = {
    "capacitacao": "A Flowmentor oferece a *escola de citometria* e a *comunidade*, com assinaturas anuais e planos institucionais. Posso te conectar a um especialista para detalhar planos e valores.",
    "financeiro": "Para segunda via de boleto, PIX ou nota fiscal, nosso time financeiro precisa confirmar seus dados. Posso te transferir agora?",
}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()


async def _bot_say(session: AsyncSession, t: Ticket, body: str, payload: dict | None = None, type_: MessageType = MessageType.text) -> None:
    await svc.queue_outbound(session, t, body=body, type_=type_, is_bot=True, payload=payload, enforce_window=False)


async def _find_department(session: AsyncSession, org_id: uuid.UUID, intent: str) -> Department | None:
    hints = INTENT_DEPT_HINTS.get(intent, [])
    depts = (await session.execute(sa.select(Department).where(Department.organization_id == org_id))).scalars().all()
    for d in depts:
        if any(h in _norm(d.name) for h in hints):
            return d
    return None


async def send_menu(session: AsyncSession, t: Ticket, cfg: dict, greeting: bool = True) -> None:
    text = cfg["bot"]["greeting"] if greeting else "Escolha uma das opções abaixo para eu direcionar seu atendimento:"
    text += "\n\n_Ao continuar, você concorda com o tratamento dos seus dados para fins de atendimento, conforme a LGPD. Envie *PARAR* para não receber mais mensagens._" if greeting else ""
    await _bot_say(session, t, text, {"kind": "list", "body": text, "button": "Ver opções", "rows": MENU_ROWS, "section_title": "Como podemos ajudar?"}, MessageType.interactive)


async def handoff(session: AsyncSession, t: Ticket, intent: str, reason: str, confidence: float | None = None, user_text: str | None = None) -> None:
    """Passa o atendimento do bot para a fila humana sem perder o contexto (resumo + departamento + skills)."""
    cfg = await svc.org_cfg(session, t.organization_id)
    dept = await _find_department(session, t.organization_id, intent)
    if dept:
        t.department_id = dept.id
        svc.apply_sla(t, dept)
    t.required_skills = [intent] if intent in INTENT_DEPT_HINTS else []
    t.subject = t.subject or INTENT_LABEL.get(intent, "Atendimento geral")
    transcript = await svc.load_transcript(session, t.id)
    t.summary = await llm.summarize_conversation(transcript)
    if t.summary:
        session.add(TicketNote(ticket_id=t.id, organization_id=t.organization_id, author_id=None, kind="note", body=f"🤖 Resumo do bot (handoff — {reason}): {t.summary}"))
    await svc.log_event(session, t, "handoff", None, "bot", "human", {"intent": intent, "reason": reason, "confidence": confidence})
    emit(session, "ticket.handoff", t.organization_id, {"ticket_id": str(t.id), "intent": intent, "reason": reason, "confidence": confidence})
    t.bot_context = {**(t.bot_context or {}), "handoff": {"intent": intent, "reason": reason, "at": utcnow().isoformat()}}
    hours = (dept.business_hours if dept and dept.business_hours else cfg["business_hours"])
    open_now = is_within_business_hours(hours, cfg["timezone"])
    t.after_hours = not open_now
    await svc.set_status(session, t, TicketStatus.queued, None, f"handoff:{reason}")
    if open_now:
        msg = f"Certo! Vou te conectar com um especialista em *{INTENT_LABEL.get(intent, 'atendimento')}*. Aguarde um instante, por favor. 🙂"
    else:
        msg = (dept.after_hours_message if dept and dept.after_hours_message else cfg["bot"]["after_hours_message"])
    await _bot_say(session, t, msg)


async def handle_inbound(session: AsyncSession, t: Ticket, m: Message, selection_id: str | None, cfg: dict) -> None:
    """Processa mensagem do cliente enquanto o ticket está sob responsabilidade do bot."""
    ctx = dict(t.bot_context or {})
    turns = int(ctx.get("turns", 0))
    ctx["turns"] = turns + 1
    t.bot_context = ctx
    text = (m.body or "").strip()
    bot_cfg = cfg["bot"]

    # 1) seleção no menu / botões do bot
    if selection_id:
        if selection_id.startswith("menu:"):
            intent = selection_id.split(":", 1)[1]
            if intent == "humano":
                return await handoff(session, t, "desconhecido", "cliente_pediu_humano")
            if intent in FAQ and not ctx.get("faq_shown"):
                ctx["faq_shown"], ctx["intent"] = True, intent
                t.bot_context = ctx
                return await _ask_resolved(session, t, FAQ[intent])
            return await handoff(session, t, intent, "menu_selecionado", 1.0)
        if selection_id == "bot:resolved":
            await _bot_say(session, t, "Que bom que consegui ajudar! Se precisar de algo mais, é só chamar. 👋")
            return await svc.close_ticket(session, t, None, "resolved_by_bot")
        if selection_id == "bot:human":
            return await handoff(session, t, ctx.get("intent", "desconhecido"), "cliente_pediu_humano")

    # 2) primeira interação: saudação + menu
    if turns == 0 and not text.strip():
        return await send_menu(session, t, cfg)
    if turns == 0:
        intent, conf = await llm.classify_intent(text)
        if any(k in _norm(text) for k in bot_cfg["handoff_keywords"]):
            return await handoff(session, t, intent, "palavra_chave", conf, text)
        if conf >= max(bot_cfg["min_confidence"], 0.7) and intent not in ("desconhecido",):
            return await handoff(session, t, intent, "intencao_clara", conf, text)
        return await send_menu(session, t, cfg)

    # 3) conversa em andamento
    norm = _norm(text)
    if any(k in norm for k in bot_cfg["handoff_keywords"]):
        return await handoff(session, t, ctx.get("intent", "desconhecido"), "palavra_chave")
    intent, conf = await llm.classify_intent(text)
    if conf < bot_cfg["min_confidence"]:
        if turns >= 3:
            return await handoff(session, t, "desconhecido", "baixa_confianca", conf)
        return await send_menu(session, t, cfg, greeting=False)
    if intent in FAQ and not ctx.get("faq_shown"):
        ctx["faq_shown"], ctx["intent"] = True, intent
        t.bot_context = ctx
        return await _ask_resolved(session, t, FAQ[intent])
    await handoff(session, t, intent, "intencao_identificada", conf)


async def _ask_resolved(session: AsyncSession, t: Ticket, body: str) -> None:
    await _bot_say(session, t, body, {"kind": "buttons", "body": body, "buttons": [["bot:resolved", "Resolvido ✅"], ["bot:human", "Falar com atendente"]]}, MessageType.interactive)
