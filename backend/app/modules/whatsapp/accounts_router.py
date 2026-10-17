"""WhatsApp accounts (WABA): Embedded Signup, manual connection, health, token rotation and sandbox simulator."""
from __future__ import annotations

import time
import uuid
from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import audit
from app.core.deps import Principal, require
from app.core.errors import AppError, bad_request, conflict, not_found
from app.core.events import emit, publish
from app.core.scheduler import periodic
from app.core.serialize import row
from app.db import get_session, session_scope, utcnow
from app.models import WhatsAppAccount
from app.modules.whatsapp.meta_client import MetaClient, MetaError
from app.security.masking import mask_phone

router = APIRouter(prefix="/api/v1/whatsapp", tags=["WhatsApp — Contas & Onboarding"])

PUBLIC_FIELDS = ["id", "label", "waba_id", "phone_number_id", "display_phone_number", "verified_name", "status", "quality_rating", "messaging_limit", "token_expires_at",
                 "token_rotated_at", "default_department_id", "bot_enabled", "payment_method_attached", "last_error", "created_at"]


def account_out(a: WhatsAppAccount) -> dict:
    d = row(a, PUBLIC_FIELDS)
    d["token_set"] = bool(a.access_token)  # the token is never returned by the API
    return d


@router.get("/signup/config")
async def signup_config(p: Principal = Depends(require("whatsapp:manage"))):
    """Public parameters for the Facebook Login SDK (Embedded Signup)."""
    return {"app_id": settings.meta_app_id, "config_id": settings.meta_embedded_signup_config_id, "graph_version": settings.meta_graph_version, "mode": settings.meta_mode,
            "webhook_url": f"{settings.public_base_url.rstrip('/')}/webhooks/whatsapp", "verify_token_hint": "Definido em META_WEBHOOK_VERIFY_TOKEN"}


class SignupCompleteIn(BaseModel):
    code: str = Field(min_length=1, max_length=2000)
    waba_id: str = Field(min_length=1, max_length=40)
    phone_number_id: str = Field(min_length=1, max_length=40)
    label: str | None = None
    pin: str | None = Field(None, pattern=r"^\d{6}$")
    payment_attached: bool = False


@router.post("/signup/complete", status_code=201)
async def signup_complete(body: SignupCompleteIn, request: Request, p: Principal = Depends(require("whatsapp:manage")), session: AsyncSession = Depends(get_session)):
    """Completes Embedded Signup: exchanges the code for a token, subscribes webhooks, registers the number and stores the encrypted token."""
    client = MetaClient()
    try:
        tok = await client.exchange_code(body.code)
        token = tok["access_token"]
        await client.subscribe_app(body.waba_id, token)
        if body.pin:
            await client.register_phone(body.phone_number_id, body.pin, token)
        info = await client.get_phone_info(body.phone_number_id, token)
    except MetaError as exc:
        raise AppError(422, "meta_error", f"Falha no onboarding com a Meta: {exc}")
    existing = (await session.execute(sa.select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == body.phone_number_id))).scalar_one_or_none()
    if existing and existing.organization_id != p.org_id:
        raise conflict("Este número já está conectado a outra organização", "phone_in_use")
    wa = existing or WhatsAppAccount(organization_id=p.org_id, waba_id=body.waba_id, phone_number_id=body.phone_number_id, label=body.label or "WhatsApp")
    wa.access_token, wa.token_rotated_at, wa.status, wa.last_error = token, utcnow(), "active", None
    wa.token_expires_at = utcnow() + timedelta(seconds=int(tok["expires_in"])) if tok.get("expires_in") else None
    wa.display_phone_number, wa.verified_name = info.get("display_phone_number"), info.get("verified_name")
    wa.quality_rating, wa.messaging_limit = info.get("quality_rating"), info.get("messaging_limit_tier")
    wa.payment_method_attached = body.payment_attached
    wa.label = body.label or wa.label
    session.add(wa)
    await session.flush()
    await audit.log_action(session, org_id=p.org_id, action="CONNECT_WHATSAPP", resource_type="whatsapp_accounts", resource_id=wa.id, actor_user_id=p.user_id, request=request,
                           new_state={"waba_id": wa.waba_id, "phone": wa.display_phone_number})
    emit(session, "whatsapp.account_connected", p.org_id, {"account_id": str(wa.id), "phone": mask_phone(wa.display_phone_number)}, p.user_id)
    return account_out(wa)


