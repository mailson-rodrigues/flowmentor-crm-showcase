"""Asynchronous processing of Meta webhooks (consumer of ``whatsapp.webhook.received``)."""
from __future__ import annotations

import logging
import unicodedata
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import Event, emit
from app.db import session_scope, utcnow
from app.models import (
    LgpdConsent, Message, MessageDirection, MessageStatus, MessageType, Ticket, TicketStatus, WaTemplate, WhatsAppAccount, TemplateStatus,
)
from app.modules.core.defaults import is_within_business_hours
from app.modules.core.router import get_or_create_contact
from app.modules.whatsapp import bot, media, service as svc, surveys
from app.modules.whatsapp.meta_client import MetaClient
from app.modules.whatsapp.outbound import apply_status
from app.modules.whatsapp.routing import candidate_agents
from app.security.masking import scrub_text

log = logging.getLogger("flowmentor.processor")

OPT_OUT_WORDS = {"parar", "stop", "sair", "descadastrar", "pare"}
MEDIA_TYPES = {"image": MessageType.image, "audio": MessageType.audio, "video": MessageType.video, "document": MessageType.document, "sticker": MessageType.image, "voice": MessageType.audio}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode().strip()


def parse_message(msg: dict) -> dict:
    """Normalizes the Cloud API message object into (type, text, interactive selection, media)."""
    mtype = msg.get("type", "text")
    out = {"type": MessageType.text, "body": None, "selection_id": None, "media": None, "payload": {}}
    if mtype == "text":
        out["body"] = msg.get("text", {}).get("body")
    elif mtype == "interactive":
        inter = msg.get("interactive", {})
        rep = inter.get("button_reply") or inter.get("list_reply") or {}
        out.update(type=MessageType.text, body=rep.get("title"), selection_id=rep.get("id"), payload={"interactive_reply": rep})
    elif mtype == "button":
        b = msg.get("button", {})
        out.update(body=b.get("text"), selection_id=b.get("payload"), payload={"button": b})
    elif mtype in MEDIA_TYPES:
        obj = msg.get(mtype, {})
        out.update(type=MEDIA_TYPES[mtype], body=obj.get("caption") or f"[{mtype}]", media={"id": obj.get("id"), "mime": obj.get("mime_type"), "filename": obj.get("filename")})
    elif mtype == "location":
        loc = msg.get("location", {})
        out.update(type=MessageType.location, body=f"📍 {loc.get('name') or 'Localização'} ({loc.get('latitude')}, {loc.get('longitude')})", payload={"location": loc})
    elif mtype == "reaction":
        out.update(body=f"{msg.get('reaction', {}).get('emoji', '')} (reação)", payload={"reaction": msg.get("reaction")})
    else:
        out["body"] = f"[mensagem do tipo '{mtype}' não suportada]"
    return out


async def handle_webhook(event: Event) -> None:
    payload = event.payload.get("payload", {})
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field, value = change.get("field"), change.get("value", {})
            try:
                if field == "messages":
                    await process_messages_value(value)
                elif field == "message_template_status_update":
                    await process_template_status(value)
                elif field == "phone_number_quality_update":
                    await process_quality_update(value)
            except Exception:  # noqa: BLE001
                log.exception("falha ao processar change %s", field)
                raise


async def _account_by_phone(session: AsyncSession, phone_number_id: str) -> WhatsAppAccount | None:
    return (await session.execute(sa.select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == phone_number_id))).scalar_one_or_none()


async def process_messages_value(value: dict) -> None:
    phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
    names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
    for st in value.get("statuses", []):
        async with session_scope() as s:
            await apply_status(s, st["id"], st["status"], (st.get("errors") or [None])[0])
    for msg in value.get("messages", []):
        async with session_scope() as s:  # one transaction per message: a failure does not lose the others
            wa = await _account_by_phone(s, phone_number_id)
            if wa is None:
                log.warning("webhook para phone_number_id desconhecido: %s", phone_number_id)
                continue
            await process_inbound(s, wa, msg, names.get(msg.get("from")))


