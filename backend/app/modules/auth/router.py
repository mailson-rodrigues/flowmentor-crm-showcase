"""Autenticação: login com lockout, refresh com rotação, logout, perfil e presença do agente."""
from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import audit
from app.core.deps import Principal, get_principal
from app.core.errors import AppError, bad_request
from app.core.events import emit
from app.core.serialize import user_out
from app.db import get_session, utcnow
from app.models import AgentPresenceLog, RefreshToken, User
from app.security.passwords import hash_password, needs_rehash, validate_password_policy, verify_password
from app.security.rbac import permissions_for
from app.security.tokens import create_access_token, hash_token, new_refresh_token

router = APIRouter(prefix="/api/v1/auth", tags=["Autenticação"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class RefreshIn(BaseModel):
    refresh_token: str


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


class StatusIn(BaseModel):
    status: str = Field(pattern="^(available|busy|paused|offline)$")
    reason: str | None = Field(default=None, max_length=60)


async def _issue_tokens(session: AsyncSession, user: User, request: Request) -> dict:
    raw, h, exp = new_refresh_token()
    session.add(RefreshToken(user_id=user.id, token_hash=h, expires_at=exp, user_agent=(request.headers.get("user-agent") or "")[:300],
                             ip_address=audit.client_ip(request)))
    return {
        "access_token": create_access_token(user.id, user.organization_id, user.role),
        "refresh_token": raw,
        "token_type": "bearer",
        "expires_in": settings.access_token_ttl_minutes * 60,
        "user": user_out(user),
        "permissions": sorted(permissions_for(user.role)),
    }


@router.post("/login")
async def login(body: LoginIn, request: Request, session: AsyncSession = Depends(get_session)):
    user = (await session.execute(sa.select(User).where(sa.func.lower(User.email) == body.email.lower()))).scalar_one_or_none()
    generic = AppError(401, "invalid_credentials", "E-mail ou senha inválidos")
    if not user or not user.password_hash or not user.is_active:
        # custo constante para não vazar existência da conta
        verify_password(body.password, hash_password("dummy-password"))
        raise generic
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        raise AppError(423, "account_locked", "Conta temporariamente bloqueada por tentativas inválidas. Tente mais tarde.")
    if not verify_password(body.password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= settings.login_max_attempts:
            user.locked_until = now + timedelta(minutes=settings.login_lockout_minutes)
            user.failed_logins = 0
        await audit.log_action(session, org_id=user.organization_id, action="LOGIN_FAILED", resource_type="users", resource_id=user.id,
                               actor_user_id=user.id, request=request)
        await session.commit()  # persiste o contador mesmo respondendo erro
        raise generic
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    await audit.log_action(session, org_id=user.organization_id, action="LOGIN", resource_type="users", resource_id=user.id,
                           actor_user_id=user.id, request=request)
    return await _issue_tokens(session, user, request)


@router.post("/refresh")
async def refresh(body: RefreshIn, request: Request, session: AsyncSession = Depends(get_session)):
    tok = (await session.execute(sa.select(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token)))).scalar_one_or_none()
    if not tok:
        raise AppError(401, "invalid_refresh", "Sessão inválida")
    if tok.revoked_at is not None:
        # reutilização de refresh token rotacionado ⇒ possível roubo: revoga toda a família do usuário
        await session.execute(sa.update(RefreshToken).where(RefreshToken.user_id == tok.user_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
        await session.commit()
        raise AppError(401, "refresh_reuse", "Sessão inválida")
    if tok.expires_at < utcnow():
        raise AppError(401, "refresh_expired", "Sessão expirada")
    user = await session.get(User, tok.user_id)
    if not user or not user.is_active:
        raise AppError(401, "invalid_refresh", "Sessão inválida")
    out = await _issue_tokens(session, user, request)
    tok.revoked_at = utcnow()
    new = (await session.execute(sa.select(RefreshToken).where(RefreshToken.token_hash == hash_token(out["refresh_token"])))).scalar_one_or_none()
    tok.replaced_by = new.id if new else None
    return out


@router.post("/logout", status_code=204)
async def logout(body: RefreshIn, p: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)):
    await session.execute(sa.update(RefreshToken).where(RefreshToken.token_hash == hash_token(body.refresh_token)).values(revoked_at=utcnow()))
    if p.user:
        await _set_status(session, p.user, "offline", "logout")


@router.get("/me")
async def me(p: Principal = Depends(get_principal)):
    if not p.user:
        return {"api_key": True, "scopes": sorted(p.scopes)}
    return {"user": user_out(p.user), "permissions": sorted(p.permissions)}


@router.post("/change-password", status_code=204)
async def change_password(body: PasswordIn, request: Request, p: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)):
    if not p.user or not verify_password(body.current_password, p.user.password_hash or ""):
        raise AppError(400, "invalid_credentials", "Senha atual incorreta")
    problems = validate_password_policy(body.new_password)
    if problems:
        raise bad_request("Senha fraca: " + ", ".join(problems), code="weak_password")
    p.user.password_hash = hash_password(body.new_password)
    await session.execute(sa.update(RefreshToken).where(RefreshToken.user_id == p.user.id, RefreshToken.revoked_at.is_(None)).values(revoked_at=utcnow()))
    await audit.log_action(session, org_id=p.org_id, action="PASSWORD_CHANGED", resource_type="users", resource_id=p.user.id,
                           actor_user_id=p.user.id, request=request)


async def _set_status(session: AsyncSession, user: User, status: str, reason: str | None = None) -> None:
    if user.status == status:
        return
    now = utcnow()
    await session.execute(
        sa.update(AgentPresenceLog).where(AgentPresenceLog.user_id == user.id, AgentPresenceLog.ended_at.is_(None)).values(ended_at=now)
    )
    session.add(AgentPresenceLog(organization_id=user.organization_id, user_id=user.id, status=status, reason=reason, started_at=now))
    prev, user.status, user.status_changed_at = user.status, status, now
    emit(session, "agent.status_changed", user.organization_id, {"user_id": str(user.id), "status": status, "previous": prev, "reason": reason}, actor_id=user.id)


@router.put("/me/status")
async def set_my_status(body: StatusIn, p: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)):
    if not p.user:
        raise AppError(400, "no_user", "Apenas usuários possuem status")
    await _set_status(session, p.user, body.status, body.reason)
    return {"status": p.user.status}


set_user_status = _set_status