class ManualConnectIn(BaseModel):
    label: str = "WhatsApp"
    waba_id: str
    phone_number_id: str
    access_token: str = Field(min_length=10)  # System User Access Token


@router.post("/accounts/manual", status_code=201)
async def connect_manual(body: ManualConnectIn, request: Request, p: Principal = Depends(require("whatsapp:manage")), session: AsyncSession = Depends(get_session)):
    client = MetaClient()
    try:
        await client.subscribe_app(body.waba_id, body.access_token)
        info = await client.get_phone_info(body.phone_number_id, body.access_token)
    except MetaError as exc:
        raise AppError(422, "meta_error", str(exc))
    if (await session.execute(sa.select(WhatsAppAccount.id).where(WhatsAppAccount.phone_number_id == body.phone_number_id))).first():
        raise conflict("Número já conectado", "phone_in_use")
    wa = WhatsAppAccount(organization_id=p.org_id, label=body.label, waba_id=body.waba_id, phone_number_id=body.phone_number_id, access_token=body.access_token, token_rotated_at=utcnow(),
                         display_phone_number=info.get("display_phone_number"), verified_name=info.get("verified_name"), quality_rating=info.get("quality_rating"),
                         messaging_limit=info.get("messaging_limit_tier"))
    session.add(wa)
    await session.flush()
    await audit.log_action(session, org_id=p.org_id, action="CONNECT_WHATSAPP", resource_type="whatsapp_accounts", resource_id=wa.id, actor_user_id=p.user_id, request=request,
                           new_state={"phone_number_id": wa.phone_number_id, "access_token": "***"})
    return account_out(wa)


@router.get("/accounts")
async def list_accounts(p: Principal = Depends(require("whatsapp:manage")), session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(sa.select(WhatsAppAccount).where(WhatsAppAccount.organization_id == p.org_id).order_by(WhatsAppAccount.created_at))).scalars().all()
    return {"items": [account_out(a) for a in rows], "mode": settings.meta_mode}


class AccountPatch(BaseModel):
    label: str | None = None
    default_department_id: uuid.UUID | None = None
    bot_enabled: bool | None = None


async def _acc(session: AsyncSession, p: Principal, aid: uuid.UUID) -> WhatsAppAccount:
    a = await session.get(WhatsAppAccount, aid)
    if not a or a.organization_id != p.org_id:
        raise not_found("Conta WhatsApp")
    return a


