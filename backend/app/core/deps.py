"""FastAPI dependencies: authentication (JWT or API key), RBAC and multi-tenant scoping."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

import jwt
import sqlalchemy as sa
from fastapi import Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, forbidden
from app.db import get_session, utcnow
from app.models import ApiKey, User
from app.security.rbac import has_permission, permissions_for
from app.security.tokens import decode_access_token


@dataclass
class Principal:
    org_id: uuid.UUID
    role: str
    user: User | None = None
    api_key_id: uuid.UUID | None = None
    scopes: set[str] = field(default_factory=set)

    @property
    def user_id(self) -> uuid.UUID | None:
        return self.user.id if self.user else None

    @property
    def permissions(self) -> set[str]:
        return self.scopes if self.api_key_id else permissions_for(self.role)

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    @property
    def department_ids(self) -> set[uuid.UUID]:
        return set(self.user.department_ids) if self.user else set()


async def get_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    if x_api_key:
        key_hash = hashlib.sha256(x_api_key.encode()).hexdigest()
        key = (await session.execute(sa.select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None)))).scalar_one_or_none()
        if not key:
            raise AppError(401, "invalid_api_key", "API key inválida ou revogada")
        key.last_used_at = utcnow()
        p = Principal(org_id=key.organization_id, role="api", api_key_id=key.id, scopes=set(key.scopes or []))
        request.state.principal = p
        return p
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError(401, "not_authenticated", "Autenticação necessária")
    try:
        payload = decode_access_token(authorization[7:].strip())
    except jwt.ExpiredSignatureError:
        raise AppError(401, "token_expired", "Sessão expirada")
    except jwt.PyJWTError:
        raise AppError(401, "invalid_token", "Token inválido")
    user = (await session.execute(sa.select(User).where(User.id == uuid.UUID(payload["sub"])))).scalar_one_or_none()
    if not user or not user.is_active or str(user.organization_id) != payload["org"]:
        raise AppError(401, "invalid_token", "Usuário inativo ou inexistente")
    p = Principal(org_id=user.organization_id, role=user.role, user=user)
    request.state.principal = p
    return p


def require(*permissions: str):
    """Requires ALL the listed permissions."""

    async def _dep(p: Principal = Depends(get_principal)) -> Principal:
        for perm in permissions:
            if not p.can(perm):
                raise forbidden(f"Permissão necessária: {perm}")
        return p

    return _dep


def require_any(*permissions: str):
    async def _dep(p: Principal = Depends(get_principal)) -> Principal:
        if not any(p.can(x) for x in permissions):
            raise forbidden()
        return p

    return _dep


@dataclass
class Page:
    limit: int
    offset: int


def pagination(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> Page:
    return Page(limit, offset)
