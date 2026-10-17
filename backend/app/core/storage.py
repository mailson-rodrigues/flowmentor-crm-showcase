"""Object storage (media) — binaries stay outside the relational database.

* LocalStorage: dev/test. HMAC-signed URLs with expiry, served by ``/media/...``.
* S3Storage: production (S3 / GCS interoperable / MinIO). Native pre-signed URLs with expiry.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from pathlib import Path

from app.config import settings


class Storage:
    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    def signed_url(self, key: str, ttl: int | None = None, filename: str | None = None) -> str: ...


class LocalStorage(Storage):
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents and p != self.root:  # path traversal
            raise ValueError("chave inválida")
        return p

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_bytes, data)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            await asyncio.to_thread(p.unlink)

    def signed_url(self, key: str, ttl: int | None = None, filename: str | None = None) -> str:
        exp = int(time.time()) + (ttl or settings.media_url_ttl_seconds)
        sig = sign_media(key, exp)
        k = base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")
        return f"/media/{k}?exp={exp}&sig={sig}"


def sign_media(key: str, exp: int) -> str:
    return hmac.new(settings.secret_key.encode(), f"{key}|{exp}".encode(), hashlib.sha256).hexdigest()


def verify_media(key: str, exp: int, sig: str) -> bool:
    return exp >= time.time() and hmac.compare_digest(sign_media(key, exp), sig)


class S3Storage(Storage):
    def __init__(self) -> None:
        import boto3

        self.client = boto3.client("s3", region_name=settings.storage_s3_region, endpoint_url=settings.storage_s3_endpoint)
        self.bucket = settings.storage_bucket

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key, Body=data, ServerSideEncryption="AES256", **extra)

    async def get(self, key: str) -> bytes:
        obj = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(obj["Body"].read)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

    def signed_url(self, key: str, ttl: int | None = None, filename: str | None = None) -> str:
        params = {"Bucket": self.bucket, "Key": key}
        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl or settings.media_url_ttl_seconds)


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = S3Storage() if settings.storage_backend == "s3" else LocalStorage(settings.storage_local_path)
    return _storage


def set_storage(s: Storage | None) -> None:
    global _storage
    _storage = s
