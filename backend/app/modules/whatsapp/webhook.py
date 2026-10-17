"""Endpoint HTTP de webhooks da Meta: valida assinatura, enfileira e responde 200 OK imediatamente (< 200 ms)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.core.errors import AppError
from app.core.events import publish

log = logging.getLogger("flowmentor.webhook")
router = APIRouter(tags=["Webhook Meta"])


def verify_signature(body: bytes, header: str | None) -> bool:
    if not settings.meta_app_secret:
        # Sem segredo configurado só é aceitável fora de produção (sandbox/dev).
        return not settings.is_prod
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.meta_app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


@router.get("/webhooks/whatsapp", response_class=PlainTextResponse)
async def verify(hub_mode: str = Query(alias="hub.mode", default=""), hub_verify_token: str = Query(alias="hub.verify_token", default=""),
                 hub_challenge: str = Query(alias="hub.challenge", default="")):
    """Handshake de verificação do webhook (configuração no App da Meta)."""
    if hub_mode == "subscribe" and hmac.compare_digest(hub_verify_token, settings.meta_webhook_verify_token):
        return hub_challenge
    raise AppError(403, "verification_failed", "Token de verificação inválido")


@router.post("/webhooks/whatsapp")
async def receive(request: Request):
    """Caminho crítico: lê o corpo, valida HMAC, publica no barramento (Redis Streams/Kafka) e devolve 200.

    Nenhum acesso a banco ou chamada externa acontece aqui; todo o processamento é assíncrono.
    """
    body = await request.body()
    if not verify_signature(body, request.headers.get("x-hub-signature-256")):
        log.warning("assinatura inválida em webhook da Meta")
        return Response(status_code=403)
    try:
        payload = json.loads(body)
    except ValueError:
        return Response(status_code=400)
    if payload.get("object") != "whatsapp_business_account":
        return Response(status_code=200)  # ignora outros produtos
    await publish("whatsapp.webhook.received", None, {"payload": payload})
    return Response(status_code=200)
