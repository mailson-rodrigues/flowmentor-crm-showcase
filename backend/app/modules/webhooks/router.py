"""Outbound webhooks / Open API: registration of HTTP endpoints that receive CRM events (deal.won, case.created, sla.breached...)."""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
import uuid
from datetime import timedelta
from urllib.parse import urlparse

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.deps import Principal, get_principal, require
from app.core.errors import AppError, bad_request, not_found
from app.core.events import Event, EventBus
from app.core.scheduler import periodic
from app.core.serialize import row
from app.db import get_session, session_scope, utcnow
from app.models import WebhookDelivery, WebhookEndpoint

log = logging.getLogger("flowmentor.webhooks")
router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks / Open API"])

PUBLIC_EVENTS = {
    "account.created": "Conta institucional criada", "contact.created": "Contato criado", "deal.created": "Oportunidade criada", "deal.updated": "Oportunidade atualizada",
    "deal.stage_changed": "Mudança de estágio", "deal.won": "Negócio ganho", "deal.lost": "Negócio perdido", "proposal.sent": "Proposta enviada",
    "contract.created": "Contrato criado", "contract.status_changed": "Status do contrato alterado", "invoice.created": "Fatura emitida", "invoice.paid": "Fatura paga",
    "case.created": "Caso criado", "case.updated": "Caso atualizado", "case.closed": "Caso encerrado", "case.hours_threshold": "Horas consumidas atingiram 80%/100%",
    "report.received": "Laudo recebido", "report.issued": "Laudo emitido", "sla.at_risk": "SLA em risco", "sla.breached": "SLA estourado",
    "ticket.created": "Ticket WhatsApp criado", "ticket.assigned": "Ticket atribuído", "ticket.transferred": "Ticket transferido", "ticket.closed": "Ticket encerrado",
    "message.received": "Mensagem recebida", "survey.answered": "Pesquisa de satisfação respondida", "survey.detractor": "Detrator identificado (CSAT/NPS baixo)",
    "lgpd.request_created": "Solicitação LGPD aberta", "lgpd.anonymized": "Titular anonimizado", "template.status_changed": "Status de template alterado",
}
MAX_ATTEMPTS = 6
BACKOFF = [10, 60, 300, 1800, 7200]


def sign(secret: str, ts: int, body: str) -> str:
    return hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()


def validate_url(url: str) -> None:
    """SSRF protection: https only (http only in dev) and never internal addresses."""
    u = urlparse(url)
    if u.scheme not in ("https", "http") or not u.hostname:
        raise bad_request("URL inválida", code="invalid_url")
    if u.scheme == "http" and settings.is_prod:
        raise bad_request("Em produção apenas HTTPS é permitido", code="https_required")
    if settings.is_prod:
        try:
            for info in socket.getaddrinfo(u.hostname, u.port or 443):
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                    raise bad_request("Destino em rede privada não é permitido", code="private_address")
        except socket.gaierror:
            raise bad_request("Host não resolvido", code="unresolvable_host")


def matches(subscribed: list[str], event_type: str) -> bool:
    return any(s == "*" or fnmatch.fnmatch(event_type, s) for s in subscribed or [])


async def deliver(endpoint: WebhookEndpoint, delivery: WebhookDelivery) -> bool:
    body = json.dumps(delivery.payload, separators=(",", ":"), default=str)
    ts = int(time.time())
    headers = {"Content-Type": "application/json", "User-Agent": "Flowmentor-Webhooks/1.0", "X-Flowmentor-Event": delivery.event_type, "X-Flowmentor-Delivery": str(delivery.id),
               "X-Flowmentor-Signature": f"t={ts},v1={sign(endpoint.secret, ts, body)}"}
    delivery.attempts += 1
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
            r = await c.post(endpoint.url, content=body, headers=headers)
        delivery.response_status, delivery.response_body = r.status_code, r.text[:500]
        ok = 200 <= r.status_code < 300
    except Exception as exc:  # noqa: BLE001
        delivery.response_status, delivery.response_body = None, f"{type(exc).__name__}: {str(exc)[:300]}"
        ok = False
    if ok:
        delivery.status, delivery.delivered_at, delivery.next_attempt_at = "success", utcnow(), None
        endpoint.consecutive_failures = 0
    else:
        endpoint.consecutive_failures += 1
        if delivery.attempts >= MAX_ATTEMPTS:
            delivery.status, delivery.next_attempt_at = "dead", None
        else:
            delivery.status, delivery.next_attempt_at = "failed", utcnow() + timedelta(seconds=BACKOFF[min(delivery.attempts - 1, len(BACKOFF) - 1)])
        if endpoint.consecutive_failures >= 30:
            endpoint.is_active, endpoint.disabled_reason = False, "Desativado automaticamente após falhas consecutivas"
    return ok


async def dispatch_event(event: Event) -> None:
    if event.org_id is None or event.type not in PUBLIC_EVENTS:
        return
    async with session_scope() as s:
        eps = (await s.execute(sa.select(WebhookEndpoint).where(WebhookEndpoint.organization_id == uuid.UUID(event.org_id), WebhookEndpoint.is_active.is_(True)))).scalars().all()
        for ep in eps:
            if not matches(ep.events, event.type):
                continue
            d = WebhookDelivery(organization_id=ep.organization_id, endpoint_id=ep.id, event_id=event.id, event_type=event.type,
                                payload={"id": event.id, "type": event.type, "created_at": event.ts, "data": event.payload})
            s.add(d)
            await s.flush()
            await deliver(ep, d)


