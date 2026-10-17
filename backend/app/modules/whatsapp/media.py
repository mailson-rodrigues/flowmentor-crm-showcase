"""Media: secure download from Meta → bucket (S3/GCS/FS) → signed URL with expiry. No binaries in the database."""
from __future__ import annotations

import hashlib
import mimetypes
import uuid
from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.deps import Principal, get_principal
from app.core.errors import AppError, not_found
from app.core.storage import get_storage, verify_media
from app.db import get_session, session_scope, utcnow
from app.models import MediaAsset, Ticket
from app.modules.whatsapp.meta_client import MetaClient
from app.security.rbac import can_access_ticket
from app.security.sanitize import safe_filename

ALLOWED_MIME_PREFIXES = ("image/", "audio/", "video/", "application/pdf", "text/plain", "application/vnd.", "application/msword", "application/octet-stream", "application/zip")
MAX_BYTES = 25 * 1024 * 1024
router = APIRouter(tags=["Mídia"])


def storage_key(org_id: uuid.UUID, ticket_id: uuid.UUID | None, filename: str) -> str:
    return f"{org_id}/{ticket_id or 'misc'}/{uuid.uuid4().hex}_{safe_filename(filename)}"


async def store_bytes(session: AsyncSession, org_id: uuid.UUID, ticket_id: uuid.UUID | None, data: bytes, mime: str | None, filename: str | None, meta_media_id: str | None = None) -> MediaAsset:
    if len(data) > MAX_BYTES:
        raise AppError(413, "media_too_large", "Arquivo excede 25 MB")
    mime = mime or mimetypes.guess_type(filename or "")[0] or "application/octet-stream"
    if not mime.startswith(ALLOWED_MIME_PREFIXES):
        raise AppError(415, "unsupported_media", f"Tipo de arquivo não permitido: {mime}")
    ext = mimetypes.guess_extension(mime.split(";")[0]) or ""
    fname = filename or f"arquivo{ext}"
    key = storage_key(org_id, ticket_id, fname)
    await get_storage().put(key, data, mime)
    asset = MediaAsset(organization_id=org_id, ticket_id=ticket_id, storage_key=key, filename=safe_filename(fname), mime_type=mime, size_bytes=len(data),
                       sha256=hashlib.sha256(data).hexdigest(), meta_media_id=meta_media_id)
    session.add(asset)
    await session.flush()
    return asset


async def download_inbound(session: AsyncSession, client: MetaClient, org_id: uuid.UUID, ticket_id: uuid.UUID, media_id: str, mime: str | None, filename: str | None) -> MediaAsset:
    info = await client.get_media_url(media_id)
    data = await client.download_media(info["url"])
    return await store_bytes(session, org_id, ticket_id, data, mime or info.get("mime_type"), filename, media_id)


def asset_url(asset: MediaAsset, ttl: int | None = None) -> str:
    return get_storage().signed_url(asset.storage_key, ttl, asset.filename)


def public_asset_url(asset: MediaAsset, ttl: int = 3600) -> str:
    """Absolute URL (Meta needs to reach the file in order to send media by link)."""
    url = asset_url(asset, ttl)
    return url if url.startswith("http") else settings.public_base_url.rstrip("/") + url


@router.get("/api/v1/media/{asset_id}/url")
async def media_url(asset_id: uuid.UUID, p: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)):
    """Generates a short-lived signed URL (checks ABAC of the originating ticket)."""
    a = await session.get(MediaAsset, asset_id)
    if not a or a.organization_id != p.org_id or a.deleted_at:
        raise not_found("Mídia")
    if a.ticket_id:
        t = await session.get(Ticket, a.ticket_id)
        if t and not can_access_ticket(p.role, p.user_id, p.department_ids, t):
            raise AppError(403, "forbidden", "Sem acesso a este atendimento")
    return {"url": asset_url(a), "filename": a.filename, "mime_type": a.mime_type, "size_bytes": a.size_bytes, "expires_in": settings.media_url_ttl_seconds}


@router.get("/media/{key}", include_in_schema=False)
async def serve_local_media(key: str, exp: int = Query(...), sig: str = Query(...)):
    import base64

    try:
        real_key = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)).decode()
    except Exception:  # noqa: BLE001
        raise not_found("Mídia")
    if not verify_media(real_key, exp, sig):
        raise AppError(403, "invalid_link", "Link expirado ou inválido")
    try:
        data = await get_storage().get(real_key)
    except Exception:  # noqa: BLE001
        raise not_found("Mídia")
    mime = mimetypes.guess_type(real_key)[0] or "application/octet-stream"
    return Response(data, media_type=mime, headers={"Content-Disposition": f'inline; filename="{real_key.rsplit("/", 1)[-1]}"', "X-Content-Type-Options": "nosniff",
                                                    "Content-Security-Policy": "default-src 'none'; sandbox", "Cache-Control": "private, max-age=300"})


async def purge_expired_media() -> int:
    """Retention: removes binaries older than ``media_retention_days`` (keeps metadata for audit)."""
    cutoff = utcnow() - timedelta(days=settings.media_retention_days)
    n = 0
    async with session_scope() as s:
        rows = (await s.execute(sa.select(MediaAsset).where(MediaAsset.created_at < cutoff, MediaAsset.deleted_at.is_(None)).limit(500))).scalars().all()
        for a in rows:
            try:
                await get_storage().delete(a.storage_key)
            except Exception:  # noqa: BLE001
                continue
            a.deleted_at = utcnow()
            n += 1
    return n
