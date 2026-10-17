"""JWT de acesso (curta duração) + refresh tokens opacos com rotação e revogação."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any

import jwt

from app.config import settings
from app.db import utcnow

ALGORITHM = "HS256"
ISSUER = "flowmentor-crm"


def create_access_token(user_id: uuid.UUID, org_id: uuid.UUID, role: str, extra: dict[str, Any] | None = None) -> str:
    now = utcnow()
    payload = {
        "iss": ISSUER,
        "sub": str(user_id),
        "org": str(org_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_minutes)).timestamp()),
        "jti": uuid.uuid4().hex,
        "typ": "access",
        **(extra or {}),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(
        token, settings.secret_key, algorithms=[ALGORITHM], issuer=ISSUER, options={"require": ["exp", "sub", "org"]}
    )
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("tipo de token inválido")
    return payload


def new_refresh_token() -> tuple[str, str, datetime]:
    """Retorna (token_em_claro, hash_sha256, expira_em)."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_token(raw), utcnow() + timedelta(days=settings.refresh_token_ttl_days)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
