"""Envio assíncrono de mensagens à Meta (consumidor de ``message.outbound_queued``) e atualização de status."""
from __future__ import annotations

import asyncio
import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.events import Event, emit
from app.db import session_scope, utcnow
from app.models import MediaAsset, Message, MessageStatus, MessageType, Ticket, WhatsAppAccount
from app.modules.whatsapp.media import public_asset_url
from app.modules.whatsapp.meta_client import MetaClient, MetaError

log = logging.getLogger("flowmentor.outbound")

STATUS_RANK = {"received": 0, "queued": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 9}


async def client_for(session: AsyncSession, ticket: Ticket) -> tuple[MetaClient, WhatsAppAccount | None]:
    wa = await session.get(WhatsAppAccount, ticket.whatsapp_account_id) if ticket.whatsapp_account_id else None
    if wa is None:
        wa = (await session.execute(sa.select(WhatsAppAccount).where(WhatsAppAccount.organization_id == ticket.organization_id, WhatsAppAccount.status == "active").limit(1))).scalar_one_or_none()
    if wa is None:
        return MetaClient(), None
    return MetaClient(wa.access_token, wa.phone_number_id, wa.waba_id), wa


async def _dispatch(session: AsyncSession, client: MetaClient, m: Message, to: str) -> str:
    p = m.payload or {}
    if m.type == MessageType.template:
        return await client.send_template(to, p["template"]["name"], p["template"]["language"], p["template"].get("components"))
    if m.type == MessageType.interactive:
        if p.get("kind") == "list":
            return await client.send_interactive_list(to, p["body"], p.get("button", "Opções"), p["rows"], p.get("section_title", "Opções"), p.get("header"), p.get("footer"))
        return await client.send_interactive_buttons(to, p["body"], [tuple(b) for b in p["buttons"]], p.get("header"), p.get("footer"))
    if m.type in (MessageType.image, MessageType.audio, MessageType.video, MessageType.document):
        asset = await session.get(MediaAsset, m.media_asset_id)
        return await client.send_media(to, m.type.value, public_asset_url(asset), m.body, asset.filename)
    return await client.send_text(to, m.body or "")


async def send_outbound(event: Event) -> None:
    mid = uuid.UUID(event.payload["message_id"])
    async with session_scope() as s:
        m = await s.get(Message, mid)
        if m is None or m.status != MessageStatus.queued:
            return
        t = await s.get(Ticket, m.ticket_id)
        await s.refresh(t, ["contact"])
        client, wa = await client_for(s, t)
        last_err: str | None = None
        for attempt in range(3):
            try:
                m.meta_message_id = await _dispatch(s, client, m, t.contact.wa_id)
                m.status, m.error = MessageStatus.sent, None
                break
            except MetaError as exc:
                last_err = f"{exc.code or ''} {exc}"[:400]
                if not exc.retryable:
                    break
                await asyncio.sleep(0.5 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                log.exception("falha ao enviar mensagem %s", mid)
                last_err = str(exc)[:400]
                break
        if m.status != MessageStatus.sent:
            m.status, m.error = MessageStatus.failed, last_err
        emit(s, "message.status", m.organization_id, {"message_id": str(m.id), "ticket_id": str(m.ticket_id), "status": m.status.value, "error": m.error})
        if m.status == MessageStatus.sent and settings.meta_mode == "sandbox":
            m.status, m.delivered_at = MessageStatus.delivered, utcnow()  # sandbox: entrega instantânea
            emit(s, "message.status", m.organization_id, {"message_id": str(m.id), "ticket_id": str(m.ticket_id), "status": "delivered"})


async def apply_status(session: AsyncSession, wamid: str, status: str, error: dict | None = None) -> None:
    m = (await session.execute(sa.select(Message).where(Message.meta_message_id == wamid).limit(1))).scalar_one_or_none()
    if m is None:
        return
    if STATUS_RANK.get(status, 0) <= STATUS_RANK.get(m.status.value, 0) and status != "failed":
        return  # nunca regride (webhooks podem chegar fora de ordem)
    m.status = MessageStatus(status)
    now = utcnow()
    if status == "delivered":
        m.delivered_at = m.delivered_at or now
    elif status == "read":
        m.delivered_at = m.delivered_at or now
        m.read_at = now
    elif status == "failed" and error:
        m.error = f"{error.get('code')}: {error.get('title') or error.get('message')}"[:400]
    emit(session, "message.status", m.organization_id, {"message_id": str(m.id), "ticket_id": str(m.ticket_id), "status": status, "error": m.error})