async def process_inbound(session: AsyncSession, wa: WhatsAppAccount, msg: dict, profile_name: str | None) -> Ticket | None:
    org_id = wa.organization_id
    wamid = msg["id"]
    if (await session.execute(sa.select(Message.id).where(Message.meta_message_id == wamid, Message.organization_id == org_id).limit(1))).first():
        return None  # idempotency (Meta redelivers)
    parsed = parse_message(msg)
    wa_id = msg["from"]
    contact, created = await get_or_create_contact(session, org_id, wa_id, profile_name)
    if contact.anonymized_at:  # titular anonimizado voltou a escrever → trata como novo contato
        contact.anonymized_at, contact.name = None, profile_name
    contact.last_interaction_at = utcnow()
    text, selection = parsed["body"], parsed["selection_id"]
    cfg = await svc.org_cfg(session, org_id)

    # --- LGPD: implicit opt-in (initiative of the data subject) and keyword opt-out
    if created or not (await session.execute(sa.select(LgpdConsent.id).where(LgpdConsent.contact_id == contact.id, LgpdConsent.revoked_at.is_(None)).limit(1))).first():
        session.add(LgpdConsent(organization_id=org_id, contact_id=contact.id, legal_basis="Consentimento (Art. 7, I)", consent_source="whatsapp_optin",
                                purpose="Atendimento, suporte e comunicações operacionais sobre serviços contratados via WhatsApp"))
    if text and _norm(text) in OPT_OUT_WORDS and not selection:
        contact.opted_out_at = utcnow()
        await session.execute(sa.update(LgpdConsent).where(LgpdConsent.contact_id == contact.id, LgpdConsent.revoked_at.is_(None)).values(revoked_at=utcnow()))
        client = MetaClient(wa.access_token, wa.phone_number_id, wa.waba_id)
        try:
            await client.send_text(wa_id, "Pronto! Você não receberá mais mensagens da Flowmentor. Para voltar a conversar, basta nos enviar uma mensagem.")
        except Exception:  # noqa: BLE001
            log.warning("falha ao confirmar opt-out", exc_info=True)
        emit(session, "contact.opted_out", org_id, {"contact_id": str(contact.id)})
        return None
    if contact.opted_out_at:  # voltou a escrever → reativa
        contact.opted_out_at = None

    # --- Satisfaction survey: replies do not open a ticket
    if await surveys.try_handle_reply(session, org_id, contact, selection_id=selection, text=text):
        return None

    # --- Localiza/abre ticket
    t = await svc.find_open_ticket(session, org_id, contact.id)
    if t is not None:
        await session.refresh(t, ["contact"])
    if t is not None and t.status == TicketStatus.resolved:
        await svc.reopen_ticket(session, t)
    if t is None:
        prev = await svc.mark_previous_not_fcr(session, org_id, contact.id)
        t = await svc.create_ticket(session, org_id, contact, wa, reopened_from=prev)

    m = await svc.add_message(session, t, direction=MessageDirection.inbound, type_=parsed["type"], body=text, meta_message_id=wamid,
                              payload={**parsed["payload"], "wamid": wamid, "timestamp": msg.get("timestamp")})
    if parsed["media"] and parsed["media"].get("id"):
        try:
            client = MetaClient(wa.access_token, wa.phone_number_id, wa.waba_id)
            asset = await media.download_inbound(session, client, org_id, t.id, parsed["media"]["id"], parsed["media"]["mime"], parsed["media"]["filename"])
            m.media_asset_id = asset.id
        except Exception as exc:  # noqa: BLE001
            log.warning("falha ao baixar mídia %s: %s", parsed["media"]["id"], exc)
            m.error = f"mídia indisponível: {str(exc)[:120]}"

    if t.status == TicketStatus.pending_client:
        await svc.set_status(session, t, TicketStatus.in_progress, None, "customer_replied")

    if t.bot_active and t.status == TicketStatus.pending_bot:
        await bot.handle_inbound(session, t, m, selection, cfg)
    elif t.status == TicketStatus.queued and t.assigned_agent_id is None:
        await _after_hours_notice(session, t, cfg)
    return t


async def _after_hours_notice(session: AsyncSession, t: Ticket, cfg: dict) -> None:
    """Out-of-hours contingency: warns once when no agent is available."""
    if t.bot_context.get("after_hours_notified"):
        return
    from app.models import Department

    dept = await session.get(Department, t.department_id) if t.department_id else None
    hours = dept.business_hours if dept and dept.business_hours else cfg["business_hours"]
    if is_within_business_hours(hours, cfg["timezone"]):
        return
    if await candidate_agents(session, t.organization_id, dept):
        return
    t.after_hours = True
    t.bot_context = {**(t.bot_context or {}), "after_hours_notified": True}
    msg = dept.after_hours_message if dept and dept.after_hours_message else cfg["bot"]["after_hours_message"]
    await svc.queue_outbound(session, t, body=msg, is_bot=True, enforce_window=False)


async def process_template_status(value: dict) -> None:
    async with session_scope() as s:
        meta_id = str(value.get("message_template_id", ""))
        q = sa.select(WaTemplate).where(WaTemplate.meta_template_id == meta_id) if meta_id else sa.select(WaTemplate).where(
            WaTemplate.name == value.get("message_template_name"), WaTemplate.language == value.get("message_template_language"))
        tpl = (await s.execute(q.limit(1))).scalar_one_or_none()
        if tpl is None:
            return
        mapping = {"APPROVED": TemplateStatus.approved, "REJECTED": TemplateStatus.rejected, "PENDING": TemplateStatus.pending, "PAUSED": TemplateStatus.paused,
                   "DISABLED": TemplateStatus.disabled, "FLAGGED": TemplateStatus.paused}
        new = mapping.get(str(value.get("event", "")).upper())
        if new:
            tpl.status = new
            tpl.rejection_reason = value.get("reason") if new == TemplateStatus.rejected else None
            emit(s, "template.status_changed", tpl.organization_id, {"template_id": str(tpl.id), "name": tpl.name, "status": new.value, "reason": tpl.rejection_reason})


async def process_quality_update(value: dict) -> None:
    async with session_scope() as s:
        wa = await _account_by_phone(s, value.get("phone_number_id", "")) or (await s.execute(sa.select(WhatsAppAccount).where(
            WhatsAppAccount.display_phone_number == value.get("display_phone_number")))).scalar_one_or_none()
        if wa is None:
            return
        wa.quality_rating = {"FLAGGED": "RED", "DOWNGRADE": "YELLOW", "UPGRADE": "GREEN", "ONBOARDING": "GREEN"}.get(str(value.get("event", "")).upper(), wa.quality_rating)
        wa.messaging_limit = value.get("current_limit", wa.messaging_limit)
