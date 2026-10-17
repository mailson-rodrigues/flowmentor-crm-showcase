"""Immutable (append-only) audit trail with a hash chain (tamper-evident).

Each record stores ``prev_hash`` and ``hash = SHA256(prev_hash | fields)``. Any later edit/removal
breaks the chain and is detected by ``verify_chain``. On PostgreSQL, additional triggers
(migration 0002) block UPDATE/DELETE on the table.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import utcnow
from app.models import AuditLog
from app.security.masking import mask_email, mask_phone

_SENSITIVE_KEYS = {"password", "password_hash", "access_token", "secret", "tax_id", "token", "refresh_token"}


def _clean(state: dict | None) -> dict | None:
    """Removes secrets and masks PII before writing the state to the log."""
    if state is None:
        return None
    out: dict[str, Any] = {}
    for k, v in state.items():
        if k in _SENSITIVE_KEYS:
            out[k] = "***"
        elif k in {"wa_id", "phone", "display_phone_number"} and isinstance(v, str):
            out[k] = mask_phone(v)
        elif k == "email" and isinstance(v, str):
            out[k] = mask_email(v)
        elif isinstance(v, (datetime,)):
            out[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return json.loads(json.dumps(out, default=str))


def _compute_hash(prev: str | None, org: uuid.UUID, seq: int, actor, action, rtype, rid, created: datetime, old, new) -> str:
    material = json.dumps(
        [prev or "", str(org), seq, str(actor) if actor else "", action, rtype, str(rid) if rid else "", created.isoformat(), old, new],
        sort_keys=True, default=str, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


async def log_action(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    request: Request | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    old_state: dict | None = None,
    new_state: dict | None = None,
) -> AuditLog:
    if request is not None:
        ip = ip or client_ip(request)
        user_agent = user_agent or request.headers.get("user-agent")
    if session.bind.dialect.name == "postgresql":  # serializes the chain per organization
        await session.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"audit:{org_id}"})
    last = (
        await session.execute(
            sa.select(AuditLog.seq, AuditLog.hash)
            .where(AuditLog.organization_id == org_id)
            .order_by(AuditLog.seq.desc())
            .limit(1)
        )
    ).first()
    seq = (last.seq + 1) if last else 1
    prev_hash = last.hash if last else None
    created = utcnow()
    old_c, new_c = _clean(old_state), _clean(new_state)
    entry = AuditLog(
        organization_id=org_id, seq=seq, actor_user_id=actor_user_id, action=action, resource_type=resource_type,
        resource_id=resource_id, ip_address=ip or "0.0.0.0", user_agent=(user_agent or "")[:500], old_state=old_c,
        new_state=new_c, prev_hash=prev_hash, created_at=created,
        hash=_compute_hash(prev_hash, org_id, seq, actor_user_id, action, resource_type, resource_id, created, old_c, new_c),
    )
    session.add(entry)
    await session.flush()
    return entry


async def verify_chain(session: AsyncSession, org_id: uuid.UUID, limit: int = 100_000) -> dict:
    rows = (
        await session.execute(
            sa.select(AuditLog).where(AuditLog.organization_id == org_id).order_by(AuditLog.seq.asc()).limit(limit)
        )
    ).scalars().all()
    prev: str | None = None
    expected_seq = rows[0].seq if rows else 1
    for r in rows:
        calc = _compute_hash(prev, r.organization_id, r.seq, r.actor_user_id, r.action, r.resource_type, r.resource_id, r.created_at, r.old_state, r.new_state)
        if r.seq != expected_seq or r.hash != calc or (r.prev_hash or None) != (prev or None):
            return {"valid": False, "broken_at_seq": r.seq, "checked": len(rows)}
        prev = r.hash
        expected_seq += 1
    return {"valid": True, "checked": len(rows)}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"