@router.patch("/accounts/{account_id}")
async def patch_account(account_id: uuid.UUID, body: AccountPatch, p: Principal = Depends(require("whatsapp:manage")), session: AsyncSession = Depends(get_session)):
    a = await _acc(session, p, account_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    return account_out(a)


@router.post("/accounts/{account_id}/rotate-token")
async def rotate_token(account_id: uuid.UUID, request: Request, p: Principal = Depends(require("whatsapp:manage")), session: AsyncSession = Depends(get_session)):
    a = await _acc(session, p, account_id)
    await rotate_account_token(a)
    await audit.log_action(session, org_id=p.org_id, action="ROTATE_WA_TOKEN", resource_type="whatsapp_accounts", resource_id=a.id, actor_user_id=p.user_id, request=request)
    return account_out(a)


@router.get("/accounts/{account_id}/health")
async def health(account_id: uuid.UUID, p: Principal = Depends(require("whatsapp:manage")), session: AsyncSession = Depends(get_session)):
    a = await _acc(session, p, account_id)
    client = MetaClient(a.access_token, a.phone_number_id, a.waba_id)
    try:
        info = await client.get_phone_info(a.phone_number_id)
        a.quality_rating, a.messaging_limit, a.last_error, a.status = info.get("quality_rating"), info.get("messaging_limit_tier"), None, "active"
    except MetaError as exc:
        a.last_error, a.status = str(exc)[:300], "error"
    return account_out(a)


@router.delete("/accounts/{account_id}", status_code=204)
async def disconnect(account_id: uuid.UUID, request: Request, p: Principal = Depends(require("whatsapp:manage")), session: AsyncSession = Depends(get_session)):
    a = await _acc(session, p, account_id)
    try:
        await MetaClient().unsubscribe_app(a.waba_id, a.access_token)
    except MetaError:
        pass
    a.status, a.access_token = "disconnected", None
    await audit.log_action(session, org_id=p.org_id, action="DISCONNECT_WHATSAPP", resource_type="whatsapp_accounts", resource_id=a.id, actor_user_id=p.user_id, request=request)


async def rotate_account_token(a: WhatsAppAccount) -> None:
    """Scheduled renewal: exchanges for a long-lived token and stores the new encrypted token (credential rotation)."""
    if not a.access_token:
        return
    res = await MetaClient().exchange_long_lived(a.access_token)
    a.access_token = res["access_token"]
    a.token_rotated_at = utcnow()
    a.token_expires_at = utcnow() + timedelta(seconds=int(res["expires_in"])) if res.get("expires_in") else None
    a.last_error = None


@periodic("whatsapp.token_rotation", 6 * 3600)
async def scheduled_token_rotation() -> None:
    horizon = utcnow() + timedelta(days=settings.meta_token_refresh_days_before)
    async with session_scope() as s:
        due = (await s.execute(sa.select(WhatsAppAccount).where(WhatsAppAccount.status == "active", WhatsAppAccount.token_expires_at.is_not(None), WhatsAppAccount.token_expires_at < horizon))).scalars().all()
        for a in due:
            try:
                await rotate_account_token(a)
                emit(s, "whatsapp.token_rotated", a.organization_id, {"account_id": str(a.id)})
            except Exception as exc:  # noqa: BLE001
                a.last_error = f"Falha na rotação do token: {str(exc)[:200]}"
                emit(s, "whatsapp.token_error", a.organization_id, {"account_id": str(a.id), "error": a.last_error})


# ------------------------------------------------------------------ Simulator (sandbox only)
class SimulateIn(BaseModel):
    from_number: str = Field(min_length=8, max_length=20)
    name: str | None = "Contato Teste"
    text: str | None = None
    selection_id: str | None = None
    selection_title: str | None = None
    phone_number_id: str | None = None


@router.post("/sandbox/simulate-inbound")
async def simulate_inbound(body: SimulateIn, p: Principal = Depends(require("whatsapp:manage")), session: AsyncSession = Depends(get_session)):
    """Injects into the real pipeline a webhook identical to Meta's (sandbox only) — ideal for demos and tests."""
    if settings.meta_mode != "sandbox":
        raise AppError(403, "not_sandbox", "Disponível apenas em modo sandbox")
    wa = (await session.execute(sa.select(WhatsAppAccount).where(WhatsAppAccount.organization_id == p.org_id, WhatsAppAccount.status == "active").limit(1))).scalar_one_or_none()
    if body.phone_number_id:
        wa = (await session.execute(sa.select(WhatsAppAccount).where(WhatsAppAccount.phone_number_id == body.phone_number_id, WhatsAppAccount.organization_id == p.org_id))).scalar_one_or_none()
    if not wa:
        raise bad_request("Nenhum número WhatsApp ativo. Conecte um em Configurações > WhatsApp.", code="no_whatsapp_account")
    num = "".join(ch for ch in body.from_number if ch.isdigit())
    msg: dict = {"from": num, "id": f"wamid.SIM{uuid.uuid4().hex[:20].upper()}", "timestamp": str(int(time.time()))}
    if body.selection_id:
        kind = "button_reply" if (body.selection_id.startswith("bot:") or ":r:" in body.selection_id) else "list_reply"
        msg.update(type="interactive", interactive={"type": kind, kind: {"id": body.selection_id, "title": body.selection_title or body.selection_id}})
    else:
        msg.update(type="text", text={"body": body.text or ""})
    payload = {"object": "whatsapp_business_account", "entry": [{"id": wa.waba_id, "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": wa.display_phone_number, "phone_number_id": wa.phone_number_id},
        "contacts": [{"profile": {"name": body.name}, "wa_id": num}], "messages": [msg]}}]}]}
    await publish("whatsapp.webhook.received", None, {"payload": payload})
    return {"queued": True, "wamid": msg["id"]}
