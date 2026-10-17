"""Envelope Encryption (AES-256-GCM) with a per-value data-encryption key (DEK).

Stored format:  ``enc:v1:<kid>:<b64(wrapped_dek)>:<b64(nonce)>:<b64(ciphertext)>``

* Each value gets its own random DEK; the DEK is wrapped (AES-GCM) by the active KEK.
* The KEK can be rotated: new writes use the first key of ``KEK_KEYS``; old
  keys remain valid for decryption until re-encryption (``rewrap``).
* In production the KEK should come from a KMS (AWS KMS / GCP KMS / Vault). ``KeyProvider`` is the port.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

import sqlalchemy as sa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

PREFIX = "enc:v1"


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode())


@dataclass(frozen=True)
class KeyProvider:
    """Port for obtaining the KEKs. The default implementation reads environment variables."""

    keys: dict[str, bytes]
    active_kid: str

    @classmethod
    def from_settings(cls) -> "KeyProvider":
        keys: dict[str, bytes] = {}
        active = ""
        raw = settings.kek_keys.strip()
        if raw:
            for i, part in enumerate(p for p in raw.split(",") if p.strip()):
                kid, _, b64 = part.strip().partition(":")
                key = _b64d(b64)
                if len(key) != 32:
                    raise ValueError(f"KEK '{kid}' deve ter 32 bytes (AES-256)")
                keys[kid] = key
                if i == 0:
                    active = kid
        else:
            # Deterministic derivation from SECRET_KEY: acceptable only in dev/test.
            keys["dev"] = hashlib.sha256(("kek|" + settings.secret_key).encode()).digest()
            active = "dev"
        return cls(keys=keys, active_kid=active)


_provider: KeyProvider | None = None


def provider() -> KeyProvider:
    global _provider
    if _provider is None:
        _provider = KeyProvider.from_settings()
    return _provider


def reset_provider() -> None:
    global _provider
    _provider = None


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX + ":")


def encrypt(plaintext: str | None, aad: bytes = b"") -> str | None:
    if plaintext is None:
        return None
    p = provider()
    kek = p.keys[p.active_kid]
    dek = AESGCM.generate_key(bit_length=256)
    wrap_nonce = os.urandom(12)
    wrapped = wrap_nonce + AESGCM(kek).encrypt(wrap_nonce, dek, p.active_kid.encode())
    nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext.encode(), aad)
    return f"{PREFIX}:{p.active_kid}:{_b64e(wrapped)}:{_b64e(nonce)}:{_b64e(ct)}"


def decrypt(token: str | None, aad: bytes = b"") -> str | None:
    if token is None:
        return None
    if not is_encrypted(token):
        return token  # legacy/plaintext (gradual migration)
    _, _, kid, wrapped_b64, nonce_b64, ct_b64 = token.split(":")
    kek = provider().keys.get(kid)
    if kek is None:
        raise ValueError(f"KEK '{kid}' indisponível para decifrar")
    wrapped = _b64d(wrapped_b64)
    dek = AESGCM(kek).decrypt(wrapped[:12], wrapped[12:], kid.encode())
    return AESGCM(dek).decrypt(_b64d(nonce_b64), _b64d(ct_b64), aad).decode()


def rewrap(token: str) -> str:
    """Re-encrypts with the active KEK (key rotation)."""
    plain = decrypt(token)
    return encrypt(plain) if plain is not None else token


def blind_index(value: str | None, purpose: str = "generic") -> str | None:
    """Deterministic HMAC for searching/unique constraints on encrypted fields (e.g. CNPJ) without exposing the value."""
    if value is None:
        return None
    key = hashlib.sha256(("bidx|" + settings.secret_key).encode()).digest()
    return hmac.new(key, f"{purpose}|{value}".encode(), hashlib.sha256).hexdigest()


class EncryptedString(sa.types.TypeDecorator):
    """Application-level encrypted column (envelope encryption). Transparent to the ORM."""

    impl = sa.Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or is_encrypted(value):
            return value
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value) if value is not None else None