@periodic("webhooks.retry", 20)
async def retry_failed() -> None:
    async with session_scope() as s:
        q = sa.select(WebhookDelivery).where(WebhookDelivery.status == "failed", WebhookDelivery.next_attempt_at <= utcnow()).limit(50)
        if s.bind.dialect.name == "postgresql":
            q = q.with_for_update(skip_locked=True)
        for d in (await s.execute(q)).scalars().all():
            ep = await s.get(WebhookEndpoint, d.endpoint_id)
            if ep is None or not ep.is_active:
                d.status = "dead"
                continue
            await deliver(ep, d)


def register(bus: EventBus) -> None:
    bus.subscribe("*", dispatch_event, "webhooks.dispatch")


# ------------------------------------------------------------------ Management API
class EndpointIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    url: str = Field(max_length=500)
    events: list[str] = Field(min_length=1)


def endpoint_out(e: WebhookEndpoint) -> dict:
    return row(e, ["id", "name", "url", "events", "is_active", "consecutive_failures", "disabled_reason", "created_at"])


def _validate_events(events: list[str]) -> None:
    for ev in events:
        if ev != "*" and not any(fnmatch.fnmatch(k, ev) for k in PUBLIC_EVENTS):
            raise bad_request(f"Evento desconhecido: {ev}", code="unknown_event")


@router.get("/events")
async def catalog(p: Principal = Depends(get_principal)):
    return {"events": [{"type": k, "description": v} for k, v in sorted(PUBLIC_EVENTS.items())]}


@router.get("/endpoints")
async def list_endpoints(p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(sa.select(WebhookEndpoint).where(WebhookEndpoint.organization_id == p.org_id).order_by(WebhookEndpoint.created_at.desc()))).scalars().all()
    return {"items": [endpoint_out(e) for e in rows]}


@router.post("/endpoints", status_code=201)
async def create_endpoint(body: EndpointIn, p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    validate_url(body.url)
    _validate_events(body.events)
    secret = "whsec_" + secrets.token_urlsafe(32)
    e = WebhookEndpoint(organization_id=p.org_id, name=body.name, url=body.url, events=body.events, secret=secret, created_by=p.user_id)
    session.add(e)
    await session.flush()
    return {**endpoint_out(e), "secret": secret, "note": "Guarde o segredo: ele não será exibido novamente. Assinatura: X-Flowmentor-Signature = t=<unix>,v1=HMAC_SHA256(secret, '<t>.<corpo>')."}


@router.put("/endpoints/{endpoint_id}")
async def update_endpoint(endpoint_id: uuid.UUID, body: EndpointIn, p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    e = await session.get(WebhookEndpoint, endpoint_id)
    if not e or e.organization_id != p.org_id:
        raise not_found("Endpoint")
    validate_url(body.url)
    _validate_events(body.events)
    e.name, e.url, e.events = body.name, body.url, body.events
    return endpoint_out(e)


@router.post("/endpoints/{endpoint_id}/toggle")
async def toggle_endpoint(endpoint_id: uuid.UUID, p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    e = await session.get(WebhookEndpoint, endpoint_id)
    if not e or e.organization_id != p.org_id:
        raise not_found("Endpoint")
    e.is_active = not e.is_active
    if e.is_active:
        e.consecutive_failures, e.disabled_reason = 0, None
    return endpoint_out(e)


@router.post("/endpoints/{endpoint_id}/rotate-secret")
async def rotate_secret(endpoint_id: uuid.UUID, p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    e = await session.get(WebhookEndpoint, endpoint_id)
    if not e or e.organization_id != p.org_id:
        raise not_found("Endpoint")
    e.secret = "whsec_" + secrets.token_urlsafe(32)
    return {"secret": e.secret}


@router.delete("/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(endpoint_id: uuid.UUID, p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    e = await session.get(WebhookEndpoint, endpoint_id)
    if not e or e.organization_id != p.org_id:
        raise not_found("Endpoint")
    await session.delete(e)


@router.post("/endpoints/{endpoint_id}/test")
async def test_endpoint(endpoint_id: uuid.UUID, p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    e = await session.get(WebhookEndpoint, endpoint_id)
    if not e or e.organization_id != p.org_id:
        raise not_found("Endpoint")
    d = WebhookDelivery(organization_id=p.org_id, endpoint_id=e.id, event_id=uuid.uuid4().hex, event_type="ping",
                        payload={"id": uuid.uuid4().hex, "type": "ping", "created_at": utcnow().isoformat(), "data": {"message": "Teste de webhook Flowmentor"}})
    session.add(d)
    await session.flush()
    ok = await deliver(e, d)
    return {"success": ok, "status": d.response_status, "response": d.response_body}


@router.get("/deliveries")
async def deliveries(endpoint_id: uuid.UUID | None = None, status: str | None = None, limit: int = 100, p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    q = sa.select(WebhookDelivery).where(WebhookDelivery.organization_id == p.org_id)
    if endpoint_id:
        q = q.where(WebhookDelivery.endpoint_id == endpoint_id)
    if status:
        q = q.where(WebhookDelivery.status == status)
    rows = (await session.execute(q.order_by(WebhookDelivery.created_at.desc()).limit(min(limit, 300)))).scalars().all()
    return {"items": [row(d, ["id", "endpoint_id", "event_type", "status", "attempts", "response_status", "response_body", "next_attempt_at", "delivered_at", "created_at"]) for d in rows]}


@router.post("/deliveries/{delivery_id}/redeliver")
async def redeliver(delivery_id: uuid.UUID, p: Principal = Depends(require("webhooks:manage")), session: AsyncSession = Depends(get_session)):
    d = await session.get(WebhookDelivery, delivery_id)
    if not d or d.organization_id != p.org_id:
        raise not_found("Entrega")
    e = await session.get(WebhookEndpoint, d.endpoint_id)
    d.attempts = 0
    ok = await deliver(e, d)
    return {"success": ok, "status": d.response_status}
